"""
Query-timeout regression tests (checklist 2.10): server-side timeouts on the API engine and the batch engine, a clean 503 for a cancelled
query, and no raw database error text stored or returned. The first group contacts no database; the last needs a disposable local
PostgreSQL (MAVLIT_TEST_PG_URL, same rules as tests/test_migration_postgres.py).

Run:  python -m pytest tests/test_query_timeouts.py -q
"""

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"
os.environ["JWT_SECRET"] = os.environ.get("JWT_SECRET") or "test-jwt-secret"
sys.path.insert(0, str(ROOT))

TIMEOUT_VARS = ("DB_STATEMENT_TIMEOUT_MS", "DB_LOCK_TIMEOUT_MS", "DB_IDLE_TX_TIMEOUT_MS", "DB_CONNECT_TIMEOUT")
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")


def _python(code: str, **env) -> str:
    full_env = {**os.environ, **env}
    for name in TIMEOUT_VARS:
        if name not in env:
            full_env.pop(name, None)
    prelude = "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"   # the defaults must come from the code, not a .env
    result = subprocess.run([sys.executable, "-c", prelude + code], cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise AssertionError(result.stderr[-1500:])
    return result.stdout.strip()


# What the engine hands to psycopg2.connect(): captured through the do_connect event, which then aborts so nothing is contacted.
CAPTURE = (
    "import json; from sqlalchemy import event\n"
    "def captured(e):\n"
    "    box = {}\n"
    "    @event.listens_for(e, 'do_connect')\n"
    "    def grab(dialect, rec, cargs, cparams):\n"
    "        box.update(cparams); raise RuntimeError('stop')\n"
    "    try: e.raw_connection()\n"
    "    except Exception: pass\n"
    "    return {k: box[k] for k in ('connect_timeout', 'options')}\n"
)


def _args(engine_name="engine", **env):
    code = CAPTURE + f"from pipeline.db import {engine_name} as e; print(json.dumps(captured(e)))"
    return json.loads(_python(code, **env))


class ApiEngineTimeouts(unittest.TestCase):
    def test_defaults(self):
        args = _args()
        self.assertEqual(args["connect_timeout"], 5)
        self.assertEqual(args["options"], "-c statement_timeout=15000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=60000")

    def test_environment_overrides(self):
        args = _args(DB_STATEMENT_TIMEOUT_MS="1800000", DB_LOCK_TIMEOUT_MS="10000", DB_IDLE_TX_TIMEOUT_MS="300000", DB_CONNECT_TIMEOUT="9")
        self.assertEqual(args["connect_timeout"], 9)
        self.assertEqual(args["options"], "-c statement_timeout=1800000 -c lock_timeout=10000 -c idle_in_transaction_session_timeout=300000")

    def test_zero_disables_a_timeout(self):
        self.assertIn("-c statement_timeout=0 ", _args(DB_STATEMENT_TIMEOUT_MS="0")["options"])

    def test_non_numeric_value_raises_value_error(self):
        for name in TIMEOUT_VARS:
            with self.subTest(name), self.assertRaises(AssertionError) as ctx:
                _args(**{name: "soon"})
            self.assertIn("ValueError", str(ctx.exception))

    def test_negative_value_raises_value_error(self):
        with self.assertRaises(AssertionError) as ctx:
            _args(DB_LOCK_TIMEOUT_MS="-1")
        self.assertIn("ValueError", str(ctx.exception))

    def test_api_pool_caps_unchanged(self):
        out = _python("from pipeline.db import engine as e; print(e.pool.size(), e.pool._max_overflow)")
        self.assertEqual(out, "10 5")


class BatchEngine(unittest.TestCase):
    def test_batch_session_timeouts_and_pool(self):
        out = _python(
            CAPTURE + "from pipeline.db import BatchSessionLocal as S, batch_engine as e; p = e.pool; "
            "print(json.dumps([captured(e), p.size(), p._max_overflow, S.kw['bind'] is e, S.kw['expire_on_commit']]))",
            DB_STATEMENT_TIMEOUT_MS="1",   # the batch engine must ignore the API's environment overrides
        )
        args, size, overflow, bound, expire = json.loads(out)
        self.assertEqual(args["options"], "-c statement_timeout=1800000 -c lock_timeout=10000 -c idle_in_transaction_session_timeout=300000")
        self.assertEqual((size, overflow, bound, expire), (2, 0, True, False))

    def test_long_jobs_use_the_batch_session(self):
        import ast
        tree = ast.parse((ROOT / "api" / "app.py").read_text())
        wanted = {"_run_seed_job", "_run_enrich_job", "_run_scoring_job", "_run_ai_ranking_job", "_run_creator_description_job"}
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                found[node.name] = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertEqual(set(found), wanted)
        for name, calls in found.items():
            self.assertIn("BatchSessionLocal", calls, name)
            self.assertNotIn("SessionLocal", calls, name)


SKIP_DIRS = {".venv", "node_modules", "production_dump", "__pycache__", ".git", "tests", "frontend", "api"}
# Modules outside api/ (request routes stay on SessionLocal) that may still use the short-timeout SessionLocal, with the reason.
SESSIONLOCAL_EXCEPTIONS = {
    "pipeline/db.py": "defines SessionLocal and get_db (the request-route dependency)",
    "pipeline/helpers/gpt_llm.py": "usage recording: its own session that inserts one row after the LLM/Apify call returned, never held across one",
    "pipeline/enrichment_re/content_creator_re.py": "add_content_creator_re: a quick insert called from the /content-creator-re/bulk-add route (its CLI main uses BatchSessionLocal)",
    "pipeline/own_brand.py": "run_own_brand_job: runs in the API process and commits before its LLM call (run_followup uses BatchSessionLocal)",
}


def _uses_session_local(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Name) and node.id == "SessionLocal":
            return True
        if isinstance(node, ast.ImportFrom) and any(a.name == "SessionLocal" for a in node.names):
            return True
    return False


class LongWorkUsesTheBatchSession(unittest.TestCase):
    """Runners, CLIs and enrichment code call Apify/LLMs while a session is open, so they must not run under the API's short timeouts."""

    def _modules(self):
        for path in sorted(ROOT.rglob("*.py")):
            rel = path.relative_to(ROOT)
            if not SKIP_DIRS.intersection(rel.parts):
                yield rel.as_posix(), path

    def test_only_listed_modules_import_sessionlocal(self):
        offenders = [rel for rel, path in self._modules() if rel not in SESSIONLOCAL_EXCEPTIONS and _uses_session_local(path)]
        self.assertEqual(offenders, [], "use BatchSessionLocal for long work, or add a reasoned entry to SESSIONLOCAL_EXCEPTIONS")

    def test_exceptions_list_has_no_stale_entries(self):
        for rel in SESSIONLOCAL_EXCEPTIONS:
            self.assertTrue((ROOT / rel).is_file(), rel)
            self.assertTrue(_uses_session_local(ROOT / rel), f"{rel} no longer uses SessionLocal: drop it from the exceptions")

    def test_batch_users_are_found(self):
        users = [rel for rel, path in self._modules() if "BatchSessionLocal" in path.read_text()]
        for expected in ("main.py", "run_end_to_end_pipeline.py", "pipeline/enrichment/brand_tier.py", "pipeline/own_brand.py"):
            self.assertIn(expected, users)


class SafeErrorText(unittest.TestCase):
    SECRET = "SELECT secret_column FROM creator_profiles WHERE email = 'victim@example.com'"

    def _dbapi_error(self, pgcode):
        from sqlalchemy.exc import OperationalError
        orig = type("Orig", (Exception,), {"pgcode": pgcode})(f"canceling statement due to statement timeout\n{self.SECRET}")
        return OperationalError(self.SECRET, {"email": "victim@example.com"}, orig)

    def test_safe_error(self):
        import api.app as api_app
        self.assertEqual(api_app._safe_error(self._dbapi_error("57014")), "database timeout")
        self.assertEqual(api_app._safe_error(self._dbapi_error("55P03")), "OperationalError")
        self.assertEqual(api_app._safe_error(ValueError("x")), "ValueError")

    def test_background_jobs_do_not_store_database_text(self):
        import api.app as api_app
        for pgcode in ("57014", "08006"):
            with self.subTest(pgcode):
                api_app._jobs["j"] = {"status": "running"}
                session = mock.MagicMock()
                with mock.patch.object(api_app, "BatchSessionLocal", return_value=session), \
                        mock.patch.object(api_app, "run_brand_scoring", side_effect=self._dbapi_error(pgcode)):
                    api_app._run_scoring_job("j", 5)
                self.assertEqual(api_app._jobs["j"]["status"], "error")
                self.assertNotIn("victim", json.dumps(api_app._jobs["j"]))
                self.assertNotIn("SELECT", json.dumps(api_app._jobs["j"]))
                session.close.assert_called_once()

    def test_ranking_job_stores_a_safe_message(self):
        import api.app as api_app
        row = mock.MagicMock()
        session = mock.MagicMock()
        session.query.return_value.filter.return_value.first.return_value = row
        api_app._AI_RANKINGS["v3"]["rank"] = mock.Mock(side_effect=self._dbapi_error("57014"))
        try:
            with mock.patch.object(api_app, "BatchSessionLocal", return_value=session):
                api_app._run_ai_ranking_job(1, "v3")
        finally:
            api_app._AI_RANKINGS["v3"]["rank"] = lambda db, cid: api_app.rank_brands_with_llm_v3(db, cid)
        self.assertEqual(row.llm_v3_ranking_error, "database timeout")
        self.assertEqual(row.llm_v3_ranking_status, "failed")

    def test_no_route_passes_str_of_a_database_error_through(self):
        # The HTTPException(detail=str(exc)) sites only catch ValueError/RuntimeError/LookupError, which are not DBAPIErrors.
        from sqlalchemy.exc import DBAPIError
        for cls in (ValueError, RuntimeError, LookupError):
            self.assertFalse(issubclass(DBAPIError, cls))


class HandlerWiring(unittest.TestCase):
    def test_app_registers_the_handler(self):
        import api.app as api_app
        from sqlalchemy.exc import DBAPIError
        self.assertIn(DBAPIError, api_app.app.exception_handlers)

    def test_codes_and_bodies_without_a_database(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy.exc import DBAPIError
        import api.app as api_app
        small = FastAPI()
        small.add_exception_handler(DBAPIError, api_app._database_error)
        errors = SafeErrorText()

        @small.get("/{code}")
        def boom(code: str):
            raise errors._dbapi_error(code)

        client = TestClient(small, raise_server_exceptions=False)
        for code, status, detail in (("57014", 503, "Database busy, retry"), ("55P03", 503, "Database busy, retry"), ("08006", 500, "Internal server error")):
            with self.subTest(code):
                r = client.get(f"/{code}")
                self.assertEqual((r.status_code, r.json()), (status, {"detail": detail}))
                self.assertNotIn("SELECT", r.text)
                self.assertNotIn("victim", r.text)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class RealStatementTimeout(unittest.TestCase):
    def setUp(self):
        from sqlalchemy.engine import make_url
        url = make_url(TEST_URL)
        host = url.host or (url.query.get("host") or "")
        if not (host in ("", "localhost", "127.0.0.1") or str(host).startswith("/")) or not any(w in (url.database or "") for w in ("test", "scratch")):
            raise RuntimeError("MAVLIT_TEST_PG_URL must be a LOCAL database whose name contains 'test' or 'scratch'; refusing to run")

    def test_cancelled_query_returns_503_without_sql(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, text
        from sqlalchemy.exc import DBAPIError
        import api.app as api_app
        from pipeline.db import _connect_args

        engine = create_engine(TEST_URL, connect_args=_connect_args(200, 5000, 60000, 5))
        small = FastAPI()
        small.add_exception_handler(DBAPIError, api_app._database_error)

        @small.get("/slow")
        def slow():
            with engine.connect() as conn:
                conn.execute(text("SELECT pg_sleep(5), 'private-marker-value' AS leaked_column"))
            return {"ok": True}

        try:
            with engine.connect() as conn:
                self.assertEqual(conn.execute(text("SHOW statement_timeout")).scalar(), "200ms")
            client = TestClient(small, raise_server_exceptions=False)
            r = client.get("/slow")
        finally:
            engine.dispose()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"detail": "Database busy, retry"})
        for fragment in ("pg_sleep", "private-marker-value", "SELECT", "statement timeout", "leaked_column"):
            self.assertNotIn(fragment, r.text)

    def test_lock_timeout_and_idle_timeout_apply(self):
        from sqlalchemy import create_engine, text
        from pipeline.db import _connect_args
        engine = create_engine(TEST_URL, connect_args=_connect_args(0, 7000, 90000, 5))
        try:
            with engine.connect() as conn:
                got = [conn.execute(text(f"SHOW {n}")).scalar() for n in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout")]
        finally:
            engine.dispose()
        self.assertEqual(got, ["0", "7s", "90s"])


if __name__ == "__main__":
    unittest.main()
