"""Admin > Send Invitation: type an email, click Send, the welcome email goes out with a single-use link. Needs MAVLIT_TEST_PG_URL."""
import os
import re
import sys
import unittest
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


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class SendInvitationPage(unittest.TestCase):
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
        import pipeline.mailer as mailer
        from pipeline.db import get_db
        engine = self.engine
        factory = lambda *a, **k: Session(bind=engine, autoflush=False, expire_on_commit=False)

        def override():
            with factory() as session:
                yield session

        api_app.app.dependency_overrides[get_db] = override
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        self.sent, self.mail_error = [], None

        def fake_send(messages):
            if self.mail_error:
                raise self.mail_error
            self.sent.extend(messages)
            return len(messages)

        for patcher in (mock.patch.object(attempt_limits, "SessionLocal", factory), mock.patch.object(api_app, "SessionLocal", factory),
                        mock.patch.object(api_app, "operator_configured", lambda: True), mock.patch.object(api_app, "passkey_matches", lambda v: True),
                        mock.patch.object(api_app, "passkey", lambda: "p" * 40), mock.patch.object(mailer, "send_by_smtp", fake_send)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api_app.app, follow_redirects=False)
        self.assertEqual(self.client.post("/admin/login", data={"username": "", "password": "x"}).status_code, 302)
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, account_invites, login_failures RESTART IDENTITY CASCADE"))
            conn.execute(text("INSERT INTO creator_profiles (email, password_hash, is_active) VALUES ('member@example.test','x',true)"))

    def rows(self):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT email FROM account_invites")).scalars().all()

    def test_the_page_is_behind_the_admin_login(self):
        anonymous = TestClient(self.client.app, follow_redirects=False)
        self.assertEqual(anonymous.get("/admin/send-invite").status_code, 302)
        self.assertEqual(anonymous.post("/admin/send-invite", data={"email": "a@b.co"}).status_code, 302)
        self.assertEqual((self.sent, self.rows()), ([], []))
        page = self.client.get("/admin/send-invite")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Send an invitation email", page.text)
        self.assertIn("Send Invitation", page.text)   # in the admin menu

    def test_sending_creates_a_working_single_use_link_and_emails_it(self):
        response = self.client.post("/admin/send-invite", data={"email": " New@Example.test ", "first_name": "Ali", "days": "7"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Invitation sent to new@example.test", response.text)
        self.assertIn("expires in 7 days", response.text)
        self.assertEqual(self.rows(), ["new@example.test"])
        self.assertEqual(len(self.sent), 1)
        message = self.sent[0]
        self.assertEqual(message["To"], "new@example.test")
        body = message.get_body(("plain",)).get_content()
        self.assertTrue(body.startswith("Hi Ali,"))
        token = re.search(r"set-password\?token=([\w-]+)", body).group(1)
        self.assertEqual(self.client.get(f"/auth/invite/{token}").json(), {"status": "valid", "email": "new@example.test"})
        self.assertIn("new@example.test", response.text)   # listed under recent invitations, form cleared for the next one

    def test_an_email_with_an_account_is_refused_and_nothing_is_sent(self):
        response = self.client.post("/admin/send-invite", data={"email": "Member@example.test"})
        self.assertIn("already has a MAVLIT account", response.text)
        self.assertEqual((self.sent, self.rows()), ([], []))

    def test_bad_input_is_refused(self):
        self.assertIn("valid email", self.client.post("/admin/send-invite", data={"email": "not an email"}).text)
        self.assertIn("1 to 365 days", self.client.post("/admin/send-invite", data={"email": "a@b.co", "days": "0"}).text)
        self.assertIn("1 to 365 days", self.client.post("/admin/send-invite", data={"email": "a@b.co", "days": "abc"}).text)
        self.assertEqual((self.sent, self.rows()), ([], []))

    def test_a_mail_failure_is_reported_and_a_second_send_replaces_the_link(self):
        self.mail_error = RuntimeError("no smtp")
        response = self.client.post("/admin/send-invite", data={"email": "new@example.test"})
        self.assertIn("email sending is not set up", response.text)
        self.mail_error = OSError("connection refused")
        self.assertIn("could not be sent", self.client.post("/admin/send-invite", data={"email": "new@example.test"}).text)
        self.mail_error = None
        self.assertIn("Invitation sent", self.client.post("/admin/send-invite", data={"email": "new@example.test"}).text)
        self.assertEqual((len(self.sent), self.rows()), (1, ["new@example.test"]))   # one row per email: the last link is the one that works


if __name__ == "__main__":
    unittest.main()
