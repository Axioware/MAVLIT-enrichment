"""
Tests for separating the database migrations from API startup: RUN_MIGRATIONS, the migration-only credentials
(pipeline/migration_settings.py), `python -m pipeline.migrate`, the lock timeout, the youtube_last_sponsorship
conversion and the removal of runtime DDL.

No test here connects to a database: the URLs point nowhere and every connection is faked or mocked.
(tests/test_migration_postgres.py is the one that needs a disposable PostgreSQL; tests/test_deploy_script.py runs deploy.sh in a sandbox.)
Run:  python -m pytest tests -q
"""

import logging
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_URL = "postgresql+psycopg2://runtime_user:runtime-pw@localhost:1/runtime_db"
MIGRATION_URL = "postgresql+psycopg2://owner_user:owner-secret-pw@migration-host:1/migration_db"
SECRETS = ("owner-secret-pw", "runtime-pw")

# config.py reads DATABASE_URL at import: keep the real .env (and any real database) out of the tests.
os.environ["DATABASE_URL"] = RUNTIME_URL
os.environ.pop("MIGRATION_DATABASE_URL", None)
os.environ["RUN_MIGRATIONS"] = ""
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from api import settings as api_settings  # noqa: E402
from pipeline import migrate, migration_settings, migrations  # noqa: E402
from pipeline.db import engine as runtime_engine  # noqa: E402
from pipeline.migration_settings import MigrationConfigError  # noqa: E402


def _python(code: str, *args: str, **env) -> subprocess.CompletedProcess:
    """Run in a fresh interpreter with exactly this environment (and no .env value able to override it)."""
    full_env = {**os.environ, "DATABASE_URL": RUNTIME_URL, "RUN_MIGRATIONS": "", **env}
    full_env.pop("MIGRATION_DATABASE_URL", None) if "MIGRATION_DATABASE_URL" not in env else None
    return subprocess.run([sys.executable, "-c", code, *args], cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=120)


def _write_env_file(directory: str, url: str = MIGRATION_URL, mode: int = 0o600, name: str = "migrate.env") -> str:
    path = Path(directory) / name
    path.write_text(f"MIGRATION_DATABASE_URL={url}\n")
    path.chmod(mode)
    return str(path)


class _FakeResult:
    rowcount = 0

    def first(self):
        return None

    def all(self):
        return []

    def scalar(self):
        return False


class FakeConn:
    """Records the SQL a migration step sends. Works as a Connection or a Session."""

    def __init__(self, fail_on: str | None = None, fail_with: Exception | None = None):
        self.statements: list[str] = []
        self._fail_on, self._fail_with = fail_on, fail_with

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        self.statements.append(sql)
        if self._fail_on and self._fail_on in sql:
            raise self._fail_with
        return _FakeResult()

    def commit(self):
        self.statements.append("COMMIT")

    def rollback(self):
        self.statements.append("ROLLBACK")

    def query(self, *args, **kwargs):
        return mock.MagicMock()

    def add(self, *args, **kwargs):
        pass


class FakeEngine:
    def __init__(self, **conn_kwargs):
        self.conns: list[FakeConn] = []
        self._conn_kwargs = conn_kwargs
        self.disposed = False

    def _new(self):
        self.conns.append(FakeConn(**self._conn_kwargs))
        return self.conns[-1]

    connect = begin = _new

    def dispose(self):
        self.disposed = True


def _lock_error() -> OperationalError:
    orig = mock.Mock()
    orig.pgcode = "55P03"
    return OperationalError("ALTER TABLE brands_raw ...", {}, orig)


def _run_all_steps(engine, lock_timeout_ms):
    """Run every migration transaction against a fake engine (create_all and the ORM session are replaced)."""
    with mock.patch.object(migrations.Base.metadata, "create_all"), \
            mock.patch.object(migrations, "sessionmaker", return_value=lambda: engine._new()):
        migrations.run_migrations(engine, lock_timeout_ms)


