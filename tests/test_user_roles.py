"""Roles: every account is a "user" or an "admin". The first four admins are set once by the migration; only admins reach GET /usage/summary. Needs MAVLIT_TEST_PG_URL."""
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

PASSWORD = "Sup3r-secret-pass"
ADMINS = ["abdulrauf@axioware.tech", "alternativenationnet@gmail.com", "brett@gmail.com", "abc@gmail.com"]


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class Roles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        host = url.query.get("host") or url.host or ""
        if not ((host in ("", "localhost", "127.0.0.1") or str(host).startswith("/")) and any(w in (url.database or "") for w in ("test", "scratch"))):
            raise RuntimeError("MAVLIT_TEST_PG_URL must be a LOCAL database whose name contains 'test' or 'scratch'; refusing to run")
        cls.engine = create_engine(TEST_URL, poolclass=NullPool)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def build(self, with_role_column: bool):
        """A fresh schema; with_role_column=False builds the 'before' state (migrations, then the role column dropped) with the four emails present."""
        from pipeline import migrations
        with self.engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        migrations.run_migrations(self.engine)
        if not with_role_column:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE creator_profiles DROP COLUMN role"))

    def insert(self, *emails):
        from pipeline.helpers.passwords import hash_password
        pw = hash_password(PASSWORD)
        with self.engine.begin() as conn:
            for e in emails:
                conn.execute(text("INSERT INTO creator_profiles (email, password_hash, is_active) VALUES (:e, :p, true)"), {"e": e, "p": pw})

    def roles(self):
        with self.engine.connect() as conn:
            return dict(conn.execute(text("SELECT email, role FROM creator_profiles")).all())

    def test_the_migration_makes_the_four_accounts_admins_once_and_everyone_else_a_user(self):
        from pipeline import migrations
        self.build(with_role_column=False)
        self.insert(*ADMINS, "Mixed.Case@x.test", "ABC@Gmail.com".replace("ABC", "other"))
        migrations.run_migrations(self.engine)
        roles = self.roles()
        self.assertEqual({e for e, r in roles.items() if r == "admin"}, set(ADMINS))
        self.assertEqual(roles["Mixed.Case@x.test"], "user")
        with self.engine.begin() as conn:   # an admin who is demoted stays demoted after the next start
            conn.execute(text("UPDATE creator_profiles SET role = 'user' WHERE email = 'brett@gmail.com'"))
        migrations.run_migrations(self.engine)
        self.assertEqual(self.roles()["brett@gmail.com"], "user")

    def test_only_an_admin_can_read_the_usage_summary_and_me_shows_the_role(self):
        import api.app as api_app
        import api.attempt_limits as attempt_limits
        from pipeline.db import get_db
        self.build(with_role_column=True)
        self.insert("brett@gmail.com", "plain@example.test")
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET role = 'admin' WHERE email = 'brett@gmail.com'"))
        engine = self.engine
        factory = lambda *a, **k: Session(bind=engine, autoflush=False, expire_on_commit=False)

        def override():
            with factory() as session:
                yield session
        api_app.app.dependency_overrides[get_db] = override
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        for patcher in (mock.patch.object(attempt_limits, "SessionLocal", factory), mock.patch.object(api_app, "SessionLocal", factory)):
            patcher.start()
            self.addCleanup(patcher.stop)

        def client(email):
            c = TestClient(api_app.app, follow_redirects=False)
            self.assertEqual(c.post("/auth/login", json={"email": email, "password": PASSWORD}).status_code, 200)
            return c

        admin, plain = client("brett@gmail.com"), client("plain@example.test")
        self.assertEqual(admin.get("/auth/me").json()["role"], "admin")
        self.assertEqual(plain.get("/auth/me").json()["role"], "user")
        self.assertEqual(TestClient(api_app.app).get("/usage/summary").status_code, 401)
        self.assertEqual(plain.get("/usage/summary").status_code, 403)
        ok = admin.get("/usage/summary?days=7")
        self.assertEqual(ok.status_code, 200, ok.text[:300])
        self.assertEqual(ok.json()["days"], 7)
        for key in ("cards", "daily", "features", "steps", "users", "models", "health", "funnel"):
            self.assertIn(key, ok.json())
        self.assertEqual(admin.get("/usage/summary?days=5").json()["days"], 30)   # a value that is not offered means 30
