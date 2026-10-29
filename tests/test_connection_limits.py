"""
Connection-limit regression tests (checklist 2.9): the API engine has an explicit, bounded pool; every utility script is capped at one
connection; the per-role limits file exists and is consistent with those caps. No database is contacted.

Run:  python -m pytest tests/test_connection_limits.py -q
"""

import ast
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"
os.environ["JWT_SECRET"] = os.environ.get("JWT_SECRET") or "test-jwt-secret"
sys.path.insert(0, str(ROOT))

SKIP_DIRS = {".venv", "node_modules", "production_dump", "__pycache__", ".git", "tests", "frontend"}
CAPPED_SCRIPTS = [
    "script_creator_niche/mavlit_user_similarity.py", "script_creator_niche/embed_creator_niches.py", "scripts/embed_test_niches.py",
    "scripts/niche_cosine_similarity.py", "scripts/populate_test_niches.py", "scripts/llm_cost_report.py",
]
# create_engine() is allowed to differ from the one-connection cap only here: the shared engine, and the migration engine (NullPool).
OTHER_ENGINES = {"pipeline/db.py", "pipeline/migration_settings.py"}


def _create_engine_calls(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (getattr(node.func, "id", None) == "create_engine" or getattr(node.func, "attr", None) == "create_engine"):
            yield node, {kw.arg: kw.value for kw in node.keywords}


def _python(code: str, **env) -> str:
    full_env = {**os.environ, "DATABASE_URL": os.environ["DATABASE_URL"], "JWT_SECRET": os.environ["JWT_SECRET"], **env}
    for name in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW"):
        if name not in env:
            full_env.pop(name, None)
    # keep the project's .env out of the picture: the defaults must come from the code
    prelude = "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
    result = subprocess.run([sys.executable, "-c", prelude + code], cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise AssertionError(result.stderr[-1500:])
    return result.stdout.strip()


POOL_REPORT = "import json; from pipeline.db import engine as e; p = e.pool; print(json.dumps([p.size(), p._max_overflow, p._timeout, p._recycle, p._pre_ping]))"


class ApiEngine(unittest.TestCase):
    def test_defaults_are_the_documented_api_settings(self):
        import json
        size, overflow, timeout, recycle, pre_ping = json.loads(_python(POOL_REPORT))
        self.assertEqual((size, overflow, timeout, recycle, pre_ping), (10, 5, 10, 1800, True))
        self.assertEqual(size + overflow, 15, "one API process can hold at most 15 connections")

    def test_the_cap_follows_the_environment(self):
        import json
        size, overflow, *_ = json.loads(_python(POOL_REPORT, DB_POOL_SIZE="2", DB_MAX_OVERFLOW="1"))
        self.assertEqual((size, overflow), (2, 1))

    def test_a_bad_value_stops_the_process_at_import(self):
        with self.assertRaises(AssertionError) as ctx:
            _python(POOL_REPORT, DB_POOL_SIZE="many")
        self.assertIn("ValueError", str(ctx.exception))

    def test_pool_timeout_is_explicit_and_shorter_than_the_default(self):
        source = (ROOT / "pipeline" / "db.py").read_text()
        self.assertRegex(source, r"pool_timeout=10")


class UtilityScripts(unittest.TestCase):
    def test_each_script_engine_is_capped_at_one_connection(self):
        for relative in CAPPED_SCRIPTS:
            calls = list(_create_engine_calls(ROOT / relative))
            self.assertEqual(len(calls), 1, relative)
            _, keywords = calls[0]
            self.assertEqual(ast.literal_eval(keywords["pool_size"]), 1, relative)
            self.assertEqual(ast.literal_eval(keywords["max_overflow"]), 0, relative)
            self.assertEqual(ast.literal_eval(keywords["pool_pre_ping"]), True, relative)

    def test_no_other_engine_in_the_repo_uses_the_default_pool(self):
        """Catches a new script that calls create_engine(DATABASE_URL) and silently gets 5 + 10 connections."""
        offenders = []
        for path in sorted(ROOT.rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            if set(path.relative_to(ROOT).parts) & SKIP_DIRS or relative in OTHER_ENGINES:
                continue
            for node, keywords in _create_engine_calls(path):
                ok = "pool_size" in keywords and "max_overflow" in keywords and ast.literal_eval(keywords["pool_size"]) <= 1 and ast.literal_eval(keywords["max_overflow"]) == 0
                if not ok:
                    offenders.append(f"{relative}:{node.lineno}")
        self.assertEqual(offenders, [], "give it pool_size=1, max_overflow=0 (or add it to OTHER_ENGINES with a reason)")

    def test_the_module_level_script_engine_really_has_one_slot(self):
        import importlib
        os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable")
        module = importlib.import_module("scripts.populate_test_niches")
        self.assertEqual((module.engine.pool.size(), module.engine.pool._max_overflow), (1, 0))

    def test_the_migration_engine_does_not_pool(self):
        self.assertIn("poolclass=NullPool", (ROOT / "pipeline" / "migration_settings.py").read_text())


class RoleConnectionLimits(unittest.TestCase):
    SQL = ROOT / "scripts" / "sql" / "role_connection_limits.sql"

    def _statements(self):
        return [line.strip() for line in self.SQL.read_text().splitlines() if line.strip() and not line.strip().startswith("--")]

    def test_the_file_sets_exactly_the_two_limits(self):
        self.assertEqual(self._statements(), ["BEGIN;", "ALTER ROLE mavlit_app CONNECTION LIMIT 40;", "ALTER ROLE mavlit_user CONNECTION LIMIT 40;", "COMMIT;"])

    def test_it_tells_the_reader_to_lower_the_owner_limit_later(self):
        text = self.SQL.read_text()
        self.assertRegex(text, r"LOWER IT TO 20")
        self.assertIn("NOT applied by anything in this repo", text)

    def test_the_limits_cover_the_api_pool_plus_a_restart_overlap(self):
        cap = 10 + 5 + 2   # API pool + the 2 of the long-job pool (BatchSessionLocal)
        self.assertGreaterEqual(40, 2 * cap + 6)   # old process draining + new process + headroom
        self.assertGreaterEqual(40, 2 * cap + 5)   # until the API moves to mavlit_app, mavlit_user carries the API too

    def test_nothing_in_the_repo_executes_it(self):
        for path in ROOT.rglob("*"):
            if path.suffix in (".py", ".sh", ".yml") and not (set(path.relative_to(ROOT).parts) & (SKIP_DIRS | {"tests"})):
                self.assertNotIn("role_connection_limits", path.read_text(errors="ignore"), str(path))


if __name__ == "__main__":
    unittest.main()