class ApiSettings(unittest.TestCase):
    def test_default_keeps_the_current_behaviour(self):
        for value in (None, "", "   "):
            self.assertTrue(api_settings.parse_run_migrations(value), repr(value))
        self.assertEqual(_python("from api import settings; print(settings.RUN_MIGRATIONS)").stdout.strip(), "True")

    def test_accepted_values(self):
        for value in ("1", "true", "TRUE", "yes", "on", " On "):
            self.assertTrue(api_settings.parse_run_migrations(value), value)
        for value in ("0", "false", "False", "no", "off", " OFF "):
            self.assertFalse(api_settings.parse_run_migrations(value), value)

    def test_invalid_value_stops_the_api_with_a_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            api_settings.parse_run_migrations("maybe")
        self.assertIn("RUN_MIGRATIONS must be one of", str(ctx.exception))
        result = _python("from api import settings", RUN_MIGRATIONS="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RUN_MIGRATIONS must be one of", result.stderr)

    def test_environment_reaches_the_setting(self):
        self.assertEqual(_python("from api import settings; print(settings.RUN_MIGRATIONS)", RUN_MIGRATIONS="0").stdout.strip(), "False")

    def test_config_py_knows_nothing_about_migrations(self):   # B1 and "the migration CLI must not depend on RUN_MIGRATIONS"
        for name in ("RUN_MIGRATIONS", "MIGRATION_DATABASE_URL", "parse_run_migrations"):
            self.assertFalse(hasattr(config, name), name)
        self.assertNotIn("MIGRATION", (ROOT / "config.py").read_text())


class ApiStartup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api.app as api_app
        from fastapi.testclient import TestClient
        cls.api_app, cls.TestClient = api_app, TestClient

    def _start(self, enabled: bool) -> mock.Mock:
        migrate_mock = mock.Mock()
        with mock.patch.object(self.api_app, "RUN_MIGRATIONS", enabled), mock.patch.object(self.api_app, "_run_migrations", migrate_mock):
            with self.TestClient(self.api_app.app):
                pass
        return migrate_mock

    def test_on_runs_the_migrations(self):
        self.assertEqual(self._start(True).call_count, 1)

    def test_off_skips_the_migrations(self):
        self.assertEqual(self._start(False).call_count, 0)

    def test_the_default_is_on(self):
        self.assertTrue(self.api_app.RUN_MIGRATIONS)

    def test_startup_migrations_use_the_runtime_engine_only(self):
        with mock.patch.object(self.api_app, "run_migrations") as shared:
            self.api_app._run_migrations()
        shared.assert_called_once_with()   # no engine, no lock timeout: the old behaviour

    def test_the_api_never_reads_the_migration_url(self):
        for path in [*(ROOT / "api").glob("*.py"), ROOT / "config.py", ROOT / "pipeline" / "db.py", ROOT / "pipeline" / "helpers" / "gpt_llm.py"]:
            if path.name == "settings.py":
                continue
            self.assertNotIn("MIGRATION_DATABASE_URL", path.read_text(), str(path))

    def test_a_migration_url_in_the_api_environment_is_reported_and_removed(self):
        with mock.patch.dict(os.environ, {"MIGRATION_DATABASE_URL": MIGRATION_URL}):
            with self.assertLogs(api_settings.logger, level="WARNING") as logs:
                with mock.patch.object(self.api_app, "RUN_MIGRATIONS", False), self.TestClient(self.api_app.app):
                    self.assertNotIn("MIGRATION_DATABASE_URL", os.environ)
        text = "\n".join(logs.output)
        self.assertIn("MIGRATION_DATABASE_URL", text)
        for secret in (*SECRETS, "migration-host", "owner_user"):
            self.assertNotIn(secret, text)


