"""
Blockers B2-B3 of the 2.6 review: DDL / TRUNCATE scripts refuse to run as the API's restricted role, the deploy runs the migrations
before the restart, and /health/db reads tables through the API's own role. The database parts need a disposable local PostgreSQL
(MAVLIT_TEST_PG_URL, same rules as tests/test_migration_postgres.py); the rest contacts nothing.

Run:  python -m pytest tests/test_owner_guard.py -q
"""

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"
os.environ["JWT_SECRET"] = os.environ.get("JWT_SECRET") or "test-jwt-secret"
os.environ["RUN_MIGRATIONS"] = "0"
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import ProgrammingError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from pipeline import owner_guard  # noqa: E402
from pipeline.owner_guard import RuntimeRoleError, require_owner_role  # noqa: E402

# (file, function that runs the DDL / TRUNCATE) - each must call require_owner_role before its first execute
GUARDED = [
    ("main.py", "ensure_tables"),
    ("pipeline/enrichment_re/content_creator_re.py", "_ensure_partnership_evidence_table"),
    ("gender_check/run_brand_gender_check.py", "_ensure_gender_columns"),
    ("gender_check/run_brand_website_audience_check.py", "_ensure_latest_product_columns"),
    ("scripts/populate_test_niches.py", "populate_test_niches"),
    ("scripts/embed_test_niches.py", "ensure_embedding_column"),
    ("script_creator_niche/embed_creator_niches.py", "ensure_embedding_column"),
]


class _Bind:
    """Stands in for a Session / Connection that answers `SELECT current_user`."""

    def __init__(self, role):
        self.role, self.statements = role, []

    def execute(self, statement, *a, **k):
        self.statements.append(str(statement))
        return mock.Mock(scalar=lambda: self.role)


class _Engine:
    def __init__(self, role):
        self.bind = _Bind(role)

    def connect(self):
        bind = self.bind
        return mock.MagicMock(__enter__=lambda s: bind, __exit__=lambda *a: False)


class Guard(unittest.TestCase):
    def test_refuses_the_runtime_role_with_a_clear_message(self):
        for bind in (_Bind("mavlit_app"), _Engine("mavlit_app")):
            with self.subTest(type(bind).__name__), self.assertRaises(RuntimeRoleError) as ctx:
                require_owner_role(bind, "some_script.py")
            message = str(ctx.exception)
            self.assertIn("some_script.py", message)
            self.assertIn("'mavlit_app'", message)
            self.assertIn("/root/.mavlit-batch.env", message)
            self.assertIn("set -a; . /root/.mavlit-batch.env; set +a", message)
            self.assertNotIn("postgresql", message)   # no URL

    def test_allows_the_owner_and_any_other_role_and_only_reads(self):
        for role in ("mavlit_user", "postgres", "dev"):
            bind = _Bind(role)
            require_owner_role(bind)
            self.assertEqual(bind.statements, ["SELECT current_user"])

    def test_the_runtime_role_name_is_configurable(self):
        with mock.patch.dict(os.environ, {"MAVLIT_RUNTIME_ROLE": "api_runtime"}):
            require_owner_role(_Bind("mavlit_app"))   # no longer the runtime role
            with self.assertRaises(RuntimeRoleError):
                require_owner_role(_Bind("api_runtime"))
        with mock.patch.dict(os.environ, {"MAVLIT_RUNTIME_ROLE": "  "}):
            self.assertEqual(owner_guard.runtime_role(), "mavlit_app")


class EveryDdlScriptIsGuarded(unittest.TestCase):
    def test_the_guard_is_the_first_database_call_of_each_function(self):
        for rel, function in GUARDED:
            with self.subTest(f"{rel}:{function}"):
                tree = ast.parse((ROOT / rel).read_text())
                node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
                calls = [(c.lineno, getattr(c.func, "id", getattr(c.func, "attr", ""))) for c in ast.walk(node) if isinstance(c, ast.Call)]
                guard_line = min(line for line, name in calls if name == "require_owner_role")
                risky = [line for line, name in calls if name in ("execute", "create_all", "begin", "connect", "insert")]
                self.assertTrue(risky, "the function no longer runs DDL: update GUARDED")
                self.assertLess(guard_line, min(risky))

    def test_populate_script_main_guards_before_create_all(self):
        source = (ROOT / "scripts/populate_test_niches.py").read_text()
        main = source[source.index('if __name__ == "__main__":'):]
        self.assertLess(main.index("require_owner_role"), main.index("create_all"))

    def test_scripts_stop_before_any_ddl_as_the_runtime_role(self):
        import main as main_module
        engine = mock.MagicMock()
        with mock.patch.object(main_module, "engine", engine), \
                mock.patch.object(main_module, "require_owner_role", side_effect=RuntimeRoleError("no")), \
                mock.patch.object(main_module.Base.metadata, "create_all") as create_all:
            with self.assertRaises(RuntimeRoleError):
                main_module.ensure_tables()
        create_all.assert_not_called()
        engine.connect.assert_not_called()


