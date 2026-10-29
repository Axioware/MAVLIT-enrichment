"""
Repository hygiene (checklist 2.11): no database dump and no Apify token may be tracked. Reads tracked files only; contacts nothing.

Run:  python -m pytest tests/test_repo_hygiene.py -q
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUMP_SUFFIXES = (".dump", ".sql.gz", ".pgdump")
APIFY_TOKEN = re.compile(rb"apify_api_" + rb"[A-Za-z0-9]{16,}")   # split so this file does not match itself
MAX_BYTES = 5_000_000


def _tracked() -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True)
    if result.returncode:
        raise unittest.SkipTest("not a git checkout")
    return [p for p in result.stdout.decode().split("\0") if p]


class TrackedFiles(unittest.TestCase):
    def test_no_database_dump_is_tracked(self):
        offenders = [p for p in _tracked() if p.lower().endswith(DUMP_SUFFIXES)]
        self.assertEqual(offenders, [], "database dumps must never be committed")

    def test_no_apify_token_in_a_tracked_file(self):
        offenders = []
        for rel in _tracked():
            path = ROOT / rel
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            if APIFY_TOKEN.search(path.read_bytes()):
                offenders.append(rel)   # the file name only, never the token
        self.assertEqual(offenders, [], "an Apify token is in a tracked file: rotate it first (SECURITY.md)")

    def test_only_the_two_env_templates_are_tracked(self):
        env_files = sorted(p for p in _tracked() if Path(p).name.startswith(".env"))
        self.assertEqual(env_files, [".env.example", ".env.migrate.example"])

    def test_gitleaks_is_configured(self):
        self.assertIn("apify_api_", (ROOT / ".gitleaks.toml").read_text())
        self.assertIn("gitleaks", (ROOT / ".github" / "workflows" / "secret-scan.yml").read_text())
        self.assertIn("gitleaks", (ROOT / ".pre-commit-config.yaml").read_text())


if __name__ == "__main__":
    unittest.main()
