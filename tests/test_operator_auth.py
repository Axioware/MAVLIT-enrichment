"""
Request-level tests of operator access (api/operator.py): the endpoints that start pipeline jobs or write in bulk, their status
routes, and /admin. No database is used and nothing external is called: the background jobs and the bulk writer are replaced by mocks,
and the /admin scenarios run in fresh interpreters because the admin gate is decided when the app is imported.

Run:  python -m pytest tests/test_operator_auth.py -q
"""

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
os.environ["RUN_MIGRATIONS"] = ""
os.environ.pop("MIGRATION_DATABASE_URL", None)
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from api import operator  # noqa: E402

KEY = "correct-operator-passkey-0123456789"
SEED = {"niche": "fashion"}
ENRICH = {"brand_ids": [1], "steps": ["youtube"]}
SCORE = {"limit": 5}
BULK = {"urls": ["https://www.instagram.com/someone/"], "niche": "music"}

# (method, path, json body) for every operator action; the status routes get a job id appended by the tests
ACTIONS = [("POST", "/seed", SEED), ("POST", "/enrich/signals", ENRICH), ("POST", "/score/brands", SCORE), ("POST", "/content-creator-re/bulk-add", BULK)]
STATUS = ["/seed/status/", "/enrich/signals/status/", "/score/brands/status/"]


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class OperatorEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api.app as api_app
        from fastapi.testclient import TestClient
        cls.api_app = api_app
        cls.client = TestClient(api_app.app)   # not entered as a context manager: no lifespan, no database

    def setUp(self):
        for patcher in (
            mock.patch.object(config, "ADMIN_PASSKEY", KEY),
            mock.patch.object(self.api_app, "_run_seed_job"), mock.patch.object(self.api_app, "_run_enrich_job"), mock.patch.object(self.api_app, "_run_scoring_job"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bulk = mock.patch.object(self.api_app, "add_content_creator_re", return_value=(1, 0))
        self.bulk_mock = self.bulk.start()
        self.addCleanup(self.bulk.stop)
        self.api_app._jobs.clear()

    def _nothing_ran(self):
        self.bulk_mock.assert_not_called()
        for name in ("_run_seed_job", "_run_enrich_job", "_run_scoring_job"):
            getattr(self.api_app, name).assert_not_called()
        self.assertEqual(self.api_app._jobs, {}, "no job may have been created")

    # ---- the four actions -------------------------------------------------------------------------------------------------
    def test_without_credentials_every_action_is_refused_and_does_nothing(self):
        for method, path, body in ACTIONS:
            response = self.client.request(method, path, json=body)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.headers.get("www-authenticate"), "Bearer", path)
        self._nothing_ran()

    def test_invalid_credentials_are_refused(self):
        bad_headers = [
            {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {KEY}x"}, {"Authorization": f"Bearer {KEY[:-1]}"}, {"Authorization": f"Bearer {KEY.upper()}"},
            {"Authorization": "Bearer"}, {"Authorization": "Bearer "}, {"Authorization": "Bearer    "}, {"Authorization": ""}, {"Authorization": KEY},
            {"Authorization": f"Basic {KEY}"}, {"Authorization": f"Token {KEY}"}, {"Authorization": f"Bearer\\t{KEY}"},
            {"X-Admin-Passkey": KEY}, {"X-Operator-Key": KEY}, {"Cookie": f"access_token={KEY}"}, {"Cookie": f"admin_authenticated=true; session={KEY}"},
        ]
        for headers in bad_headers:
            for method, path, body in ACTIONS:
                self.assertEqual(self.client.request(method, path, json=body, headers=headers).status_code, 401, f"{path} with {list(headers)}")
        self.assertEqual(self.client.post("/seed", json=SEED, params={"authorization": KEY, "key": KEY, "passkey": KEY}).status_code, 401, "a query-string credential is not accepted")
        self._nothing_ran()

    def test_a_correct_credential_works_for_every_action(self):
        self.assertEqual(self.client.post("/seed", json=SEED, headers=bearer(KEY)).status_code, 200)
        self.assertEqual(self.client.post("/enrich/signals", json=ENRICH, headers=bearer(KEY)).status_code, 200)
        self.assertEqual(self.client.post("/score/brands", json=SCORE, headers=bearer(KEY)).status_code, 200)
        response = self.client.post("/content-creator-re/bulk-add", json=BULK, headers={"Authorization": f"bearer {KEY}"})   # the scheme name is case-insensitive
        self.assertEqual((response.status_code, response.json()["added"]), (200, 1))
        self.api_app._run_seed_job.assert_called_once()
        self.bulk_mock.assert_called_once()

    def test_the_credential_may_contain_any_characters(self):
        for odd in ("with space and 'quotes' \"x\" !@#$%^&*()", "a" * 5000, "=padding=="):   # HTTP header values are ASCII/latin-1: use an ASCII passkey
            with mock.patch.object(config, "ADMIN_PASSKEY", odd):
                self.assertEqual(self.client.post("/seed", json=SEED, headers=bearer(odd)).status_code, 200, odd[:20])
                self.assertEqual(self.client.post("/seed", json=SEED, headers=bearer(odd + "x")).status_code, 401)

    # ---- status routes ------------------------------------------------------------------------------------------------------
    def test_status_routes_follow_the_same_policy(self):
        job_ids = {
            "/seed/status/": self.client.post("/seed", json=SEED, headers=bearer(KEY)).json()["job_id"],
            "/enrich/signals/status/": self.client.post("/enrich/signals", json=ENRICH, headers=bearer(KEY)).json()["job_id"],
            "/score/brands/status/": self.client.post("/score/brands", json=SCORE, headers=bearer(KEY)).json()["job_id"],
        }
        for prefix, job_id in job_ids.items():
            self.assertEqual(self.client.get(prefix + job_id).status_code, 401, "a real job id is not enough")
            self.assertEqual(self.client.get(prefix + job_id, headers=bearer("wrong")).status_code, 401)
            self.assertEqual(self.client.get(prefix + job_id, headers=bearer(KEY)).status_code, 200)
            self.assertEqual(self.client.get(prefix + "does-not-exist", headers=bearer(KEY)).status_code, 404)
            self.assertEqual(self.client.get(prefix + "does-not-exist").status_code, 401, "no job-existence oracle without credentials")

    def test_a_failed_job_detail_is_only_visible_to_the_operator(self):
        job_id = self.client.post("/seed", json=SEED, headers=bearer(KEY)).json()["job_id"]
        self.api_app._jobs[job_id].update({"status": "error", "error": "psycopg2.OperationalError: could not connect to server at 10.0.0.5"})
        anonymous = self.client.get(f"/seed/status/{job_id}")
        self.assertNotIn("psycopg2", anonymous.text)
        self.assertNotIn("10.0.0.5", anonymous.text)

    # ---- fail closed when nothing is configured ----------------------------------------------------------------------------------
    def test_no_configured_passkey_means_nobody_gets_in(self):
        for configured in (None, "", "   ", "\n\t"):
            with mock.patch.object(config, "ADMIN_PASSKEY", configured):
                for headers in ({}, bearer(""), bearer("anything"), bearer("   "), bearer(str(configured))):
                    for method, path, body in ACTIONS:
                        self.assertEqual(self.client.request(method, path, json=body, headers=headers).status_code, 403, f"{path} {configured!r}")
                    for prefix in STATUS:
                        self.assertEqual(self.client.get(prefix + "x", headers=headers).status_code, 403)
        self._nothing_ran()

    # ---- other methods, paths and credentials ------------------------------------------------------------------------------------
    def test_other_methods_and_path_variants_never_succeed_without_credentials(self):
        paths = [path for _, path, _ in ACTIONS] + [prefix + "abc" for prefix in STATUS]
        for path in paths:
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                response = self.client.request(method, path, json=SEED if method in ("POST", "PUT", "PATCH", "DELETE") else None)
                self.assertGreaterEqual(response.status_code, 400, f"{method} {path} -> {response.status_code}")
            for variant in (path + "/", path.upper(), path + "?x=1", "//" + path.lstrip("/")):
                self.assertGreaterEqual(self.client.post(variant, json=SEED).status_code, 400, variant)
        self._nothing_ran()

    def test_a_creator_session_is_not_an_operator_credential(self):
        from api.auth import _make_jwt
        cookies = {"access_token": _make_jwt(1, "creator@example.com")}
        for method, path, body in ACTIONS:
            self.assertEqual(self.client.request(method, path, json=body, cookies=cookies).status_code, 401, path)
        self._nothing_ran()

    def test_the_operator_credential_is_not_a_creator_session(self):
        for path in ("/creator-profile/me", "/auth/me", "/matches/me", "/saved-brands/me", "/pitches/me", "/rate-intelligence/latest"):
            self.assertEqual(self.client.get(path, headers=bearer(KEY)).status_code, 401, path)

    def test_every_operator_route_declares_the_dependency(self):
        """Guards against a future route being added without it: all of these paths must carry require_operator."""
        from fastapi.routing import APIRoute
        protected = {path for _, path, _ in ACTIONS} | {prefix + "{job_id}" for prefix in STATUS}
        found = set()
        for route in self.api_app.app.routes:
            if isinstance(route, APIRoute) and route.path in protected:
                self.assertIn(operator.require_operator, [d.call for d in route.dependant.dependencies], route.path)
                found.add(route.path)
        self.assertEqual(found, protected)

    # ---- public and creator routes keep working ----------------------------------------------------------------------------------
    def test_public_routes_need_no_operator_credential(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/signin").status_code, 200)
        self.assertEqual(self.client.post("/early-access", json={"email": "not-an-email"}).status_code, 422)   # reaches its own validation
        self.assertIn(self.client.get("/login", follow_redirects=False).status_code, (301, 302, 307, 308))
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 307)                      # creators are sent to sign in, not refused
        self.assertEqual(self.client.get("/creator-profile/me").status_code, 401)                            # creator auth unchanged

    def test_creator_login_does_not_involve_the_operator_passkey(self):
        from types import SimpleNamespace
        from api.auth import get_db

        fake_user = SimpleNamespace(
            email="a@b.co",
            password_hash="stored-hash",
            is_active=True,
        )

        fake_query = mock.Mock()
        fake_query.filter.return_value.first.return_value = fake_user
        fake_db = mock.Mock()
        fake_db.query.return_value = fake_query

        self.api_app.app.dependency_overrides[get_db] = lambda: fake_db
        self.addCleanup(self.api_app.app.dependency_overrides.pop, get_db, None)

        with mock.patch.object(config, "ADMIN_PASSKEY", None), \
             mock.patch("api.auth.verify_password", return_value=False), \
             mock.patch("api.auth.login_limits.locked_for_seconds", return_value=0), \
             mock.patch("api.auth.login_limits.record_failure"):   # the lockout store is tested in tests/test_access_control.py
            response = self.client.post(
                "/auth/login",
                json={"email": "a@b.co", "password": "x"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid email or password")
        fake_db.query.assert_called_once()

    # ---- the helper itself ------------------------------------------------------------------------------------------------------
    def test_passkey_matches_is_strict(self):
        self.assertTrue(operator.passkey_matches(KEY))
        with mock.patch.object(config, "ADMIN_PASSKEY", "pässkey-ünïcode"):   # the /admin login form can carry non-ASCII; the Bearer header cannot
            self.assertTrue(operator.passkey_matches("pässkey-ünïcode"))
            self.assertFalse(operator.passkey_matches("passkey-unicode"))
        for bad in (None, "", " ", KEY + " ", " " + KEY, KEY.lower() if KEY != KEY.lower() else KEY.upper()):
            self.assertFalse(operator.passkey_matches(bad), repr(bad))
        with mock.patch.object(config, "ADMIN_PASSKEY", ""):
            self.assertFalse(operator.passkey_matches(""))
            self.assertFalse(operator.passkey_matches("x"))

    def test_nothing_secret_is_logged(self):
        with self.assertLogs("api.operator", level="WARNING") as logs, mock.patch.object(config, "ADMIN_PASSKEY", ""):
            self.client.post("/seed", json=SEED, headers=bearer("attempted-secret-value"))
        self.assertNotIn("attempted-secret-value", "\n".join(logs.output))


def _run_admin_scenario(env: dict, script: str) -> dict:
    """A fresh interpreter, because whether /admin is protected is decided when the app is imported."""
    full_env = {**os.environ, "DATABASE_URL": "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable", "JWT_SECRET": "test-jwt-secret",
                "RUN_MIGRATIONS": "", "ADMIN_PASSKEY": "", "ADMIN_ALLOW_UNAUTHENTICATED": "", "ENVIRONMENT": "development", **env}
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


ADMIN_PROBE = """
import json, os
import api.app as A
from fastapi.testclient import TestClient
c = TestClient(A.app, follow_redirects=False)
def code(r): return r.status_code
out = {"anonymous": code(c.get("/admin/"))}
for label, password in (("empty", ""), ("wrong", "wrong"), ("the_unset_default", "None"), ("blank", "   "), ("upper", os.environ.get("PROBE_KEY", "").upper() or "X")):
    r = c.post("/admin/login", data={"username": "", "password": password})
    out["login_" + label] = code(r)
    out["after_" + label] = code(c.get("/admin/"))
key = os.environ.get("PROBE_KEY")
if key:
    r = c.post("/admin/login", data={"username": "", "password": key})
    out["login_correct"] = code(r)
    out["after_correct"] = code(c.get("/admin/"))
    c.get("/admin/logout")
    out["after_logout"] = code(c.get("/admin/"))
print(json.dumps(out))
"""


class AdminGate(unittest.TestCase):
    CLOSED = {"anonymous": 302, "after_empty": 302, "after_wrong": 302, "after_the_unset_default": 302, "after_blank": 302, "after_upper": 302}

    def _assert_closed(self, outcome):
        for key, expected in self.CLOSED.items():
            self.assertEqual(outcome[key], expected, f"{key}: {outcome}")

    def test_unset_passkey_closes_admin(self):
        self._assert_closed(_run_admin_scenario({"ADMIN_PASSKEY": ""}, ADMIN_PROBE))

    def test_blank_passkey_closes_admin(self):
        for blank in ("   ", "\t", "\n"):
            self._assert_closed(_run_admin_scenario({"ADMIN_PASSKEY": blank}, ADMIN_PROBE))

    def test_a_blank_password_never_matches_a_blank_passkey(self):
        outcome = _run_admin_scenario({"ADMIN_PASSKEY": "   "}, ADMIN_PROBE)
        self.assertEqual(outcome["after_blank"], 302)
        self.assertEqual(outcome["after_empty"], 302)

    def test_dev_escape_hatch_opens_admin_only_outside_production(self):
        opened = _run_admin_scenario({"ADMIN_PASSKEY": "", "ADMIN_ALLOW_UNAUTHENTICATED": "1"}, ADMIN_PROBE)
        self.assertEqual(opened["anonymous"], 200, "explicitly opted-in development use")
        self._assert_closed(_run_admin_scenario({"ADMIN_PASSKEY": "", "ADMIN_ALLOW_UNAUTHENTICATED": "1", "ENVIRONMENT": "production"}, ADMIN_PROBE))
        for off in ("0", "false", "no", "", "anything-else"):
            self._assert_closed(_run_admin_scenario({"ADMIN_PASSKEY": "", "ADMIN_ALLOW_UNAUTHENTICATED": off}, ADMIN_PROBE))

    def test_the_hatch_does_not_open_admin_when_a_passkey_is_set(self):
        outcome = _run_admin_scenario({"ADMIN_PASSKEY": KEY, "ADMIN_ALLOW_UNAUTHENTICATED": "1", "PROBE_KEY": KEY}, ADMIN_PROBE)
        self.assertEqual(outcome["anonymous"], 302)

    def test_with_a_passkey_only_the_exact_value_logs_in(self):
        outcome = _run_admin_scenario({"ADMIN_PASSKEY": KEY, "PROBE_KEY": KEY}, ADMIN_PROBE)
        self._assert_closed({k: outcome[k] for k in self.CLOSED})
        self.assertEqual(outcome["login_correct"], 302)
        self.assertEqual(outcome["after_correct"], 200, outcome)
        self.assertEqual(outcome["after_logout"], 302, "logging out closes the session")

    def test_the_operator_endpoints_stay_closed_in_production_without_a_passkey(self):
        script = """
import json
import api.app as A
from fastapi.testclient import TestClient
c = TestClient(A.app)
print(json.dumps({"seed": c.post("/seed", json={"niche": "x"}, headers={"Authorization": "Bearer "}).status_code, "bulk": c.post("/content-creator-re/bulk-add", json={"urls": ["https://instagram.com/x"], "niche": "y"}).status_code}))
"""
        self.assertEqual(_run_admin_scenario({"ADMIN_PASSKEY": "", "ENVIRONMENT": "production", "ADMIN_ALLOW_UNAUTHENTICATED": "1"}, script), {"seed": 403, "bulk": 403})


if __name__ == "__main__":
    unittest.main()