class MigrationTargetResolution(unittest.TestCase):
    def test_env_file_is_used_and_does_not_touch_the_environment(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_env_file(d)
            before = dict(os.environ)
            target = migration_settings.resolve_migration_target(env_file=path, environ={})
            self.assertEqual(os.environ, before)
        self.assertEqual(target.url, MIGRATION_URL)
        self.assertIn(path, target.source)
        self.assertNotIn("owner-secret-pw", target.source)

    def test_env_file_must_not_be_readable_by_others(self):
        with tempfile.TemporaryDirectory() as d:
            for mode in (0o644, 0o640, 0o604):
                path = _write_env_file(d, mode=mode)
                with self.assertRaises(MigrationConfigError) as ctx:
                    migration_settings.resolve_migration_target(env_file=path, environ={})
                self.assertIn("chmod 600", str(ctx.exception))
                self.assertNotIn("owner-secret-pw", str(ctx.exception))

    def test_env_file_problems_are_clear(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(MigrationConfigError) as ctx:
                migration_settings.resolve_migration_target(env_file=str(Path(d) / "missing.env"), environ={})
            self.assertIn("does not exist", str(ctx.exception))
            empty = Path(d) / "empty.env"
            empty.write_text("OTHER=1\n")
            empty.chmod(0o600)
            with self.assertRaises(MigrationConfigError) as ctx:
                migration_settings.resolve_migration_target(env_file=str(empty), environ={})
            self.assertIn("does not define MIGRATION_DATABASE_URL", str(ctx.exception))

    def test_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            default_file = Path(_write_env_file(d, url="postgresql://a:default-pw@h/default_db", name="default.env"))
            with mock.patch.object(migration_settings, "DEFAULT_ENV_FILE", default_file):
                explicit = _write_env_file(d, url="postgresql://a:explicit-pw@h/explicit_db", name="explicit.env")
                env = {"MIGRATION_DATABASE_URL": "postgresql://a:var-pw@h/var_db"}
                self.assertIn("explicit_db", migration_settings.resolve_migration_target(env_file=explicit, environ=env).url)
                self.assertIn("var_db", migration_settings.resolve_migration_target(environ=env).url)
                self.assertIn("default_db", migration_settings.resolve_migration_target(environ={}).url)
                via_variable = migration_settings.resolve_migration_target(environ={"MIGRATION_ENV_FILE": explicit})
                self.assertIn("explicit_db", via_variable.url)
            with mock.patch.object(migration_settings, "DEFAULT_ENV_FILE", Path(d) / "absent.env"):
                fallback = migration_settings.resolve_migration_target(environ={})
        self.assertIsNone(fallback.url)
        self.assertIn("DATABASE_URL", fallback.source)

    def test_urls_are_validated_without_echoing_them(self):
        bad = {
            "not a url at all, password hunter2": "valid database URL",
            "mysql+pymysql://u:hunter2@h/db": "PostgreSQL URL",
            "postgresql+psycopg2://u:hunter2@h": "does not name a database",
        }
        for value, expected in bad.items():
            with self.assertRaises(MigrationConfigError) as ctx:
                migration_settings.resolve_migration_target(environ={"MIGRATION_DATABASE_URL": value})
            self.assertIn(expected, str(ctx.exception))
            self.assertNotIn("hunter2", str(ctx.exception))
            self.assertNotIn(value, str(ctx.exception))

    def test_lock_timeout_setting(self):
        resolve = migration_settings.resolve_lock_timeout_ms
        self.assertEqual(resolve(None, {}), migration_settings.DEFAULT_LOCK_TIMEOUT_MS)
        self.assertEqual(resolve(None, {"MIGRATION_LOCK_TIMEOUT_MS": "2500"}), 2500)
        self.assertEqual(resolve("1500", {"MIGRATION_LOCK_TIMEOUT_MS": "2500"}), 1500)
        self.assertIsNone(resolve("0", {}))
        for bad in ("abc", "-5", "99999999999"):
            with self.assertRaises(MigrationConfigError):
                resolve(bad, {})

    def test_build_engine_is_lazy_and_does_not_pool(self):
        engine = migration_settings.build_engine(MIGRATION_URL)
        self.addCleanup(engine.dispose)
        self.assertEqual(engine.url.host, "migration-host")
        self.assertEqual(type(engine.pool).__name__, "NullPool")

    def test_redact(self):
        text = f"could not connect using {MIGRATION_URL} (password owner-secret-pw) and runtime-pw"
        out = migration_settings.redact(text, MIGRATION_URL, RUNTIME_URL, None)
        for secret in (*SECRETS, MIGRATION_URL):
            self.assertNotIn(secret, out)

    def test_the_settings_module_loads_no_database_code(self):
        result = _python("import sys, pipeline.migration_settings; print('pipeline.db' in sys.modules, 'config' in sys.modules)")
        self.assertEqual(result.stdout.strip(), "False False", result.stderr)


class MigrationCommand(unittest.TestCase):
    def test_dry_run_connects_to_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_env_file(d)
            with mock.patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("connected")), \
                    mock.patch.object(migrate, "run_migrations") as run_mock, mock.patch.object(migrate, "build_engine") as build_mock:
                self.assertEqual(migrate.main(["--dry-run", "--env-file", path]), 0)
        run_mock.assert_not_called()
        build_mock.assert_not_called()

    def test_dry_run_validates_the_settings(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_env_file(d, mode=0o644)
            with self.assertLogs("pipeline.migrate", level="ERROR") as logs:
                self.assertEqual(migrate.main(["--dry-run", "--env-file", path]), migrate.EXIT_CONFIG)
        self.assertNotIn("owner-secret-pw", "\n".join(logs.output))

    def test_it_migrates_with_the_migration_url_and_the_lock_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_env_file(d)
            with mock.patch.object(migrations, "_apply_migrations") as apply_mock:
                self.assertEqual(migrate.main(["--env-file", path, "--lock-timeout-ms", "1234"]), 0)
        engine, timeout, grant_role = apply_mock.call_args.args
        self.assertEqual((engine.url.host, engine.url.database, engine.url.username), ("migration-host", "migration_db", "owner_user"))
        self.assertEqual(timeout, 1234)
        self.assertIsNone(grant_role, "no grants unless --grant-role / MIGRATION_GRANT_ROLE is given")

    def test_without_a_migration_url_it_falls_back_to_the_runtime_engine(self):
        with mock.patch.object(migration_settings, "DEFAULT_ENV_FILE", ROOT / "does-not-exist.env"), \
                mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(migrations, "_apply_migrations") as apply_mock:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
            os.environ.pop("MIGRATION_ENV_FILE", None)
            self.assertEqual(migrate.main([]), 0)
        self.assertIs(apply_mock.call_args.args[0], runtime_engine)

    def test_the_dedicated_engine_is_disposed_on_every_path(self):
        cases = [
            ("success", None, migrate.EXIT_OK),
            ("failure", RuntimeError("boom"), migrate.EXIT_FAILED),
            ("lock timeout", migrations.MigrationLockTimeout("lock"), migrate.EXIT_LOCK_TIMEOUT),
        ]
        for label, error, code in cases:
            engine = FakeEngine()
            with tempfile.TemporaryDirectory() as d, mock.patch.object(migrate, "build_engine", return_value=engine), \
                    mock.patch.object(migrate, "run_migrations", side_effect=error):
                self.assertEqual(migrate.main(["--env-file", _write_env_file(d)]), code, label)
            self.assertTrue(engine.disposed, label)

    def test_a_failure_is_reported_without_credentials(self):
        leak = RuntimeError(f"connection to {MIGRATION_URL} failed, password owner-secret-pw, runtime-pw")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"DATABASE_URL": RUNTIME_URL}), \
                mock.patch.object(migrate, "build_engine", return_value=FakeEngine()), \
                mock.patch.object(migrate, "run_migrations", side_effect=leak), self.assertLogs("pipeline.migrate", level="INFO") as logs:
            self.assertEqual(migrate.main(["--env-file", _write_env_file(d), "--traceback"]), migrate.EXIT_FAILED)
        text = "\n".join(logs.output)
        for secret in (*SECRETS, MIGRATION_URL):
            self.assertNotIn(secret, text)
        self.assertIn("Migrations failed: RuntimeError", text)

    def test_a_malformed_url_is_a_config_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_env_file(d, url="garbage with hunter2 inside")
            with self.assertLogs("pipeline.migrate", level="ERROR") as logs:
                self.assertEqual(migrate.main(["--env-file", path]), migrate.EXIT_CONFIG)
        self.assertNotIn("hunter2", "\n".join(logs.output))

    def test_the_command_does_not_depend_on_run_migrations_or_load_the_api(self):
        result = _python("import sys, pipeline.migrate as m; print('api.app' in sys.modules, 'fastapi' in sys.modules, m.main(['--dry-run']))", RUN_MIGRATIONS="banana")
        self.assertEqual(result.stdout.strip(), "False False 0", result.stderr)
        self.assertEqual(_python("import sys; from pipeline import migrate; sys.exit(migrate.main(['--dry-run']))", RUN_MIGRATIONS="banana").returncode, 0)


