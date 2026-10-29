"""
Runs scripts/deploy.sh in a sandbox (a throwaway git origin and clone, fake systemctl / journalctl / python / pip) to
check the deployment state handling. Nothing here touches a real service, database or server.

Run:  python -m pytest tests/test_deploy_script.py -q
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS_LOG"
if [ "$1" = "is-active" ]; then exit "${FAKE_ACTIVE_RC:-0}"; fi
if [ "$1" = "restart" ]; then exit "${FAKE_RESTART_RC:-0}"; fi
exit 0
"""
FAKE_PYTHON = """#!/usr/bin/env bash
echo "python $*" >> "$CALLS_LOG"
if [ "$*" = "-m pipeline.migrate" ]; then echo "migrate output line"; exit "${FAKE_MIGRATE_RC:-0}"; fi
exit 0
"""
FAKE_PIP = """#!/usr/bin/env bash
echo "pip $*" >> "$CALLS_LOG"
exit 0
"""
FAKE_JOURNALCTL = """#!/usr/bin/env bash
echo "journalctl $*" >> "$CALLS_LOG"
exit 0
"""


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class DeployScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin, self.app, self.dev = self.tmp / "origin.git", self.tmp / "app", self.tmp / "dev"
        _git(self.tmp, "init", "--bare", "-b", "main", str(origin))
        _git(self.tmp, "clone", str(origin), str(self.dev))
        self.push("README", "one\n", "first")
        _git(self.tmp, "clone", str(origin), str(self.app))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("python", FAKE_PYTHON), ("pip", FAKE_PIP), ("journalctl", FAKE_JOURNALCTL)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.calls = self.tmp / "calls.log"
        self.state = self.tmp / "state"
        self.env = {
            **os.environ,
            "MAVLIT_APP_DIR": str(self.app), "MAVLIT_SERVICE": "test.service",
            "SYSTEMCTL": str(bin_dir / "systemctl"), "JOURNALCTL": str(bin_dir / "journalctl"),
            "PYTHON_BIN": str(bin_dir / "python"), "PIP_BIN": str(bin_dir / "pip"),
            "LOCK_FILE": str(self.tmp / "lock"), "DEPLOY_STATE_DIR": str(self.state),
            "DEPLOY_RESTART_WAIT": "0", "DEPLOY_HEALTH_INTERVAL": "0", "DEPLOY_HEALTH_RETRIES": "2",
            "DEPLOY_HEALTH_CMD": 'exit "${FAKE_HEALTH_RC:-0}"', "CALLS_LOG": str(self.calls),
            "SECRET_FOR_TEST": "do-not-print-me",
        }
        self.env.pop("DEPLOY_RUN_MIGRATIONS", None)

    def push(self, name, content, message):
        (self.dev / name).write_text(content)
        _git(self.dev, "add", name)
        _git(self.dev, "commit", "-m", message)
        _git(self.dev, "push", "origin", "main")

    def run_deploy(self, migrations: bool = False, **extra) -> subprocess.CompletedProcess:
        env = {**self.env, **extra}
        if migrations:
            env["DEPLOY_RUN_MIGRATIONS"] = "1"
        result = subprocess.run(["bash", str(DEPLOY)], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotIn("do-not-print-me", result.stdout + result.stderr)
        return result

    def called(self, needle: str) -> bool:
        return self.calls.exists() and needle in self.calls.read_text()

    def head(self) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.app, capture_output=True, text=True, check=True).stdout.strip()

    def marker(self) -> str | None:
        path = self.state / "last_success_commit"
        return path.read_text().strip() if path.exists() else None

    # ---- default behaviour is unchanged -------------------------------------------------------------------------
    def test_default_up_to_date_exits_without_restarting(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0)
        self.assertIn("already up to date", result.stdout)
        self.assertFalse(self.called("restart"))

    def test_default_new_commit_restarts_and_never_migrates(self):
        self.push("README", "two\n", "second")
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.called("systemctl restart test.service"))
        self.assertFalse(self.called("pipeline.migrate"))
        self.assertFalse(self.state.exists(), "the default path must not create the state directory")
        self.assertIn("deployed ", result.stdout)

    def test_default_start_failure_still_fails(self):
        self.push("README", "two\n", "second")
        result = self.run_deploy(FAKE_ACTIVE_RC="3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed to start", result.stdout)

    def test_default_installs_requirements_when_they_change(self):
        self.push("requirements.txt", "x\n", "deps")
        self.run_deploy()
        self.assertTrue(self.called("pip install -r requirements.txt"))

    def test_the_opt_in_health_probe_reads_the_database_by_default(self):
        text = DEPLOY.read_text()
        self.assertIn("DEPLOY_HEALTH_URL:-http://127.0.0.1:8000/brand-niches", text)   # /health never touches the database

    def test_the_flag_must_be_0_or_1(self):
        result = self.run_deploy(DEPLOY_RUN_MIGRATIONS="2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be 0 or 1", result.stderr)

    # ---- migrations enabled -------------------------------------------------------------------------------------
    def test_full_success_writes_the_marker_last(self):
        self.push("README", "two\n", "second")
        result = self.run_deploy(migrations=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.calls.read_text()
        self.assertLess(log.index("pipeline.migrate"), log.index("systemctl restart"))
        self.assertEqual(self.marker(), self.head())
        self.assertIn("migrations applied, service healthy", result.stdout)

    def test_a_failed_migration_does_not_restart_or_mark_the_deploy(self):
        self.push("README", "two\n", "second")
        result = self.run_deploy(migrations=True, FAKE_MIGRATE_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("migrations failed", result.stderr)
        self.assertIn("NOT restarted", result.stderr)
        self.assertFalse(self.called("systemctl restart"))
        self.assertIsNone(self.marker())

    def test_retry_at_the_same_commit_runs_the_migration_again(self):
        self.push("README", "two\n", "second")
        self.assertEqual(self.run_deploy(migrations=True, FAKE_MIGRATE_RC="1").returncode, 1)
        self.assertNotEqual(self.head(), "", "git HEAD has already advanced")
        self.calls.write_text("")
        retry = self.run_deploy(migrations=True)   # git now says "up to date": the migration must still run
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertNotIn("already up to date", retry.stdout)
        self.assertTrue(self.called("pipeline.migrate"))
        self.assertTrue(self.called("systemctl restart"))
        self.assertEqual(self.marker(), self.head())

    def test_default_path_would_have_skipped_it(self):   # documents why the opt-in path exists
        self.push("README", "two\n", "second")
        self.run_deploy(FAKE_ACTIVE_RC="0")
        self.assertIn("already up to date", self.run_deploy().stdout)

    def test_a_failed_health_check_leaves_no_marker_and_a_retry_completes(self):
        self.push("README", "two\n", "second")
        failed = self.run_deploy(migrations=True, FAKE_HEALTH_RC="1")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("health check still fails", failed.stderr)
        self.assertIsNone(self.marker())
        ok = self.run_deploy(migrations=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(self.marker(), self.head())

    def test_an_inactive_service_after_restart_is_a_failure(self):
        self.push("README", "two\n", "second")
        result = self.run_deploy(migrations=True, FAKE_ACTIVE_RC="3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not active", result.stderr)
        self.assertIsNone(self.marker())

    def test_a_failed_restart_is_a_failure(self):
        self.push("README", "two\n", "second")
        result = self.run_deploy(migrations=True, FAKE_RESTART_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(self.marker())

    def test_a_verified_commit_is_not_redeployed(self):
        self.push("README", "two\n", "second")
        self.run_deploy(migrations=True)
        self.calls.write_text("")
        again = self.run_deploy(migrations=True)
        self.assertEqual(again.returncode, 0)
        self.assertIn("already deployed and verified", again.stdout)
        self.assertFalse(self.called("pipeline.migrate"))
        self.assertFalse(self.called("restart"))

    def test_a_verified_commit_with_a_dead_service_runs_everything_again(self):
        self.push("README", "two\n", "second")
        self.run_deploy(migrations=True)
        self.calls.write_text("")
        again = self.run_deploy(migrations=True, FAKE_ACTIVE_RC="3")   # is-active fails before and after the restart
        self.assertEqual(again.returncode, 1)
        self.assertTrue(self.called("pipeline.migrate"))
        self.assertTrue(self.called("systemctl restart"))

    def test_requirements_changes_since_the_last_verified_deploy_are_installed(self):
        self.push("README", "two\n", "second")
        self.run_deploy(migrations=True)
        self.push("requirements.txt", "x\n", "deps")
        self.assertEqual(self.run_deploy(migrations=True, FAKE_MIGRATE_RC="1").returncode, 1)   # fails after pip, before the marker
        self.calls.write_text("")
        self.assertEqual(self.run_deploy(migrations=True).returncode, 0)
        self.assertTrue(self.called("pip install -r requirements.txt"), "the retry must still install what changed since the last verified deploy")


if __name__ == "__main__":
    unittest.main()
