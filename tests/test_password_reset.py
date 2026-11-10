"""Forgot password: the email, the single-use link and the reset API. DB tests need MAVLIT_TEST_PG_URL (a disposable local database)."""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from pipeline.password_reset_email import BUTTON_TEXT, SUBJECT, build_reset_message  # noqa: E402

OLD, NEW = "old password 123", "brand new password"


class Email(unittest.TestCase):
    def test_the_reset_email(self):
        link = "https://app.mavlit.com/reset-password?token=TOK123"
        message = build_reset_message("a@example.test", link, 60, "MAVLIT <contact@mavlit.com>")
        self.assertEqual((message["To"], message["Subject"]), ("a@example.test", SUBJECT))
        text_part, html_part = message.get_body(("plain",)).get_content(), message.get_body(("html",)).get_content()
        for part in (text_part, html_part):
            self.assertIn(link, part)
            self.assertIn("Reset My Password", part)
            self.assertIn("60 minutes", part)
            self.assertIn("ignore this email", part)
        self.assertIn("cid:mavlit-logo", html_part)
        self.assertEqual(BUTTON_TEXT, "Reset My Password")
        self.assertEqual([p.get_content_type() for p in message.walk()].count("image/png"), 1)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class AgainstPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        host = url.query.get("host") or url.host or ""
        if not ((host in ("", "localhost", "127.0.0.1") or str(host).startswith("/")) and any(w in (url.database or "") for w in ("test", "scratch"))):
            raise RuntimeError("MAVLIT_TEST_PG_URL must be a LOCAL database whose name contains 'test' or 'scratch'; refusing to run")
        from pipeline import migrations
        cls.engine = create_engine(TEST_URL, poolclass=NullPool)
        with cls.engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        migrations.run_migrations(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        import api.app as api_app
        import api.attempt_limits as attempt_limits
        import api.password_reset as pr
        from pipeline.db import get_db
        from pipeline.helpers.passwords import hash_password
        engine = self.engine
        self.factory = lambda *a, **k: Session(bind=engine, autoflush=False, expire_on_commit=False)

        def override():
            with self.factory() as session:
                yield session

        api_app.app.dependency_overrides[get_db] = override
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        self.sent = []
        patchers = [mock.patch.object(attempt_limits, "SessionLocal", self.factory), mock.patch.object(api_app, "SessionLocal", self.factory),
                    mock.patch.object(pr, "send_by_smtp", side_effect=lambda messages: self.sent.extend(messages) or len(messages))]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api_app.app, raise_server_exceptions=False)
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, password_reset_tokens, login_failures RESTART IDENTITY CASCADE"))
            for email, active in (("Alex@example.test", True), ("off@example.test", False)):
                conn.execute(text("INSERT INTO creator_profiles (email, password_hash, is_active) VALUES (:e, :h, :a)"),
                             {"e": email, "h": hash_password(OLD), "a": active})

    def ask(self, email="Alex@example.test", expect="ok"):
        response = self.client.post("/auth/forgot-password", json={"email": email})
        self.assertEqual((response.status_code, response.json()), (200, {"status": expect}))
        return response

    def token_from_mail(self):
        message = self.sent[-1]
        return re.search(r"reset-password\?token=([\w-]+)", message.get_body(("plain",)).get_content()).group(1)

    def rows(self):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT count(*) FROM password_reset_tokens")).scalar_one()

    def test_a_registered_account_gets_one_email_and_other_addresses_are_told_and_get_nothing(self):
        self.ask("Alex@example.test")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["To"], "Alex@example.test")
        self.ask("Alex@example.test")   # case does not matter
        self.ask("nobody@example.test", "not_registered")
        self.ask("off@example.test", "inactive")
        self.ask("not an email", "invalid_email")
        self.assertEqual([m["To"] for m in self.sent], ["Alex@example.test", "Alex@example.test"])   # nothing went to the others
        self.assertEqual(self.rows(), 1)                                                          # and no link was made for them

    def test_the_token_is_stored_only_as_a_hash(self):
        self.ask()
        token = self.token_from_mail()
        with self.engine.connect() as conn:
            stored = conn.execute(text("SELECT token_hash FROM password_reset_tokens")).scalar_one()
        self.assertNotIn(token, stored)
        self.assertEqual(len(stored), 64)

    def test_the_full_reset_flow(self):
        self.ask()
        token = self.token_from_mail()
        self.assertEqual(self.client.get(f"/auth/password-reset/{token}").json(), {"status": "valid", "email": "Alex@example.test"})
        old_cookie = self.client.post("/auth/login", json={"email": "Alex@example.test", "password": OLD}).cookies.get("access_token")
        self.assertTrue(old_cookie)
        short = self.client.post("/auth/password-reset", json={"token": token, "password": "abcde"})
        self.assertEqual(short.status_code, 422)
        ok = self.client.post("/auth/password-reset", json={"token": token, "password": NEW})
        self.assertEqual((ok.status_code, ok.json()), (200, {"status": "ok", "email": "Alex@example.test"}))
        # the new password works, the old one does not, and the old session is ended
        self.assertEqual(self.client.post("/auth/login", json={"email": "Alex@example.test", "password": NEW}).status_code, 200)
        self.assertEqual(self.client.post("/auth/login", json={"email": "Alex@example.test", "password": OLD}).status_code, 401)
        stale = TestClient(self.client.app, raise_server_exceptions=False)
        stale.cookies.set("access_token", old_cookie)
        self.assertEqual(stale.get("/auth/me").status_code, 401)
        # the link is used up
        self.assertEqual(self.client.get(f"/auth/password-reset/{token}").json(), {"status": "used", "email": None})
        again = self.client.post("/auth/password-reset", json={"token": token, "password": "another password"})
        self.assertEqual((again.status_code, again.json()["detail"]["status"]), (410, "used"))
        self.assertEqual(self.client.post("/auth/login", json={"email": "Alex@example.test", "password": "another password"}).status_code, 401)

    def test_a_lockout_from_wrong_passwords_does_not_block_the_new_password(self):
        for _ in range(6):
            self.client.post("/auth/login", json={"email": "Alex@example.test", "password": "wrong wrong"})
        self.ask()
        token = self.token_from_mail()
        self.assertEqual(self.client.post("/auth/password-reset", json={"token": token, "password": NEW}).status_code, 200)
        self.assertEqual(self.client.post("/auth/login", json={"email": "Alex@example.test", "password": NEW}).status_code, 200)

    def test_expired_unknown_and_replaced_links(self):
        self.ask()
        first = self.token_from_mail()
        self.assertEqual(self.client.get("/auth/password-reset/nope").json(), {"status": "invalid", "email": None})
        self.ask()                                  # asking again replaces the link
        second = self.token_from_mail()
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.get(f"/auth/password-reset/{first}").json()["status"], "invalid")
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE password_reset_tokens SET expires_at = :t"), {"t": datetime.now(timezone.utc) - timedelta(minutes=1)})
        self.assertEqual(self.client.get(f"/auth/password-reset/{second}").json()["status"], "expired")
        self.assertEqual(self.client.post("/auth/password-reset", json={"token": second, "password": NEW}).status_code, 410)

    def test_a_deactivated_account_cannot_use_a_link(self):
        self.ask()
        token = self.token_from_mail()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET is_active = false WHERE email = 'Alex@example.test'"))
        self.assertEqual(self.client.get(f"/auth/password-reset/{token}").json()["status"], "invalid")
        self.assertEqual(self.client.post("/auth/password-reset", json={"token": token, "password": NEW}).status_code, 410)

    def test_one_address_is_mailed_at_most_three_times_an_hour(self):
        for _ in range(3):
            self.ask()
        self.ask(expect="limited")
        self.ask(expect="limited")
        self.assertEqual(len(self.sent), 3)


if __name__ == "__main__":
    unittest.main()
