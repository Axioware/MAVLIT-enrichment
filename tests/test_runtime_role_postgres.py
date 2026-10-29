"""
REQUIRES A DISPOSABLE LOCAL POSTGRESQL DATABASE (pgvector available, superuser URL, trust authentication for the two test
roles). Skipped unless MAVLIT_TEST_PG_URL is set. It DROPS AND RECREATES the public schema of that database and creates the
roles mavlit_test_owner (stands for mavlit_user) and mavlit_test_app (stands for mavlit_app), so it refuses to run unless
the database is local and its name contains "test" or "scratch". DATABASE_URL is forced to an unreachable address.

It proves, against a real PostgreSQL, that after migrations run by the owner role:
  - the runtime role has exactly the planned privileges on exactly the planned objects (no TRUNCATE / REFERENCES / TRIGGER,
    nothing on backup tables, other schemas or other databases), uses the sequences, and can read the recreated view;
  - the runtime role cannot create, alter, drop, truncate, change ownership, or run the migrations;
  - objects created by later migrations get the grants, backup tables created later do not;
  - the generated SQL script gives the same result as the migration's grant step, and refuses wrong databases / users;
  - the real FastAPI routes and every SQLAdmin list page work as the runtime role (tests/_runtime_smoke.py).

Setup used by the author (any equivalent works):
    initdb -D /tmp/mpgdata -A trust -U postgres
    pg_ctl -D /tmp/mpgdata -o "-c listen_addresses='' -c unix_socket_directories=/tmp/mpg -c port=55432" start
    createdb -h /tmp/mpg -p 55432 -U postgres mavlit_test_scratch
    MAVLIT_TEST_PG_URL='postgresql+psycopg2://postgres@/mavlit_test_scratch?host=/tmp/mpg&port=55432' python -m pytest tests/test_runtime_role_postgres.py -q
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")
ROOT = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"
os.environ["RUN_MIGRATIONS"] = ""
os.environ.pop("MIGRATION_DATABASE_URL", None)
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import DBAPIError, ProgrammingError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

OWNER, APP, OTHER, MIDDLE = "mavlit_test_owner", "mavlit_test_app", "mavlit_test_other", "mavlit_test_middle"
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _assert_disposable(raw: str) -> None:
    url = make_url(raw)
    host = url.query.get("host") or url.host or ""
    local = host in ("", "localhost", "127.0.0.1") or str(host).startswith("/")
    if not (local and any(word in (url.database or "") for word in ("test", "scratch"))):
        raise RuntimeError("MAVLIT_TEST_PG_URL must be a LOCAL database whose name contains 'test' or 'scratch'; refusing to run")


def _as(raw: str, username: str, database: str | None = None):
    url = make_url(raw).set(username=username, password=None)
    if database:
        url = url.set(database=database)
    return create_engine(url, poolclass=NullPool)


def _psql() -> str | None:
    return shutil.which("psql") or next(iter(sorted(glob.glob("/usr/lib/postgresql/*/bin/psql"))[::-1]), None)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class RuntimeRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _assert_disposable(TEST_URL)
        from pipeline import db_grants, migrations
        cls.db_grants, cls.migrations = db_grants, migrations
        cls.url = make_url(TEST_URL)
        cls.dbname = cls.url.database
        cls.admin = create_engine(TEST_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        with cls.admin.connect() as c:
            c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            c.execute(text("DROP SCHEMA IF EXISTS other_schema CASCADE"))
            c.execute(text("CREATE SCHEMA public"))
            c.execute(text(f"DROP DATABASE IF EXISTS {cls.dbname}_other"))
            for role in (APP, OTHER, MIDDLE, OWNER):
                if c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first():
                    c.execute(text(f"DROP OWNED BY {role}"))
                    c.execute(text(f"DROP ROLE {role}"))
            c.execute(text(f"CREATE ROLE {OWNER} LOGIN"))
            c.execute(text(f"CREATE ROLE {APP} LOGIN"))
            c.execute(text(f"CREATE ROLE {OTHER} LOGIN"))     # an unrelated role whose grants must never be touched
            c.execute(text(f"CREATE ROLE {MIDDLE} NOLOGIN"))  # for transitive membership
            c.execute(text(f"ALTER DATABASE {cls.dbname} OWNER TO {OWNER}"))
            c.execute(text(f"ALTER SCHEMA public OWNER TO {OWNER}"))
            c.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))     # PostgreSQL 15+ defaults, as in production
            c.execute(text("GRANT USAGE ON SCHEMA public TO PUBLIC"))
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))       # needs a superuser; the migrations only run IF NOT EXISTS
        cls.owner_engine, cls.app_engine = _as(TEST_URL, OWNER), _as(TEST_URL, APP)
        migrations.run_migrations(cls.owner_engine, grant_role=APP)       # as the non-superuser owner, with the grant step
        cls.model_tables = sorted(db_grants.CODE)

    @classmethod
    def tearDownClass(cls):
        for engine in (cls.owner_engine, cls.app_engine):
            engine.dispose()
        with cls.admin.connect() as c:
            c.execute(text("DROP SCHEMA IF EXISTS other_schema CASCADE"))
            c.execute(text(f"DROP DATABASE IF EXISTS {cls.dbname}_other"))
            c.execute(text(f"ALTER DATABASE {cls.dbname} OWNER TO CURRENT_USER"))
            c.execute(text("ALTER SCHEMA public OWNER TO CURRENT_USER"))
            for role in (APP, OTHER, MIDDLE, OWNER):
                c.execute(text(f"DROP OWNED BY {role}"))
                c.execute(text(f"DROP ROLE {role}"))
        cls.admin.dispose()

    def setUp(self):
        self._reset()
        self.addCleanup(self._reset)

    def _reset(self):
        """Back to the baseline between tests: the plan applied, nothing else around."""
        with self.admin.connect() as c:
            for role in (OWNER, MIDDLE, "pg_read_all_data"):
                if c.execute(text("SELECT pg_has_role(:a, :r, 'MEMBER')"), {"a": APP, "r": role}).scalar():
                    c.execute(text(f"REVOKE {role} FROM {APP}"))
            c.execute(text(f"REVOKE {OWNER} FROM {MIDDLE}"))
            c.execute(text(f"ALTER ROLE {APP} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"))
            c.execute(text(f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA public REVOKE ALL ON TABLES FROM {APP}"))
            c.execute(text("DROP SCHEMA IF EXISTS other_schema CASCADE"))
            for table in ("bak_pre", "bak_cols", "bak_public", "owned_by_app", "stray_view"):
                c.execute(text(f"DROP TABLE IF EXISTS public.{table} CASCADE"))
            c.execute(text("DROP VIEW IF EXISTS public.stray_view"))
            c.execute(text("DROP SEQUENCE IF EXISTS public.stray_seq"))
            c.execute(text(f"REVOKE ALL ON DATABASE {self.dbname} FROM PUBLIC"))
            c.execute(text(f"GRANT CONNECT, TEMPORARY ON DATABASE {self.dbname} TO PUBLIC"))   # PostgreSQL's defaults
        with self.owner_engine.begin() as c:
            self.db_grants.apply_grants(c, APP)

    # ---- helpers --------------------------------------------------------------------------------------------------
    def _snapshot(self) -> dict:
        """{object: sorted privileges} the runtime role holds on every table, view and sequence of the public schema."""
        with self.owner_engine.connect() as c:
            out = {}
            for name, kind in c.execute(text("SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'S')")):
                if kind == "S":
                    held = ["USAGE"] if c.execute(text("SELECT has_sequence_privilege(:r, :o, 'USAGE')"), {"r": APP, "o": f"public.{name}"}).scalar() else []
                    held += [p for p in ("SELECT", "UPDATE") if c.execute(text(f"SELECT has_sequence_privilege(:r, :o, '{p}')"), {"r": APP, "o": f"public.{name}"}).scalar()]
                else:
                    held = [p for p in PRIVILEGES if c.execute(text(f"SELECT has_table_privilege(:r, :o, '{p}')"), {"r": APP, "o": f"public.{name}"}).scalar()]
                if held:
                    out[name] = sorted(held)
            return out

    def _expected_tables(self) -> dict:
        names = {"S": "SELECT", "I": "INSERT", "U": "UPDATE", "D": "DELETE"}
        expected = {t: sorted(names[c] for c in letters) for t, letters in self.db_grants.table_privileges().items()}
        expected.update({v: sorted(names[c] for c in letters) for v, letters in self.db_grants.VIEWS.items()})
        return expected

    def _owner_sequences(self):
        with self.owner_engine.connect() as c:
            return self.db_grants.planned_sequences(c)

    def _scalar(self, sql, **params):
        with self.owner_engine.connect() as c:
            return c.execute(text(sql), params).scalar()

    def _acl_state(self) -> tuple:
        """Every ACL in the database, to prove that a refused run changed nothing."""
        with self.admin.connect() as c:
            return (
                sorted(tuple(r) for r in c.execute(text("SELECT n.nspname, c.relname, c.relacl::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_toast'"))),
                sorted(tuple(r) for r in c.execute(text("SELECT nspname, nspacl::text FROM pg_namespace WHERE nspname NOT IN ('pg_catalog', 'information_schema') AND nspname !~ '^pg_'"))),
                c.execute(text("SELECT datacl::text FROM pg_database WHERE datname = current_database()")).scalar(),
            )

    def _direct_grants(self, role: str) -> list[str]:
        """Every direct ACL entry that names `role`, anywhere in the database."""
        with self.admin.connect() as c:
            return [r[0] for r in c.execute(text("""
                SELECT what FROM (
                    SELECT 'relation ' || n.nspname || '.' || c.relname AS what FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = :r)
                    UNION SELECT 'column of ' || n.nspname || '.' || c.relname FROM pg_attribute at JOIN pg_class c ON c.oid = at.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(at.attacl) a WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = :r)
                    UNION SELECT 'schema ' || nspname FROM pg_namespace, aclexplode(nspacl) a WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = :r)
                    UNION SELECT 'database ' || datname FROM pg_database, aclexplode(datacl) a WHERE datname = current_database() AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = :r)
                ) x ORDER BY 1"""), {"r": role})]

    def _psql_run(self, username: str, sql: str, expect_ok: bool = True):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as fh:
            fh.write(sql)
        self.addCleanup(os.unlink, fh.name)
        host, port = self.url.query.get("host") or self.url.host, str(self.url.query.get("port") or self.url.port or 5432)
        result = subprocess.run([_psql(), "-X", "-q", "-h", host, "-p", port, "-U", username, "-d", self.dbname, "-f", fh.name], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode == 0, expect_ok, result.stderr)
        return result

    def _apply(self, mode: str, expect_ok: bool = True):
        """Apply the plan the way the migration step (python) or the reviewed script (script) does."""
        if mode == "script":
            return self._psql_run(OWNER, self.db_grants.sql_script(APP, OWNER, self.dbname), expect_ok)
        try:
            with self.owner_engine.begin() as c:
                self.db_grants.apply_grants(c, APP)
        except self.db_grants.GrantAuditError as exc:
            self.assertFalse(expect_ok, str(exc))
            return exc
        self.assertTrue(expect_ok, "expected the grant step to be refused")

    def _denied(self, sql: str, **params):
        with self.app_engine.connect() as c:
            with self.assertRaises(DBAPIError, msg=sql) as ctx:
                c.execute(text(sql), params)
            self.assertEqual(getattr(ctx.exception.orig, "pgcode", None), "42501", f"{sql}: {ctx.exception.orig}")   # insufficient_privilege

    # ---- the grants -----------------------------------------------------------------------------------------------
    def test_runtime_role_holds_exactly_the_planned_privileges(self):
        snapshot = self._snapshot()
        tables_and_views = {name: privs for name, privs in snapshot.items() if not name.endswith("_seq")}
        self.assertEqual(tables_and_views, self._expected_tables())
        for name, privs in tables_and_views.items():
            for forbidden in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                self.assertNotIn(forbidden, privs, name)

    def test_all_30_model_tables_and_the_view_exist_and_are_covered(self):
        from pipeline.db import Base
        with self.owner_engine.connect() as c:
            existing = {r[0] for r in c.execute(text("SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND relkind IN ('r', 'v')"))}
        self.assertTrue({t.name for t in Base.metadata.sorted_tables} <= existing)
        self.assertIn("creator_llm_cost", existing)
        self.assertTrue(set(self._expected_tables()) <= existing)

    def test_sequences_are_usable_and_nothing_more(self):
        sequences = self._owner_sequences()
        self.assertGreaterEqual(len(sequences), 20)
        snapshot = self._snapshot()
        for _, seq in sequences:
            self.assertEqual(snapshot.get(seq), ["USAGE"], f"{seq}: USAGE only (no setval)")
        with self.app_engine.begin() as c:
            for schema, seq in sequences:
                self.assertIsNotNone(c.execute(text(f'SELECT nextval(\'"{schema}"."{seq}"\')')).scalar())
        self._denied("SELECT setval('prompts_id_seq', 1000000)")

    def test_no_default_privileges_and_no_owned_objects(self):
        with self.owner_engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT count(*) FROM pg_default_acl")).scalar(), 0, "grants are explicit; default privileges would expose backup tables")
            self.assertEqual(c.execute(text("SELECT count(*) FROM pg_class WHERE relowner = (SELECT oid FROM pg_roles WHERE rolname = :r)"), {"r": APP}).scalar(), 0)
            role = c.execute(text("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = :r"), {"r": APP}).one()
            self.assertEqual(tuple(role), (False, False, False, False, False))
            self.assertFalse(c.execute(text("SELECT pg_has_role(:a, :o, 'MEMBER')"), {"a": APP, "o": OWNER}).scalar())

    # ---- the view -------------------------------------------------------------------------------------------------
    def test_the_recreated_view_keeps_its_grants_with_or_without_the_grant_step(self):
        with self.owner_engine.begin() as c:
            c.execute(text(f"GRANT SELECT ON creator_llm_cost TO {OTHER}"))   # an unrelated role that uses the view
        self.addCleanup(self._revoke_view_from_other)
        for grant_role in (APP, None, APP):
            self.migrations.run_migrations(self.owner_engine, grant_role=grant_role)      # DROP VIEW + CREATE VIEW every time
            with self.app_engine.connect() as c:
                c.execute(text("SELECT * FROM creator_llm_cost")).fetchall()               # never lost, even without --grant-role
            with self.owner_engine.connect() as c:
                self.assertTrue(c.execute(text("SELECT has_table_privilege(:o, 'creator_llm_cost', 'SELECT')"), {"o": OTHER}).scalar(), "other roles' view grants survive too")
        self.assertEqual({k: v for k, v in self._snapshot().items() if k == "creator_llm_cost"}, {"creator_llm_cost": ["SELECT"]})

    def _revoke_view_from_other(self):
        with self.owner_engine.begin() as c:
            c.execute(text(f"REVOKE ALL ON creator_llm_cost FROM {OTHER}"))

    def test_the_view_is_read_only_for_the_runtime_role_after_every_recreation(self):
        self.migrations.run_migrations(self.owner_engine)
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            self.assertFalse(self._scalar(f"SELECT has_table_privilege('{APP}', 'creator_llm_cost', '{privilege}')"), privilege)

    def test_reports_the_server_version_and_can_insist_on_a_major(self):
        with self.admin.connect() as c:
            version, number = c.execute(text("SELECT version(), current_setting('server_version_num')::int")).one()
        print(f"\n  server under test: {version.split(',')[0]} (server_version_num {number})")
        expected = os.environ.get("MAVLIT_EXPECT_PG_MAJOR")
        if expected:
            self.assertEqual(number // 10000, int(expected), f"these tests were meant to run on PostgreSQL {expected}")

    def test_the_view_cannot_be_changed_by_the_runtime_role(self):
        self._denied("DROP VIEW creator_llm_cost")
        self._denied("CREATE OR REPLACE VIEW creator_llm_cost AS SELECT 1 AS x")

    # ---- what the runtime role can and cannot do --------------------------------------------------------------------
    def test_crud_on_application_tables(self):
        from pipeline.db import BrandRaw, CreatorProfile, LlmUsage, SavedBrand
        with Session(self.app_engine) as db:
            brand = BrandRaw(name="grant test brand")
            profile = CreatorProfile(email=f"{time.time_ns()}@example.com", full_name="G", creator_handle="g")
            db.add_all([brand, profile])
            db.commit()                                                       # ids come from the sequences
            self.assertIsNotNone(brand.id)
            brand.name = "renamed"
            db.add(SavedBrand(creator_profile_id=profile.id, brand_raw_id=brand.id))
            db.add(LlmUsage(model="m", endpoint="e", prompt_tokens=1, completion_tokens=1))   # INSERT .. RETURNING needs SELECT
            db.commit()
            db.delete(db.get(SavedBrand, (profile.id, brand.id)))
            db.commit()
            db.delete(brand)
            db.commit()
        self._denied("DELETE FROM llm_usage")             # usage rows are insert-only
        self._denied("UPDATE llm_usage SET model = 'x'")
        self._denied("UPDATE apify_usage SET handle = 'x'")

    def test_runtime_role_cannot_change_the_schema(self):
        for sql in (
            "CREATE TABLE public.sneaky (id int)",
            "CREATE SCHEMA sneaky_schema",
            "ALTER TABLE brands_raw ADD COLUMN sneaky text",
            "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS country TEXT",      # even a no-op "IF NOT EXISTS" needs ownership
            "ALTER TABLE brands_raw OWNER TO mavlit_test_app",
            "ALTER TABLE brands_raw RENAME TO sneaky",
            "ALTER TABLE brands_raw DROP COLUMN IF EXISTS country",
            "DROP TABLE brands_raw",
            "DROP TABLE IF EXISTS prompts",
            "TRUNCATE brands_raw",
            "TRUNCATE prompts, pitches",
            "CREATE INDEX sneaky_idx ON brands_raw (id)",
            "CREATE TRIGGER sneaky AFTER INSERT ON brands_raw FOR EACH ROW EXECUTE FUNCTION pg_catalog.suppress_redundant_updates_trigger()",
            "ALTER TABLE pitches ADD CONSTRAINT sneaky_fk FOREIGN KEY (id) REFERENCES brands_raw (id)",
            "ALTER SCHEMA public OWNER TO mavlit_test_app",
            "DROP SCHEMA public CASCADE",
        ):
            self._denied(sql)

    def test_runtime_role_cannot_pass_privileges_on(self):
        # PostgreSQL only WARNS ("no privileges were granted") when a role without grant option tries this; what matters is the effect.
        with self.owner_engine.connect() as c:
            before = c.execute(text("SELECT relacl::text FROM pg_class WHERE relname = 'brands_raw'")).scalar()
        with self.app_engine.begin() as c:
            c.execute(text("GRANT SELECT ON brands_raw TO PUBLIC"))
            c.execute(text("GRANT ALL ON brands_raw TO mavlit_test_owner"))
        with self.owner_engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT relacl::text FROM pg_class WHERE relname = 'brands_raw'")).scalar(), before)

    def test_runtime_role_cannot_run_the_migrations(self):
        with self.assertRaises(DBAPIError) as ctx:
            self.migrations.run_migrations(self.app_engine)
        self.assertEqual(getattr(ctx.exception.orig, "pgcode", None), "42501", str(ctx.exception.orig))
        with self.owner_engine.connect() as c:   # and it changed nothing that matters
            self.assertEqual(c.execute(text("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'sneaky'")).scalar(), 0)

    # ---- later migrations and the objects that must stay hidden ---------------------------------------------------------
    def test_tables_created_by_a_later_migration_get_the_grants_and_backups_do_not(self):
        with self.owner_engine.begin() as c:
            c.execute(text("CREATE TABLE later_migration_table (id serial PRIMARY KEY, note text)"))
            c.execute(text("CREATE TABLE bak_20261101_prompts AS SELECT * FROM prompts"))
            c.execute(text("CREATE TABLE bak_with_serial (id serial PRIMARY KEY)"))
            c.execute(text("CREATE TABLE llm_model_test_rankings_copy (id serial PRIMARY KEY)"))
        self.addCleanup(self._drop_later_objects)
        with mock.patch.dict(self.db_grants.CODE, {"later_migration_table": "SIU"}), mock.patch.dict(self.db_grants.ADMIN, {"later_migration_table": None}):
            with self.assertLogs("pipeline.migrations", level="WARNING") as logs:
                self.migrations.run_migrations(self.owner_engine, grant_role=APP)
        self.assertIn("bak_20261101_prompts", "\n".join(logs.output), "unclassified tables are reported")
        snapshot = self._snapshot()
        self.assertEqual(snapshot["later_migration_table"], ["INSERT", "SELECT", "UPDATE"])
        self.assertEqual(snapshot["later_migration_table_id_seq"], ["USAGE"])
        for hidden in ("bak_20261101_prompts", "bak_with_serial", "bak_with_serial_id_seq", "llm_model_test_rankings_copy"):
            self.assertNotIn(hidden, snapshot, f"{hidden} must stay inaccessible")
        self._denied("SELECT * FROM bak_20261101_prompts")
        self._denied("SELECT * FROM bak_with_serial")

    def _drop_later_objects(self):
        with self.owner_engine.begin() as c:
            for name in ("later_migration_table", "bak_20261101_prompts", "bak_with_serial", "llm_model_test_rankings_copy"):
                c.execute(text(f"DROP TABLE IF EXISTS {name}"))

    def test_grants_are_scoped_to_this_database_and_schema(self):
        with self.admin.connect() as c:
            c.execute(text(f"CREATE DATABASE {self.dbname}_other OWNER {OWNER}"))
        with self.owner_engine.begin() as c:
            c.execute(text("CREATE SCHEMA other_schema"))
            c.execute(text("CREATE TABLE other_schema.secrets (id int)"))
        self.addCleanup(lambda: self.owner_engine.dispose())
        self._denied("SELECT * FROM other_schema.secrets")
        other = _as(TEST_URL, OWNER, f"{self.dbname}_other")
        with other.begin() as c:
            c.execute(text("CREATE TABLE elsewhere (id int)"))
        other_app = _as(TEST_URL, APP, f"{self.dbname}_other")
        with other_app.connect() as c:
            with self.assertRaises(ProgrammingError):
                c.execute(text("SELECT * FROM elsewhere"))
        for engine in (other, other_app):
            engine.dispose()
        with self.owner_engine.connect() as c:
            self.assertFalse(c.execute(text("SELECT has_database_privilege(:r, :d, 'CREATE')"), {"r": APP, "d": f"{self.dbname}_other"}).scalar())
            self.assertFalse(c.execute(text("SELECT has_schema_privilege(:r, 'public', 'CREATE')"), {"r": APP}).scalar())
            self.assertFalse(c.execute(text("SELECT has_schema_privilege(:r, 'other_schema', 'USAGE')"), {"r": APP}).scalar())

    # ---- the reviewable script ---------------------------------------------------------------------------------------
    @unittest.skipUnless(_psql(), "psql is not installed")
    def test_the_sql_script_gives_the_same_result_as_the_grant_step(self):
        expected = self._snapshot()
        with self.owner_engine.begin() as c:
            for table in [*self.db_grants.CODE, *self.db_grants.VIEWS]:
                c.execute(text(f'REVOKE ALL ON TABLE "{table}" FROM {APP}'))
            for _, seq in self._owner_sequences():
                c.execute(text(f'REVOKE ALL ON SEQUENCE "{seq}" FROM {APP}'))
        self.assertEqual(self._snapshot(), {})
        script = self.db_grants.sql_script(APP, OWNER, self.dbname)
        self._psql_run(OWNER, script)
        self.assertEqual(self._snapshot(), expected)
        self._psql_run(OWNER, script)                        # repeatable
        self.assertEqual(self._snapshot(), expected)
        self.assertIn("run this script as", self._psql_run(APP, script, expect_ok=False).stderr)                           # not as the runtime role
        self.assertIn("this script is for database", self._psql_run(OWNER, self.db_grants.sql_script(APP, OWNER, "some_other_db"), expect_ok=False).stderr)
        self.assertEqual(self._snapshot(), expected, "a refused script must change nothing")

    # ---- pre-existing grants, memberships and other inherited access ----------------------------------------------------------
    def _make_stray_state(self):
        """Direct grants to the runtime role outside the plan, plus an unrelated role's grants that must survive."""
        with self.owner_engine.begin() as c:
            c.execute(text("CREATE TABLE bak_pre (id serial PRIMARY KEY, note text)"))
            c.execute(text("CREATE TABLE bak_cols (id int, secret text)"))
            c.execute(text("CREATE VIEW stray_view AS SELECT id FROM bak_cols"))
            c.execute(text("CREATE SEQUENCE stray_seq"))
            c.execute(text("CREATE SCHEMA other_schema"))
            c.execute(text("CREATE TABLE other_schema.secrets (id int)"))
            for sql in (f"GRANT SELECT, UPDATE ON bak_pre TO {APP}", f"GRANT USAGE ON SEQUENCE bak_pre_id_seq TO {APP}", f"GRANT SELECT (secret) ON bak_cols TO {APP}",
                        f"GRANT SELECT ON stray_view TO {APP}", f"GRANT USAGE, UPDATE ON SEQUENCE stray_seq TO {APP}",
                        f"GRANT USAGE ON SCHEMA other_schema TO {APP}", f"GRANT SELECT ON other_schema.secrets TO {APP}",
                        f"GRANT TRUNCATE, REFERENCES, TRIGGER, DELETE ON llm_usage TO {APP}",          # extra privileges on a LISTED table
                        f"GRANT SELECT ON bak_pre TO {OTHER}", f"GRANT SELECT, INSERT ON prompts TO {OTHER}"):
                c.execute(text(sql))
        with self.admin.connect() as c:
            c.execute(text(f"GRANT CREATE, TEMPORARY ON DATABASE {self.dbname} TO {APP}"))

    def _check_stray_state_cleaned(self):
        self.assertEqual({k: v for k, v in self._snapshot().items() if not k.endswith("_seq")}, self._expected_tables())
        self.assertEqual(self._direct_grants(APP).count("schema other_schema"), 0)
        with self.owner_engine.connect() as c:
            for sql in ("SELECT has_table_privilege(:a, 'bak_pre', 'SELECT')", "SELECT has_any_column_privilege(:a, 'bak_cols', 'SELECT')",
                        "SELECT has_table_privilege(:a, 'stray_view', 'SELECT')", "SELECT has_sequence_privilege(:a, 'stray_seq', 'USAGE')",
                        "SELECT has_sequence_privilege(:a, 'bak_pre_id_seq', 'USAGE')", "SELECT has_table_privilege(:a, 'other_schema.secrets', 'SELECT')",
                        "SELECT has_schema_privilege(:a, 'other_schema', 'USAGE')", "SELECT has_table_privilege(:a, 'llm_usage', 'DELETE')",
                        "SELECT has_table_privilege(:a, 'llm_usage', 'TRUNCATE')", "SELECT has_database_privilege(:a, current_database(), 'CREATE')"):
                self.assertFalse(c.execute(text(sql), {"a": APP}).scalar(), sql)
            # the unrelated role keeps exactly what it had
            self.assertTrue(c.execute(text("SELECT has_table_privilege(:o, 'bak_pre', 'SELECT')"), {"o": OTHER}).scalar())
            self.assertTrue(c.execute(text("SELECT has_table_privilege(:o, 'prompts', 'SELECT') AND has_table_privilege(:o, 'prompts', 'INSERT')"), {"o": OTHER}).scalar())

    def test_the_runtime_roles_own_stray_grants_are_revoked_and_other_roles_are_not_touched(self):
        for mode in ("python", "script") if _psql() else ("python",):
            self._reset()
            self._make_stray_state()
            self._apply(mode)
            self._check_stray_state_cleaned()
            self._apply(mode)                                              # repeatable
            self._check_stray_state_cleaned()

    def test_membership_in_a_privileged_role_is_refused_with_instructions(self):
        cases = {
            "direct membership in the owner role": [f"GRANT {OWNER} TO {APP}"],
            "transitive membership": [f"GRANT {OWNER} TO {MIDDLE}", f"GRANT {MIDDLE} TO {APP}"],
            "a predefined role": [f"GRANT pg_read_all_data TO {APP}"],
        }
        for label, statements in cases.items():
            for mode in ("python", "script") if _psql() else ("python",):
                self._reset()
                with self.admin.connect() as c:
                    for sql in statements:
                        c.execute(text(sql))
                before = self._acl_state()
                outcome = self._apply(mode, expect_ok=False)
                text_out = str(outcome) if mode == "python" else outcome.stderr
                self.assertIn("member of the role", text_out, label)
                self.assertIn(f"REVOKE", text_out, label)
                self.assertIn("planned access", text_out, label)
                self.assertEqual(self._acl_state(), before, f"{label} ({mode}): a refused run must change nothing")
        # once the membership is removed the same run passes
        self._reset()
        self._apply("python")

    def test_the_migration_grant_step_surfaces_the_refusal(self):
        with self.admin.connect() as c:
            c.execute(text(f"GRANT {OWNER} TO {APP}"))
        with self.assertRaises(self.db_grants.GrantAuditError) as ctx:
            self.migrations.run_migrations(self.owner_engine, grant_role=APP)
        self.assertIn(f"member of the role {OWNER}", str(ctx.exception))

    def test_public_grants_default_privileges_ownership_and_attributes_are_refused_not_silently_changed(self):
        def expect_refused(setup, undo, needle):
            self._reset()
            with self.admin.connect() as c:
                c.execute(text(setup))
            before = self._acl_state()
            refusal = self._apply("python", expect_ok=False)
            self.assertIn(needle, str(refusal), setup)
            self.assertEqual(self._acl_state(), before, f"{setup}: the refusal must not change anything")
            with self.admin.connect() as c:
                c.execute(text(undo))
            self._apply("python")                                          # fixed by the documented step: now accepted

        expect_refused(f"CREATE TABLE bak_public (id int); ALTER TABLE bak_public OWNER TO {OWNER}; GRANT SELECT ON bak_public TO PUBLIC",
                       "REVOKE ALL ON bak_public FROM PUBLIC", "PUBLIC")
        expect_refused(f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA public GRANT SELECT ON TABLES TO {APP}",
                       f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA public REVOKE ALL ON TABLES FROM {APP}", "default-privilege entry")
        expect_refused(f"ALTER ROLE {APP} CREATEDB", f"ALTER ROLE {APP} NOCREATEDB", "CREATEDB")
        expect_refused(f"GRANT CREATE ON SCHEMA public TO PUBLIC", "REVOKE CREATE ON SCHEMA public FROM PUBLIC", "CREATE")
        expect_refused(f"CREATE TABLE owned_by_app (id int); ALTER TABLE owned_by_app OWNER TO {APP}", "DROP TABLE owned_by_app", "owned by the runtime role")

    def test_a_temporary_table_permission_from_public_is_reported_as_a_warning_only(self):
        with self.owner_engine.connect() as c:
            findings = self.db_grants.verification_report(c, APP)
        self.assertEqual([f[0] for f in findings], ["warning"])
        self.assertIn("TEMPORARY", findings[0][2])

    # ---- rollback ----------------------------------------------------------------------------------------------------
    @unittest.skipUnless(_psql(), "psql is not installed")
    def test_rollback_revokes_every_direct_grant_without_touching_roles_data_or_other_roles(self):
        self._make_stray_state()                                           # include grants the plan would not make
        expected = self._snapshot()
        with self.owner_engine.connect() as c:
            rows_before = c.execute(text("SELECT (SELECT count(*) FROM prompts), (SELECT count(*) FROM brands_raw)")).one()
        script = self.db_grants.rollback_sql(APP, OWNER, self.dbname)
        self.assertIn("run this script as", self._psql_run(APP, script, expect_ok=False).stderr)                              # not as the runtime role
        self.assertIn("this script is for database", self._psql_run(OWNER, self.db_grants.rollback_sql(APP, OWNER, "other_db"), expect_ok=False).stderr)
        self.assertEqual(self._snapshot(), expected, "a refused rollback must change nothing")
        self._psql_run(OWNER, script)
        self.assertEqual(self._direct_grants(APP), [], "no direct grant to the role may remain")
        self.assertEqual({k: v for k, v in self._snapshot().items()}, {}, "no table, view or sequence access remains")
        self._denied("SELECT * FROM prompts")
        self._denied("SELECT * FROM creator_llm_cost")
        with self.owner_engine.connect() as c:
            self.assertEqual(tuple(c.execute(text("SELECT (SELECT count(*) FROM prompts), (SELECT count(*) FROM brands_raw)")).one()), tuple(rows_before), "no data touched")
            self.assertTrue(c.execute(text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)"), {"r": APP}).scalar(), "the role is not dropped")
            self.assertTrue(c.execute(text("SELECT has_table_privilege(:o, 'bak_pre', 'SELECT')"), {"o": OTHER}).scalar(), "other roles' grants stay")
            self.assertTrue(c.execute(text("SELECT has_schema_privilege(:a, 'public', 'USAGE')"), {"a": APP}).scalar(), "what PUBLIC gives everyone is not ours to remove")
        self._psql_run(OWNER, script)                                      # repeatable
        self.assertEqual(self._direct_grants(APP), [])
        self._psql_run(OWNER, self.db_grants.sql_script(APP, OWNER, self.dbname))   # and the grants can be put back
        self.assertEqual({k: v for k, v in self._snapshot().items() if not k.endswith("_seq")}, self._expected_tables())

    @unittest.skipUnless(_psql(), "psql is not installed")
    def test_rollback_does_not_undo_a_membership_it_did_not_create(self):
        with self.admin.connect() as c:
            c.execute(text(f"GRANT {MIDDLE} TO {APP}"))
        self._psql_run(OWNER, self.db_grants.rollback_sql(APP, OWNER, self.dbname))
        with self.admin.connect() as c:
            self.assertTrue(c.execute(text("SELECT pg_has_role(:a, :m, 'MEMBER')"), {"a": APP, "m": MIDDLE}).scalar(), "memberships are reverted by hand (documented), never by the rollback")

    # ---- SQLAdmin: every configured operation is backed by a privilege --------------------------------------------------------
    def test_every_sqladmin_operation_has_its_privilege(self):
        import api.app as api_app
        for view in api_app.admin.views:
            model = getattr(view, "model", None)
            if model is None:
                continue
            table = model.__table__.name
            held = {p: self._scalar(f"SELECT has_table_privilege('{APP}', 'public.{table}', '{p}')") for p in ("SELECT", "INSERT", "UPDATE", "DELETE")}
            self.assertTrue(held["SELECT"], f"{table}: list/detail pages need SELECT")
            if view.can_create:
                self.assertTrue(held["INSERT"], f"{table}: create is enabled in SQLAdmin")
            if view.can_edit:
                self.assertTrue(held["UPDATE"], f"{table}: edit is enabled in SQLAdmin")
            if view.can_delete:
                self.assertTrue(held["DELETE"], f"{table}: delete is enabled in SQLAdmin")
            if table in self.db_grants.VIEWS:
                self.assertFalse(held["INSERT"] or held["UPDATE"] or held["DELETE"], f"{table}: the view is read-only")

    # ---- the real application routes and SQLAdmin, as the runtime role ----------------------------------------------------
    def test_api_routes_and_sqladmin_work_as_the_runtime_role(self):
        from pipeline.db import BrandRaw, CreatorProfile
        from pipeline.helpers.passwords import hash_password
        email, password = f"smoke{time.time_ns()}@example.com", "smoke-password"
        with Session(self.owner_engine) as db:
            brand = BrandRaw(name="Smoke Brand", instagram_handle=f"smoke{time.time_ns()}")
            db.add(brand)
            db.add(CreatorProfile(email=email, password_hash=hash_password(password), full_name="Smoke", age=30, gender="female",
                                  primary_platform="instagram", follower_count=50000, content_niche="beauty", creator_handle="smoke",
                                  instagram_handle="smoke", instagram_followers=50000, instagram_description="d", is_active=True))
            db.commit()
            brand_id = brand.id
        env = {**os.environ, "DATABASE_URL": str(self.url.set(username=APP, password=None).render_as_string(hide_password=False)),
               "RUN_MIGRATIONS": "0", "JWT_SECRET": "smoke-secret", "ADMIN_PASSKEY": "smoke-admin-passkey-0123456789", "ENVIRONMENT": "development"}
        env.pop("MIGRATION_DATABASE_URL", None)
        result = subprocess.run([sys.executable, str(ROOT / "tests" / "_runtime_smoke.py"), email, password, str(brand_id)],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report["denied"], [], "a permission-denied error reached the application")
        names = [s["name"] for s in report["steps"]]
        if os.environ.get("MAVLIT_TEST_VERBOSE"):
            print("\n".join(f'  {s["status"]}  {s["name"]}' for s in report["steps"]))
        self.assertGreaterEqual(sum(n.startswith("admin list") for n in names), 28)
        failed = [s for s in report["steps"] if not s["ok"]]
        self.assertEqual(failed, [], json.dumps(failed, indent=1)[:3000])


if __name__ == "__main__":
    unittest.main()