class LockTimeout(unittest.TestCase):
    def test_every_transaction_starts_with_the_timeout(self):
        engine = FakeEngine()
        _run_all_steps(engine, 4321)
        self.assertGreaterEqual(len(engine.conns), 6)
        for conn in engine.conns:
            self.assertEqual(conn.statements[0], "SET LOCAL lock_timeout = 4321")
            for i, statement in enumerate(conn.statements[:-1]):
                if statement == "COMMIT":   # a new transaction begins after a commit
                    self.assertEqual(conn.statements[i + 1], "SET LOCAL lock_timeout = 4321")

    def test_without_a_timeout_nothing_is_set(self):   # the API's startup: the old behaviour
        engine = FakeEngine()
        _run_all_steps(engine, None)
        self.assertFalse(any("lock_timeout" in s for conn in engine.conns for s in conn.statements))

    def test_a_lock_timeout_becomes_a_clear_error_and_stops(self):
        engine = FakeEngine(fail_on="ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS country", fail_with=_lock_error())
        with self.assertRaises(migrations.MigrationLockTimeout) as ctx:
            _run_all_steps(engine, 250)
        self.assertIn("250 ms", str(ctx.exception))
        self.assertIn("rolled back", str(ctx.exception))
        stopped_at = [c for c in engine.conns if any("country" in s for s in c.statements)][0]
        self.assertNotIn("COMMIT", stopped_at.statements)   # the failing transaction was never committed
        self.assertFalse(any("login_credentials" in s for c in engine.conns for s in c.statements), "later steps must not run")

    def test_a_lock_timeout_is_not_swallowed_by_the_extension_step(self):
        engine = FakeEngine(fail_on="CREATE EXTENSION", fail_with=_lock_error())
        with self.assertRaises(migrations.MigrationLockTimeout):
            _run_all_steps(engine, 250)

    def test_other_database_errors_are_not_disguised(self):
        orig = mock.Mock()
        orig.pgcode = "42501"
        engine = FakeEngine(fail_on="ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS country", fail_with=OperationalError("ALTER", {}, orig))
        with self.assertRaises(OperationalError):
            _run_all_steps(engine, 250)

    def test_the_command_exits_with_the_lock_timeout_code(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(migrate, "build_engine", return_value=FakeEngine()), \
                mock.patch.object(migrations, "_apply_migrations", side_effect=_lock_error()):
            self.assertEqual(migrate.main(["--env-file", _write_env_file(d), "--lock-timeout-ms", "50"]), migrate.EXIT_LOCK_TIMEOUT)

    def test_a_negative_timeout_is_rejected(self):
        with self.assertRaises(migrations.MigrationError):
            migrations.run_migrations(FakeEngine(), -1)


class YoutubeLastSponsorship(unittest.TestCase):
    """Unit tests with a scripted connection; tests/test_migration_postgres.py runs the real thing."""

    class Conn(FakeConn):
        def __init__(self, data_type, alter_error=None):
            super().__init__()
            self._data_type, self._alter_error = data_type, alter_error

        def execute(self, statement, *args, **kwargs):
            sql = str(statement)
            self.statements.append(sql)
            if sql.lstrip().startswith("SELECT data_type"):
                result = mock.MagicMock()
                result.first.return_value = None if self._data_type is None else (self._data_type,)
                return result
            if sql.startswith("ALTER") and self._alter_error:
                raise self._alter_error
            return _FakeResult()

    def _alters(self, conn):
        return [s for s in conn.statements if s.startswith("ALTER")]

    def test_a_timestamptz_column_is_left_alone(self):
        conn = self.Conn("timestamp with time zone")
        migrations._convert_youtube_last_sponsorship(conn)
        self.assertEqual(self._alters(conn), [])

    def test_a_missing_column_is_ignored(self):
        conn = self.Conn(None)
        migrations._convert_youtube_last_sponsorship(conn)
        self.assertEqual(self._alters(conn), [])

    def test_older_types_are_converted_keeping_their_values(self):
        for data_type in ("text", "character varying", "timestamp without time zone", "date"):
            conn = self.Conn(data_type)
            migrations._convert_youtube_last_sponsorship(conn)
            alters = self._alters(conn)
            self.assertEqual(len(alters), 1, data_type)
            self.assertIn("youtube_last_sponsorship::timestamptz" if "time" in data_type or data_type == "date" else "NULLIF(BTRIM(youtube_last_sponsorship)", alters[0])
            self.assertNotIn("USING NULL::", alters[0])

    def test_an_unexpected_type_stops_instead_of_clearing_values(self):
        conn = self.Conn("integer")
        with self.assertRaises(migrations.MigrationSchemaError) as ctx:
            migrations._convert_youtube_last_sponsorship(conn)
        self.assertIn("unexpected type", str(ctx.exception))
        self.assertEqual(self._alters(conn), [])

    def test_an_unparseable_value_stops_with_a_clear_error(self):
        orig = mock.Mock()
        orig.pgcode = "22007"
        conn = self.Conn("text", alter_error=OperationalError("ALTER", {}, orig))
        with self.assertRaises(migrations.MigrationSchemaError) as ctx:
            migrations._convert_youtube_last_sponsorship(conn)
        self.assertIn("not a valid timestamp", str(ctx.exception))

    def test_the_clearing_statement_is_gone_and_the_step_keeps_its_place(self):
        source = (ROOT / "pipeline" / "migrations.py").read_text()
        self.assertNotIn("youtube_last_sponsorship TYPE TIMESTAMPTZ USING NULL", source)   # the statement itself (prose may still mention it)
        self.assertEqual(source.count("        _convert_youtube_last_sponsorship,\n"), 1)

    def test_the_view_keeps_its_grants_across_drop_and_create(self):
        source = (ROOT / "pipeline" / "migrations.py").read_text()
        save, drop = source.index("        view_acl.save,\n"), source.index('"DROP VIEW IF EXISTS creator_llm_cost"')
        create, restore, grant = source.index("CREATE VIEW creator_llm_cost AS"), source.index("        view_acl.restore,"), source.index("_grant_creator_llm_cost(grant_role),")
        self.assertLess(save, drop)
        self.assertLess(drop, create)
        self.assertLess(create, restore)
        self.assertLess(restore, grant)

    def test_the_step_runs_inside_the_migration_transaction(self):
        engine = FakeEngine()
        with mock.patch.object(migrations, "_convert_youtube_last_sponsorship") as step:
            _run_all_steps(engine, None)
        step.assert_called_once()


class _Stop(Exception):
    pass


class NoRuntimeDdl(unittest.TestCase):
    def _executed(self, db) -> list[str]:
        return [str(call.args[0]) for call in db.execute.call_args_list]

    def _db(self, column_exists: bool) -> mock.MagicMock:
        db = mock.MagicMock()
        db.execute.return_value.first.return_value = (1,) if column_exists else None
        db.query.side_effect = _Stop
        return db

    def setUp(self):
        from pipeline.enrichment import instagram_users
        self.iu = instagram_users
        self.iu._post_collaborators_checked = False
        patcher = mock.patch.object(self.iu, "APIFY_TOKEN", "token")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, self.iu, "_post_collaborators_checked", False)

    def test_enrich_instagram_users_changes_nothing_when_the_column_exists(self):
        db = self._db(True)
        with self.assertRaises(_Stop):
            self.iu.enrich_instagram_users(db, limit=1)
        self.assertFalse(any("ALTER" in sql for sql in self._executed(db)))
        db.commit.assert_not_called()

    def test_enrich_instagram_users_fails_clearly_when_the_column_is_missing(self):
        db = self._db(False)
        with self.assertRaises(RuntimeError) as ctx:
            self.iu.enrich_instagram_users(db, limit=1)
        self.assertIn("python -m pipeline.migrate", str(ctx.exception))
        self.assertFalse(any("ALTER" in sql for sql in self._executed(db)))

    def test_the_cli_flag_adds_the_column_only_when_it_is_missing(self):
        db = self._db(False)
        with self.assertRaises(_Stop):
            self.iu.enrich_instagram_users(db, limit=1, ensure_schema=True)
        self.assertEqual(sum("ADD COLUMN IF NOT EXISTS post_collaborators" in sql for sql in self._executed(db)), 1)
        db = self._db(True)
        with self.assertRaises(_Stop):
            self.iu.enrich_instagram_users(db, limit=1, ensure_schema=True)
        self.assertFalse(any("ALTER" in sql for sql in self._executed(db)))

    def test_cli_runners_ask_for_the_schema_check(self):
        for script in ("run_end_to_end_pipeline.py", "run_reverse_engineering.py"):
            self.assertIn("ensure_schema=True", (ROOT / script).read_text(), script)

    def test_usage_tracking_never_creates_tables(self):
        from pipeline import db as pipeline_db
        from pipeline.helpers import gpt_llm
        session = mock.MagicMock()
        session.__enter__.return_value = session
        with mock.patch.object(pipeline_db, "SessionLocal", return_value=session), \
                mock.patch.object(pipeline_db.LlmUsage.__table__, "create") as llm_create, \
                mock.patch.object(pipeline_db.ApifyUsage.__table__, "create") as apify_create:
            gpt_llm._record_usage("/v1/chat", {"model": "gpt-6-luna"}, {"usage": {}}, "test", 0.1)
            with gpt_llm.llm_usage_scope(1, "test"):
                gpt_llm.record_apify_scrape("instagram", "someone", 5, 5)
        llm_create.assert_not_called()
        apify_create.assert_not_called()
        self.assertEqual(session.add.call_count, 2)

    def test_a_missing_usage_table_is_reported_without_raising(self):
        from pipeline import db as pipeline_db
        from pipeline.helpers import gpt_llm
        session = mock.MagicMock()
        session.commit.side_effect = RuntimeError('relation "llm_usage" does not exist')
        with mock.patch.object(pipeline_db, "SessionLocal", return_value=session), self.assertLogs(gpt_llm.logger, level="ERROR") as logs:
            gpt_llm._record_usage("/v1/chat", {"model": "gpt-6-luna"}, {"usage": {}}, "test", 0.1)
        self.assertIn("python -m pipeline.migrate", "\n".join(logs.output))

    def test_no_ddl_calls_remain_in_the_runtime_helpers(self):
        source = (ROOT / "pipeline/helpers/gpt_llm.py").read_text()
        self.assertNotIn("__table__.create", source)
        self.assertNotIn("create_all", source)


if __name__ == "__main__":
    unittest.main()