class DeployWiring(unittest.TestCase):
    def test_the_workflow_runs_the_deploy_with_migrations_and_holds_no_secret(self):
        text_ = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertIn("DEPLOY_RUN_MIGRATIONS=1 bash /opt/MAVLIT-enrichment/scripts/deploy.sh", text_)
        self.assertEqual(text_.count("secrets."), 2)   # only the ssh key and host, as before
        self.assertNotIn("DATABASE_URL", text_)

    def test_deploy_script_probes_both_urls(self):
        script = (ROOT / "scripts/deploy.sh").read_text()
        self.assertIn("http://127.0.0.1:8000/brand-niches http://127.0.0.1:8000/health/db", script)
        self.assertIn("all(urllib.request.urlopen(u, timeout=5).status == 200 for u in sys.argv[1:])", script)


class HealthDbRoute(unittest.TestCase):
    def _client(self, session):
        from fastapi.testclient import TestClient
        import api.app as api_app
        from pipeline.db import get_db
        api_app.app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        return TestClient(api_app.app, raise_server_exceptions=False)

    def test_reads_both_tables_and_returns_no_data(self):
        session = mock.MagicMock()
        r = self._client(session).get("/health/db")
        self.assertEqual((r.status_code, r.json()), (200, {"status": "ok"}))
        sql = " ".join(str(c.args[0]) for c in session.execute.call_args_list)
        self.assertIn("creator_profiles", sql)
        self.assertIn("brands_raw", sql)

    def test_a_permission_error_is_a_generic_500_without_sql(self):
        from sqlalchemy.exc import ProgrammingError as PE
        orig = type("Orig", (Exception,), {"pgcode": "42501"})('permission denied for table creator_profiles')
        session = mock.MagicMock()
        session.execute.side_effect = PE("SELECT 1 FROM creator_profiles LIMIT 1", {}, orig)
        r = self._client(session).get("/health/db")
        self.assertEqual((r.status_code, r.json()), (500, {"detail": "Internal server error"}))
        self.assertNotIn("creator_profiles", r.text)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class RealPostgres(unittest.TestCase):
    ROLE = "mavlit_probe_role_xx"

    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        host = url.query.get("host") or url.host or ""
        if not ((host in ("", "localhost", "127.0.0.1") or str(host).startswith("/")) and any(w in (url.database or "") for w in ("test", "scratch"))):
            raise RuntimeError("MAVLIT_TEST_PG_URL must be a LOCAL database whose name contains 'test' or 'scratch'; refusing to run")
        from pipeline import migrations
        cls.admin = create_engine(TEST_URL, poolclass=NullPool)
        with cls.admin.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        migrations.run_migrations(cls.admin)
        with cls.admin.begin() as conn:
            conn.execute(text(f"DROP ROLE IF EXISTS {cls.ROLE}"))
            conn.execute(text(f"CREATE ROLE {cls.ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"))
            conn.execute(text(f"GRANT CONNECT ON DATABASE {url.database} TO {cls.ROLE}"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {cls.ROLE}"))
        cls.role_url = url.set(username=cls.ROLE)

    @classmethod
    def tearDownClass(cls):
        with cls.admin.begin() as conn:
            conn.execute(text(f"DROP OWNED BY {cls.ROLE}"))
            conn.execute(text(f"DROP ROLE IF EXISTS {cls.ROLE}"))
        cls.admin.dispose()

    def test_guard_reads_the_real_current_user(self):
        engine = create_engine(TEST_URL, poolclass=NullPool)
        try:
            with engine.connect() as conn:
                current = conn.execute(text("SELECT current_user")).scalar()
            with mock.patch.dict(os.environ, {"MAVLIT_RUNTIME_ROLE": current}):
                with self.assertRaises(RuntimeRoleError):
                    require_owner_role(engine)
                with Session(engine) as session, self.assertRaises(RuntimeRoleError):
                    require_owner_role(session)
            with mock.patch.dict(os.environ, {"MAVLIT_RUNTIME_ROLE": "mavlit_app"}):
                require_owner_role(engine)   # connected as someone else: allowed
        finally:
            engine.dispose()

    def test_health_db_catches_a_missing_grant(self):
        from fastapi.testclient import TestClient
        import api.app as api_app
        from pipeline.db import get_db
        engine = create_engine(self.role_url, poolclass=NullPool)
        self.addCleanup(engine.dispose)

        def override():
            with Session(engine) as session:
                yield session

        api_app.app.dependency_overrides[get_db] = override
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        client = TestClient(api_app.app, raise_server_exceptions=False)
        with self.admin.begin() as conn:
            conn.execute(text(f"REVOKE ALL ON creator_profiles, brands_raw FROM {self.ROLE}"))
        denied = client.get("/health/db")
        self.assertEqual(denied.status_code, 500, denied.text)
        self.assertNotIn("permission denied", denied.text)
        with self.admin.begin() as conn:
            conn.execute(text(f"GRANT SELECT ON creator_profiles, brands_raw TO {self.ROLE}"))
        self.assertEqual(client.get("/health/db").json(), {"status": "ok"})

    def test_a_restricted_role_really_cannot_run_the_guarded_ddl(self):
        engine = create_engine(self.role_url, poolclass=NullPool)
        self.addCleanup(engine.dispose)
        with engine.connect() as conn, self.assertRaises(ProgrammingError):
            conn.execute(text("ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS refferls BOOLEAN"))


if __name__ == "__main__":
    unittest.main()
