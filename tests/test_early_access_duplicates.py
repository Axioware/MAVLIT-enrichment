"""One early-access sign-up per email: an email that has an account, or already filled in the form, is refused (409). Needs MAVLIT_TEST_PG_URL."""
import os
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

BODY = {"email": "new@example.test", "handle": "newbie", "platform": "instagram", "niche": ["music"], "followers": "under10k"}


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class DuplicateSignups(unittest.TestCase):
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
        import api.early_access as early
        from pipeline.db import get_db
        early._recent.clear()
        engine = self.engine

        def override():
            with Session(bind=engine, autoflush=False, expire_on_commit=False) as session:
                yield session

        api_app.app.dependency_overrides[get_db] = override
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        self.client = TestClient(api_app.app, raise_server_exceptions=False)
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, early_access_signups RESTART IDENTITY CASCADE"))
            conn.execute(text("INSERT INTO creator_profiles (email, password_hash, is_active) VALUES ('member@example.test','x',true)"))

    def signups(self):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT email, handle FROM early_access_signups ORDER BY id")).all()

    def test_the_first_signup_is_saved(self):
        response = self.client.post("/early-access", json=BODY)
        self.assertEqual((response.status_code, response.json()), (201, {"ok": True}))
        self.assertEqual(self.signups(), [("new@example.test", "newbie")])

    def test_filling_the_form_a_second_time_is_refused_and_changes_nothing(self):
        self.client.post("/early-access", json=BODY)
        again = self.client.post("/early-access", json={**BODY, "email": " New@Example.test ", "handle": "someone_else", "niche": ["beauty"]})
        self.assertEqual(again.status_code, 409)
        self.assertIn("already filled in this form", again.json()["detail"])
        self.assertEqual(self.signups(), [("new@example.test", "newbie")])   # the first answers stay as they were

    def test_an_email_that_already_has_an_account_cannot_sign_up(self):
        response = self.client.post("/early-access", json={**BODY, "email": "Member@example.test"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("already have a MAVLIT account", response.json()["detail"])
        self.assertEqual(self.signups(), [])

    def test_the_hidden_bot_field_still_gets_a_quiet_success(self):
        self.assertEqual(self.client.post("/early-access", json={**BODY, "website": "bot"}).status_code, 201)
        self.assertEqual(self.signups(), [])


if __name__ == "__main__":
    unittest.main()
