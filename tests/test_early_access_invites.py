"""Early-access invitation links: the email, the single-use token and the password page's API. DB tests need MAVLIT_TEST_PG_URL (a disposable local database)."""
import os
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

from pipeline import early_access_invites as inv  # noqa: E402

PASSWORD = "a long enough password"


class EmailAndToken(unittest.TestCase):
    def test_token_link_and_hash(self):
        self.assertEqual(inv.DEFAULT_DAYS, 30)   # a link works for 30 days unless --days says otherwise
        a, b = inv.make_token(), inv.make_token()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 40)
        self.assertEqual(inv.invite_link("T", "https://app.mavlit.com/"), "https://app.mavlit.com/set-password?token=T")
        self.assertEqual(len(inv.hash_token(a)), 64)
        self.assertNotIn(a, inv.hash_token(a))

    def test_the_email_carries_the_link_in_text_and_html(self):
        link = inv.invite_link("TOKEN123")
        message = inv.build_message("new@example.test", link, 30, "MAVLIT <hello@mavlit.com>", "Ali Ahmed")
        self.assertEqual(message["To"], "new@example.test")
        self.assertEqual(message["Subject"], inv.SUBJECT)
        self.assertTrue(message["Date"] and message["Message-ID"].endswith("@mavlit.com>"))
        text_part, html_part = message.get_body(("plain",)).get_content(), message.get_body(("html",)).get_content()
        for part in (text_part, html_part):
            self.assertIn(link, part)
            self.assertIn("Welcome to MAVLIT!", part)
            self.assertIn("founding creators", part)
            self.assertIn("30 days", part)
            self.assertIn("The MAVLIT Team", part)
            self.assertIn("https://mavlit.com", part)
            self.assertNotIn("{{", part)
        self.assertTrue(text_part.startswith("Hi Ali,\n"))
        self.assertIn("Choose Your Password", html_part)

    def test_the_greeting_falls_back_when_there_is_no_name(self):
        message = inv.build_message("new@example.test", inv.invite_link("T"), 30)
        self.assertTrue(message.get_body(("plain",)).get_content().startswith("Hi there,\n"))


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
        from pipeline.db import get_db
        engine = self.engine
        self.factory = lambda *a, **k: Session(bind=engine, autoflush=False, expire_on_commit=False)

        def override():
            with self.factory() as session:
                yield session

        api_app.app.dependency_overrides[get_db] = override
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        for patcher in (mock.patch.object(attempt_limits, "SessionLocal", self.factory), mock.patch.object(api_app, "SessionLocal", self.factory)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api_app.app, raise_server_exceptions=False)
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, early_access_signups, account_invites, login_failures RESTART IDENTITY CASCADE"))
            conn.execute(text("INSERT INTO early_access_signups (email, handle, platform, niche, followers) VALUES "
                              "('new@example.test','newbie','instagram','music','under10k'),"
                              "('old@example.test','oldie','instagram','beauty','10k-50k')"))
            conn.execute(text("INSERT INTO creator_profiles (email, password_hash, is_active) VALUES ('old@example.test','x',true)"))

    def issue(self, email="new@example.test", days=14):
        with self.factory() as db:
            made = inv.invite(db, [email], days, "https://app.mavlit.com", None, None)
        self.assertEqual(len(made), 1)
        return made[0][1].split("token=")[1]

    def test_only_signups_without_an_account_are_invited(self):
        with self.factory() as db:
            self.assertEqual([s.email for s in inv.people_to_invite(db)], ["new@example.test"])

    def test_only_the_emails_given_are_invited_and_accounts_are_skipped(self):
        with self.factory() as db:
            todo, skipped = inv.people_for_emails(db, [" New@Example.test ", "old@example.test", "new@example.test", "other@example.test", ""])
        self.assertEqual((todo, skipped), (["new@example.test", "other@example.test"], ["old@example.test"]))   # an email outside the table is used as given
        with self.factory() as db:
            inv.invite(db, todo[:1], 14, "https://app.mavlit.com", None, None)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT email FROM account_invites")).scalars().all(), ["new@example.test"])   # nobody else got a token

    def test_the_token_is_stored_only_as_a_hash(self):
        token = self.issue()
        with self.engine.connect() as conn:
            stored = conn.execute(text("SELECT token_hash FROM account_invites")).scalar_one()
        self.assertEqual(stored, inv.hash_token(token))
        self.assertNotEqual(stored, token)

    def test_status_valid_shows_the_email_and_unknown_tokens_show_nothing(self):
        token = self.issue()
        self.assertEqual(self.client.get(f"/auth/invite/{token}").json(), {"status": "valid", "email": "new@example.test"})
        self.assertEqual(self.client.get("/auth/invite/nope").json(), {"status": "invalid", "email": None})

    def test_choosing_a_password_creates_the_account_uses_the_link_and_the_login_works(self):
        token = self.issue()
        short = self.client.post("/auth/invite/accept", json={"token": token, "password": "short"})
        self.assertEqual(short.status_code, 422)
        ok = self.client.post("/auth/invite/accept", json={"token": token, "password": PASSWORD})
        self.assertEqual((ok.status_code, ok.json()), (200, {"status": "ok", "email": "new@example.test"}))
        login = self.client.post("/auth/login", json={"email": "new@example.test", "password": PASSWORD})
        self.assertEqual(login.status_code, 200)
        # the link is used up: the status says so, and a second use is refused (and creates nothing)
        self.assertEqual(self.client.get(f"/auth/invite/{token}").json(), {"status": "used", "email": None})
        again = self.client.post("/auth/invite/accept", json={"token": token, "password": "another long password"})
        self.assertEqual((again.status_code, again.json()["detail"]["status"]), (410, "used"))
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM creator_profiles WHERE email='new@example.test'")).scalar_one(), 1)
        self.assertEqual(self.client.post("/auth/login", json={"email": "new@example.test", "password": "another long password"}).status_code, 401)

    def test_an_expired_link_cannot_be_used(self):
        token = self.issue()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE account_invites SET expires_at = :t"), {"t": datetime.now(timezone.utc) - timedelta(minutes=1)})
        self.assertEqual(self.client.get(f"/auth/invite/{token}").json()["status"], "expired")
        self.assertEqual(self.client.post("/auth/invite/accept", json={"token": token, "password": PASSWORD}).status_code, 410)

    def test_inviting_again_replaces_the_earlier_link(self):
        first, second = self.issue(), self.issue()
        self.assertEqual(self.client.get(f"/auth/invite/{first}").json()["status"], "invalid")
        self.assertEqual(self.client.get(f"/auth/invite/{second}").json()["status"], "valid")

    def test_a_link_for_an_email_that_already_has_an_account_counts_as_used(self):
        token = self.issue()
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO creator_profiles (email, password_hash, is_active) VALUES ('new@example.test','x',true)"))
        self.assertEqual(self.client.get(f"/auth/invite/{token}").json()["status"], "used")
        self.assertEqual(self.client.post("/auth/invite/accept", json={"token": token, "password": PASSWORD}).status_code, 410)


if __name__ == "__main__":
    unittest.main()
