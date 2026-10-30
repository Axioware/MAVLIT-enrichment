"""
Phase 4 (batch A) of the security checklist: token revocation, strict token claims, secret strength, cookie attributes, CSRF origin check,
attempt limits and lockout for /admin and the operator endpoints, password hashing. The route tests that need a database use a disposable
local PostgreSQL (MAVLIT_TEST_PG_URL, same rules as tests/test_migration_postgres.py); everything else contacts nothing.

Run:  python -m pytest tests/test_phase4_auth.py -q
"""

import base64
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-0123456789-abcdefghijklmnop")
os.environ.setdefault("ADMIN_SESSION_SECRET", "test-admin-session-secret-0123456789-abcdefgh")
os.environ["RUN_MIGRATIONS"] = "0"
os.environ.pop("MIGRATION_DATABASE_URL", None)
sys.path.insert(0, str(ROOT))

import bcrypt  # noqa: E402
from fastapi import Response  # noqa: E402
from jose import jwt  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import config  # noqa: E402

JWT_SECRET = os.environ["JWT_SECRET"]
PASSWORD = "correct horse battery"
GOOD_32 = "g" * 32
GOOD_32_B = "h" * 32


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _claims(**override):
    now = int(time.time())
    claims = {"sub": "1", "email": "a@example.test", "iat": now, "exp": now + 3600, "tv": 0}
    claims.update(override)
    return {k: v for k, v in claims.items() if v is not None}


def _client(api_app, **kw):
    from fastapi.testclient import TestClient
    return TestClient(api_app.app, raise_server_exceptions=False, **kw)


def _config_import(**env):
    """Import config in a fresh interpreter with exactly this environment (no .env): returns (returncode, stderr)."""
    full_env = {k: v for k, v in os.environ.items() if k not in ("JWT_SECRET", "ADMIN_SESSION_SECRET", "ADMIN_PASSKEY", "OPERATOR_PASSKEY",
                                                                  "ENVIRONMENT", "COOKIE_SAMESITE", "COOKIE_DOMAIN", "ACCESS_TOKEN_DAYS")}
    full_env.update({"DATABASE_URL": "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable", **env})
    prelude = "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
    result = subprocess.run([sys.executable, "-c", prelude + "import config; print(config.COOKIE_SAMESITE)"], cwd=ROOT, env=full_env,
                            capture_output=True, text=True, timeout=120)
    return result.returncode, result.stderr, result.stdout.strip()


VALID = {"JWT_SECRET": GOOD_32, "ADMIN_SESSION_SECRET": GOOD_32_B}


class SecretsRefuseStartup(unittest.TestCase):
    def test_valid_settings_start(self):
        self.assertEqual(_config_import(**VALID)[0], 0)

    def test_short_or_missing_secrets_refuse_to_start_without_printing_them(self):
        cases = [
            ({"JWT_SECRET": "short-secret-value", "ADMIN_SESSION_SECRET": GOOD_32_B}, "JWT_SECRET must be at least 32"),
            ({"ADMIN_SESSION_SECRET": GOOD_32_B}, "JWT_SECRET is not set"),
            ({"JWT_SECRET": GOOD_32}, "ADMIN_SESSION_SECRET is not set"),
            ({"JWT_SECRET": GOOD_32, "ADMIN_SESSION_SECRET": "short-session-secret"}, "ADMIN_SESSION_SECRET must be at least 32"),
            ({"JWT_SECRET": GOOD_32, "ADMIN_SESSION_SECRET": GOOD_32}, "must differ from JWT_SECRET"),
        ]
        for env, message in cases:
            with self.subTest(message):
                code, stderr, _ = _config_import(**env)
                self.assertNotEqual(code, 0)
                self.assertIn(message, stderr)
                for value in env.values():
                    self.assertNotIn(value, stderr, "the secret itself must never be printed")

    def test_passkeys_need_16_characters_in_production_only(self):
        short = "only-15-chars-x"
        self.assertEqual(len(short), 15)
        for name in ("ADMIN_PASSKEY", "OPERATOR_PASSKEY"):
            with self.subTest(name):
                code, stderr, _ = _config_import(**VALID, **{name: short}, ENVIRONMENT="production")
                self.assertNotEqual(code, 0)
                self.assertIn(f"{name} must be at least 16", stderr)
                self.assertNotIn(short, stderr)
                self.assertEqual(_config_import(**VALID, **{name: short}, ENVIRONMENT="development")[0], 0)
                self.assertEqual(_config_import(**VALID, **{name: "x" * 16}, ENVIRONMENT="production")[0], 0)
                self.assertEqual(_config_import(**VALID, **{name: ""}, ENVIRONMENT="production")[0], 0, "unset stays closed, not refused")

    def test_cookie_settings(self):
        self.assertEqual(_config_import(**VALID)[2], "lax", "the default is Lax")
        self.assertEqual(_config_import(**VALID, COOKIE_SAMESITE="Strict")[2], "strict")
        self.assertEqual(_config_import(**VALID, COOKIE_SAMESITE="none", ENVIRONMENT="production")[2], "none")
        self.assertIn("lax, strict or none", _config_import(**VALID, COOKIE_SAMESITE="sometimes")[1])
        self.assertIn("needs ENVIRONMENT=production", _config_import(**VALID, COOKIE_SAMESITE="none")[1])


class TokenValidation(unittest.TestCase):
    """The claims are checked before any database access, so none of these need one."""

    def setUp(self):
        import api.app as api_app
        self.api_app = api_app

    def me(self, token):
        client = _client(self.api_app)
        client.cookies.set("access_token", token)
        return client.get("/auth/me")

    def test_unsigned_and_other_algorithms_are_refused(self):
        claims = _claims()
        none_token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."
        self.assertEqual(self.me(none_token).status_code, 401)
        self.assertEqual(self.me(jwt.encode(claims, JWT_SECRET, algorithm="HS384")).status_code, 401)
        self.assertEqual(self.me(jwt.encode(claims, JWT_SECRET, algorithm="HS512")).status_code, 401)
        self.assertEqual(self.me(jwt.encode(claims, "another-secret-" * 4, algorithm="HS256")).status_code, 401)

    def test_exp_sub_and_iat_are_required(self):
        for missing in ("exp", "sub", "iat"):
            with self.subTest(missing):
                token = jwt.encode(_claims(**{missing: None}), JWT_SECRET, algorithm="HS256")
                response = self.me(token)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["detail"], "Invalid or expired token")

    def test_an_expired_token_is_refused(self):
        self.assertEqual(self.me(jwt.encode(_claims(exp=int(time.time()) - 10), JWT_SECRET, algorithm="HS256")).status_code, 401)

    def test_a_malformed_sub_or_tv_is_401_never_500(self):
        for bad in ({"sub": "abc"}, {"sub": ""}, {"sub": "1.5"}, {"sub": [1]}, {"sub": {"a": 1}}, {"tv": "x"}, {"tv": [1]}):
            with self.subTest(bad):
                self.assertEqual(self.me(jwt.encode(_claims(**bad), JWT_SECRET, algorithm="HS256")).status_code, 401)

    def test_garbage_tokens_are_401(self):
        for token in ("", "abc", "a.b.c", "x" * 500):
            self.assertEqual(self.me(token).status_code, 401)


class CookieAttributes(unittest.TestCase):
    @staticmethod
    def _attributes(response: Response) -> dict:
        header = response.headers["set-cookie"]
        parts = [p.strip() for p in header.split(";")]
        attrs = {p.split("=", 1)[0].lower(): (p.split("=", 1)[1] if "=" in p else True) for p in parts[1:]}
        for transient in ("max-age", "expires"):
            attrs.pop(transient, None)
        return attrs

    def test_defaults_are_lax_host_only_httponly(self):
        import api.auth as auth
        response = Response()
        auth._set_login_cookie(response, "token")
        attrs = self._attributes(response)
        self.assertEqual(attrs, {"path": "/", "httponly": True, "samesite": "lax"})
        self.assertIn("Max-Age=2592000", response.headers["set-cookie"], "30 days by default")

    def test_jwt_exp_and_cookie_max_age_match_and_follow_one_setting(self):
        import api.auth as auth
        for days in (30, 7, 90):
            with self.subTest(days=days), mock.patch.object(config, "ACCESS_TOKEN_DAYS", days):
                response = Response()
                token = auth._make_jwt(1, "a@example.test", 0)
                auth._set_login_cookie(response, token)
                claims = jwt.get_unverified_claims(token)
                max_age = int(self._attributes_with_age(response)["max-age"])
                self.assertEqual(claims["exp"] - claims["iat"], max_age)
                self.assertEqual(max_age, days * 86400)

    def test_access_token_days_setting(self):
        self.assertEqual(config.ACCESS_TOKEN_DAYS, 30)
        script_ok = _config_import(**VALID, ACCESS_TOKEN_DAYS="14")
        self.assertEqual(script_ok[0], 0)
        for bad in ("abc", "0", "-3", "400"):
            rc, err, _ = _config_import(**VALID, ACCESS_TOKEN_DAYS=bad)
            self.assertNotEqual(rc, 0, bad)
            self.assertIn("ACCESS_TOKEN_DAYS", err)

    @staticmethod
    def _attributes_with_age(response: Response) -> dict:
        parts = [p.strip() for p in response.headers["set-cookie"].split(";")]
        return {p.split("=", 1)[0].lower(): (p.split("=", 1)[1] if "=" in p else True) for p in parts[1:]}

    def test_the_delete_uses_identical_attributes(self):
        import api.auth as auth
        for patch in ({"COOKIE_SAMESITE": "lax"}, {"COOKIE_SAMESITE": "strict", "COOKIE_DOMAIN": ".example.test", "IS_PRODUCTION": True}):
            with self.subTest(patch), mock.patch.multiple(config, **patch):
                set_, cleared = Response(), Response()
                auth._set_login_cookie(set_, "token")
                auth._clear_login_cookie(cleared)
                self.assertEqual(self._attributes(set_), self._attributes(cleared))
                self.assertIn("Max-Age=0", cleared.headers["set-cookie"])
        with mock.patch.multiple(config, COOKIE_SAMESITE="none", COOKIE_DOMAIN=None, IS_PRODUCTION=True):
            response = Response()
            auth._set_login_cookie(response, "token")
            self.assertEqual(self._attributes(response), {"path": "/", "httponly": True, "secure": True, "samesite": "none"})

    def test_logout_route_clears_with_the_same_attributes(self):
        import api.app as api_app
        with mock.patch.multiple(config, COOKIE_SAMESITE="strict", COOKIE_DOMAIN=".example.test", IS_PRODUCTION=True):
            header = _client(api_app).post("/auth/logout").headers["set-cookie"].lower()
        for part in ("domain=.example.test", "samesite=strict", "secure", "httponly", "path=/", "max-age=0"):
            self.assertIn(part, header)


class CsrfOriginCheck(unittest.TestCase):
    def setUp(self):
        import api.app as api_app
        self.api_app = api_app
        self.client = _client(api_app)

    def post(self, path="/auth/logout", **headers):
        return self.client.post(path, headers=headers)

    def test_a_cross_origin_post_is_refused_and_a_same_origin_one_works(self):
        evil = self.post(Origin="https://evil.example")
        self.assertEqual(evil.status_code, 403)
        self.assertNotIn("set-cookie", evil.headers)
        self.assertEqual(self.post(Origin="http://testserver").status_code, 200)
        self.assertEqual(self.post(Origin="http://testserver/").status_code, 200)
        self.assertEqual(self.post(Origin="null").status_code, 403)
        self.assertEqual(self.post(Origin="https://testserver.evil.example").status_code, 403)

    def test_every_unsafe_method_is_checked_and_safe_ones_are_not(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.client.request(method, "/auth/logout", headers={"Origin": "https://evil.example"}).status_code, 403, method)
        self.assertNotEqual(self.client.get("/health", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertNotEqual(self.client.options("/auth/logout", headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_configured_frontends_are_allowed(self):
        with mock.patch.object(self.api_app, "FRONTEND_ORIGINS", ["https://app.example.test"]):
            self.assertEqual(self.post(Origin="https://app.example.test").status_code, 200)
            self.assertEqual(self.post(Origin="https://app.example.test/").status_code, 200)
            self.assertEqual(self.post(Origin="https://other.example.test").status_code, 403)

    def test_without_an_origin_sec_fetch_site_decides(self):
        self.assertEqual(self.post(**{"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        for value in ("same-origin", "same-site", "none"):
            self.assertEqual(self.post(**{"Sec-Fetch-Site": value}).status_code, 200, value)
        self.assertEqual(self.post().status_code, 200, "no browser headers at all: curl or a server")

    def test_exempt_routes(self):
        evil = {"Origin": "https://evil.example"}
        # public sign-up (called from the landing page on another site): the honeypot answers before the database is touched
        body = {"email": "x@example.test", "handle": "x", "platform": "instagram", "niche": "beauty", "followers": "under10k", "website": "bot"}
        self.assertEqual(self.client.post("/early-access", json=body, headers=evil).status_code, 201)
        # operator endpoints use a Bearer passkey, not a cookie: refused for lack of credentials (401/403), never by the origin check
        for path in ("/seed", "/enrich/signals", "/score/brands", "/content-creator-re/bulk-add"):
            status = self.client.post(path, json={}, headers=evil).status_code
            self.assertIn(status, (401, 403, 422), path)
            self.assertNotEqual(self.client.post(path, json={}, headers=evil).json().get("detail"), "Cross-site request refused", path)
        # health*: exempt (a POST there is simply not allowed, 405, but never the origin check)
        self.assertEqual(self.client.post("/health", headers=evil).status_code, 405)


class OperatorSeparation(unittest.TestCase):
    def setUp(self):
        import api.app as api_app
        self.api_app = api_app
        self.client = _client(api_app)
        patcher = mock.patch("api.attempt_limits.SessionLocal", side_effect=__import__("sqlalchemy").exc.OperationalError("x", {}, Exception("down")))
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, key):
        with mock.patch.object(self.api_app, "add_content_creator_re", return_value=(1, 0)):
            return self.client.post("/content-creator-re/bulk-add", json={"urls": ["https://www.instagram.com/someone/"], "niche": "Beauty"},
                                    headers={"Authorization": f"Bearer {key}"})

    def test_without_operator_passkey_the_admin_passkey_works_as_before(self):
        with mock.patch.multiple(config, ADMIN_PASSKEY="admin-passkey-0123456789", OPERATOR_PASSKEY=""):
            self.assertEqual(self.call("admin-passkey-0123456789").status_code, 200)
            self.assertEqual(self.call("wrong").status_code, 401)

    def test_with_an_operator_passkey_only_that_one_works(self):
        with mock.patch.multiple(config, ADMIN_PASSKEY="admin-passkey-0123456789", OPERATOR_PASSKEY="operator-passkey-0123456789"):
            self.assertEqual(self.call("operator-passkey-0123456789").status_code, 200)
            self.assertEqual(self.call("admin-passkey-0123456789").status_code, 401)

    def test_an_operator_passkey_alone_opens_the_operator_endpoints_but_not_admin(self):
        import api.operator as operator
        with mock.patch.multiple(config, ADMIN_PASSKEY="", OPERATOR_PASSKEY="operator-passkey-0123456789"):
            self.assertEqual(self.call("operator-passkey-0123456789").status_code, 200)
            self.assertFalse(operator.operator_configured(), "/admin stays closed")


class Hashing(unittest.TestCase):
    def test_too_long_password_is_a_clear_value_error(self):
        from pipeline.helpers.passwords import hash_password
        self.assertTrue(hash_password("x" * 72).startswith("$2b$"))
        with self.assertRaises(ValueError) as ctx:
            hash_password("x" * 73)
        self.assertIn("too long", str(ctx.exception))
        with self.assertRaises(ValueError):
            hash_password("é" * 37)   # 74 bytes

    def test_needs_rehash(self):
        from pipeline.helpers.passwords import hash_password, needs_rehash
        low = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=10)).decode()
        with mock.patch.dict(os.environ, {"BCRYPT_ROUNDS": "12"}):
            self.assertTrue(needs_rehash(low))
            self.assertFalse(needs_rehash(hash_password("pw")))
            self.assertFalse(needs_rehash("not a bcrypt hash"))
            self.assertFalse(needs_rehash(""))
        with mock.patch.dict(os.environ, {"BCRYPT_ROUNDS": "10"}):
            self.assertFalse(needs_rehash(low))
        with mock.patch.dict(os.environ, {"BCRYPT_ROUNDS": "4"}), self.assertRaises(ValueError):
            hash_password("pw")


class AdminSessionBinding(unittest.TestCase):
    """The admin session is tied to the current passkey and session secret (no database needed: the dashboard page does not read any)."""

    def setUp(self):
        import api.app as api_app
        self.api_app = api_app
        patcher = mock.patch("api.attempt_limits.SessionLocal", side_effect=__import__("sqlalchemy").exc.OperationalError("x", {}, Exception("down")))
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self, passkey):
        client = _client(self.api_app)
        response = client.post("/admin/login", data={"username": "", "password": passkey}, follow_redirects=False)
        return client, response

    def test_the_session_dies_when_the_passkey_or_the_session_secret_changes(self):
        with mock.patch.object(config, "ADMIN_PASSKEY", "admin-passkey-0123456789"):
            client, response = self.login("admin-passkey-0123456789")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(client.get("/admin/", follow_redirects=False).status_code, 200)
            with mock.patch.object(config, "ADMIN_PASSKEY", "a-different-passkey-9876543210"):
                self.assertIn(client.get("/admin/", follow_redirects=False).status_code, (302, 303, 307), "passkey changed")
            with mock.patch.object(config, "ADMIN_SESSION_SECRET", "z" * 40):
                self.assertIn(client.get("/admin/", follow_redirects=False).status_code, (302, 303, 307), "session secret changed")
            self.assertEqual(client.get("/admin/", follow_redirects=False).status_code, 200, "unchanged settings still work")

    def test_the_fingerprint_depends_on_both_and_hides_the_passkey(self):
        with mock.patch.multiple(config, ADMIN_PASSKEY="admin-passkey-0123456789", ADMIN_SESSION_SECRET="s" * 40):
            base = self.api_app._admin_fingerprint()
        with mock.patch.multiple(config, ADMIN_PASSKEY="admin-passkey-0123456780", ADMIN_SESSION_SECRET="s" * 40):
            self.assertNotEqual(base, self.api_app._admin_fingerprint())
        with mock.patch.multiple(config, ADMIN_PASSKEY="admin-passkey-0123456789", ADMIN_SESSION_SECRET="t" * 40):
            self.assertNotEqual(base, self.api_app._admin_fingerprint())
        self.assertNotIn("admin-passkey", base)

    def test_the_admin_cookie_is_not_signed_with_the_user_token_secret_and_lasts_8_hours(self):
        self.assertEqual(self.api_app.ADMIN_SESSION_SECONDS, 8 * 3600)
        views = self.api_app.admin.authentication_backend.middlewares[0]
        self.assertEqual(views.kwargs["secret_key"], config.ADMIN_SESSION_SECRET)
        self.assertNotEqual(views.kwargs["secret_key"], config.JWT_SECRET)
        self.assertEqual(views.kwargs["max_age"], 8 * 3600)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class WithDatabase(unittest.TestCase):
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
        import api.attempt_limits as attempt_limits
        from pipeline.db import get_db
        self.api_app = api_app
        engine = self.engine

        def factory(*a, **k):
            return Session(bind=engine, autoflush=False, expire_on_commit=False)

        def override():
            with factory() as session:
                yield session

        api_app.app.dependency_overrides[get_db] = override
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        for patcher in (mock.patch.object(attempt_limits, "SessionLocal", factory),
                        mock.patch.object(config, "ADMIN_PASSKEY", "admin-passkey-0123456789")):
            patcher.start()
            self.addCleanup(patcher.stop)
        for view in api_app.admin.views:   # SQLAdmin's own sessions go to the test database too
            view.session_maker = sessionmaker(bind=engine, expire_on_commit=False)
        self.factory = factory
        self._seed()

    def _seed(self):
        from pipeline.db import CreatorProfile
        from pipeline.helpers.passwords import hash_password
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, login_failures RESTART IDENTITY CASCADE"))
        pw_hash = hash_password(PASSWORD)
        with self.factory() as s:
            for email, extra in (("a@example.test", {}), ("must@example.test", {"must_change_password": True})):
                s.add(CreatorProfile(email=email, password_hash=pw_hash, is_active=True, full_name="Name", age=30, gender="female",
                                     primary_platform="instagram", follower_count=20000, content_niche="Beauty", instagram_handle="h",
                                     instagram_followers=20000, instagram_description="d", **extra))
            s.commit()

    def login(self, email="a@example.test", password=PASSWORD):
        client = _client(self.api_app)
        return client, client.post("/auth/login", json={"email": email, "password": password})

    def token_of(self, client):
        return client.cookies.get("access_token")

    def with_token(self, token):
        client = _client(self.api_app)
        client.cookies.set("access_token", token)
        return client

    def db_value(self, sql, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    # -- revocation ------------------------------------------------------------------------------------------------------
    def test_a_stolen_token_dies_after_logout_all_on_every_device(self):
        phone, r1 = self.login()
        laptop, r2 = self.login()
        stolen, other = self.token_of(phone), self.token_of(laptop)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(self.with_token(stolen).get("/auth/me").status_code, 200)
        out = phone.post("/auth/logout-all")
        self.assertEqual(out.status_code, 200)
        self.assertIn("max-age=0", out.headers["set-cookie"].lower())
        self.assertEqual(self.with_token(stolen).get("/auth/me").status_code, 401)
        self.assertEqual(self.with_token(other).get("/auth/me").status_code, 401, "the other device is signed out too")
        self.assertEqual(self.db_value("SELECT token_version FROM creator_profiles WHERE email = 'a@example.test'"), 1)
        again, response = self.login()   # signing in again works and gets the new version
        self.assertEqual(response.status_code, 200)
        self.assertEqual(again.get("/auth/me").status_code, 200)
        self.assertEqual(jwt.get_unverified_claims(self.token_of(again))["tv"], 1)

    def test_logout_all_response_and_cookie(self):
        client, _ = self.login()
        out = client.post("/auth/logout-all")
        self.assertEqual((out.status_code, out.json()), (200, {"status": "ok"}))
        self.assertNotIn("a@example.test", out.text)
        cookie = out.headers["set-cookie"].lower()
        for part in ("access_token=", "max-age=0", "httponly", "samesite=lax", "path=/"):
            self.assertIn(part, cookie)
        set_ = Response()
        import api.auth as auth
        auth._set_login_cookie(set_, "t")
        keep = lambda h: sorted(p.strip().lower() for p in h.split(";")[1:] if p.strip().split("=")[0].lower() in ("path", "samesite", "secure", "httponly", "domain"))
        self.assertEqual(keep(out.headers["set-cookie"]), keep(set_.headers["set-cookie"]), "delete matches the set")

    def test_logout_all_works_when_a_password_change_is_required(self):
        client, _ = self.login(email="must@example.test")
        self.assertEqual(client.get("/creator-profile/me").status_code, 403)
        self.assertEqual(client.post("/auth/logout-all").status_code, 200)

    def test_logout_all_without_a_session_is_401(self):
        self.assertEqual(_client(self.api_app).post("/auth/logout-all").status_code, 401)
        self.assertEqual(self.with_token("garbage").post("/auth/logout-all").status_code, 401)

    def test_logout_all_origin_check(self):
        client, _ = self.login()
        with mock.patch.object(self.api_app, "FRONTEND_ORIGINS", ["https://mavlit.com"]):
            self.assertEqual(client.post("/auth/logout-all", headers={"Origin": "https://evil.example"}).status_code, 403)
            self.assertEqual(self.db_value("SELECT token_version FROM creator_profiles WHERE email = 'a@example.test'"), 0, "nothing happened")
            self.assertEqual(client.post("/auth/logout-all", headers={"Origin": "https://mavlit.com"}).status_code, 200)

    def test_logout_all_is_limited_per_user(self):
        import api.auth as auth
        client, _ = self.login()
        other, _ = self.login(email="must@example.test")
        for _ in range(auth.LOGOUT_ALL_LIMIT):
            self.assertEqual(self.with_token(self.token_of(self.login()[0])).post("/auth/logout-all").status_code, 200)
        fresh, _ = self.login()
        blocked = fresh.post("/auth/logout-all")
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)
        self.assertEqual(fresh.get("/auth/me").status_code, 200, "a refused call changes nothing")
        self.assertEqual(self.db_value("SELECT token_version FROM creator_profiles WHERE email = 'a@example.test'"), auth.LOGOUT_ALL_LIMIT)
        self.assertEqual(other.post("/auth/logout-all").status_code, 200, "another user is not affected")

    def test_a_stolen_token_dies_after_a_password_change(self):
        client, _ = self.login()
        stolen = self.token_of(client)
        response = client.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": "a brand new password"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("password_hash", response.text)
        self.assertEqual(self.with_token(stolen).get("/auth/me").status_code, 401)
        self.assertEqual(client.get("/auth/me").status_code, 200, "this browser got a fresh cookie")
        self.assertEqual(self.login(password=PASSWORD)[1].status_code, 401, "the old password no longer works")
        self.assertEqual(self.login(password="a brand new password")[1].status_code, 200)

    def test_change_password_rules(self):
        client, _ = self.login()
        post = lambda cur, new: client.post("/auth/change-password", json={"current_password": cur, "new_password": new})  # noqa: E731
        self.assertEqual(post(PASSWORD, "short").status_code, 422, "at least 12 characters")
        self.assertEqual(post(PASSWORD, "12345678901").status_code, 422)
        self.assertEqual(post("not my password", "a brand new password").status_code, 400)
        self.assertEqual(post(PASSWORD, PASSWORD).status_code, 400)
        too_long = post(PASSWORD, "x" * 73)
        self.assertEqual(too_long.status_code, 422)
        self.assertIn("too long", too_long.text)
        self.assertEqual(client.get("/auth/me").status_code, 200, "nothing changed")
        self.assertEqual(self.db_value("SELECT token_version FROM creator_profiles WHERE email = 'a@example.test'"), 0)
        self.assertEqual(_client(self.api_app).post("/auth/change-password", json={"current_password": "x", "new_password": "y" * 12}).status_code, 401)

    def test_wrong_current_passwords_are_counted_towards_the_lockout(self):
        client, _ = self.login()
        for _ in range(5):
            self.assertEqual(client.post("/auth/change-password", json={"current_password": "wrong", "new_password": "a brand new password"}).status_code, 400)
        self.assertEqual(client.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": "a brand new password"}).status_code, 429)

    def test_tokens_from_before_revocation_existed_are_refused_by_the_claim_check_only_when_they_lack_iat(self):
        no_iat = jwt.encode(_claims(sub="1", iat=None), JWT_SECRET, algorithm="HS256")
        self.assertEqual(self.with_token(no_iat).get("/auth/me").status_code, 401)
        no_tv = jwt.encode(_claims(sub="1", tv=None), JWT_SECRET, algorithm="HS256")   # iat present, tv missing: counts as version 0
        with self.factory() as s:
            uid = s.execute(text("SELECT id FROM creator_profiles WHERE email = 'a@example.test'")).scalar()
        no_tv = jwt.encode(_claims(sub=str(uid), tv=None), JWT_SECRET, algorithm="HS256")
        self.assertEqual(self.with_token(no_tv).get("/auth/me").status_code, 200)

    def test_a_deactivated_user_is_refused_at_once(self):
        client, _ = self.login()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET is_active = false WHERE email = 'a@example.test'"))
        self.assertEqual(client.get("/auth/me").status_code, 401)

    # -- admin edits revoke sessions ---------------------------------------------------------------------------------------
    def admin_client(self):
        client = _client(self.api_app)
        self.assertEqual(client.post("/admin/login", data={"username": "", "password": "admin-passkey-0123456789"}, follow_redirects=False).status_code, 302)
        return client

    def admin_edit(self, client, user_id, **fields):
        return client.post(f"/admin/creator-profile/edit/{user_id}", data={"email": "a@example.test", **fields}, follow_redirects=False)

    def user_id(self, email="a@example.test"):
        return self.db_value("SELECT id FROM creator_profiles WHERE email = :e", e=email)

    def test_an_admin_password_edit_signs_the_user_out(self):
        user, _ = self.login()
        stolen = self.token_of(user)
        uid = self.user_id()
        edit = self.admin_edit(self.admin_client(), uid, password_hash="an admin typed password", is_active="y")
        self.assertEqual(edit.status_code, 302, edit.text[:300])
        self.assertEqual(self.with_token(stolen).get("/auth/me").status_code, 401)
        self.assertEqual(self.db_value("SELECT token_version FROM creator_profiles WHERE id = :i", i=uid), 1)
        self.assertEqual(self.login(password="an admin typed password")[1].status_code, 200)

    def test_an_admin_edit_without_a_password_or_status_change_keeps_sessions(self):
        user, _ = self.login()
        edit = self.admin_edit(self.admin_client(), self.user_id(), full_name="Renamed", is_active="y")
        self.assertEqual(edit.status_code, 302, edit.text[:300])
        self.assertEqual(user.get("/auth/me").status_code, 200)
        self.assertEqual(self.db_value("SELECT token_version FROM creator_profiles WHERE email = 'a@example.test'"), 0)

    def test_deactivating_and_reactivating_in_admin_never_revives_old_tokens(self):
        user, _ = self.login()
        stolen = self.token_of(user)
        uid, admin = self.user_id(), self.admin_client()
        self.assertEqual(self.admin_edit(admin, uid).status_code, 302)   # is_active omitted = unchecked = deactivated
        self.assertEqual(self.with_token(stolen).get("/auth/me").status_code, 401)
        self.assertEqual(self.admin_edit(admin, uid, is_active="y").status_code, 302)   # reactivated
        self.assertEqual(self.with_token(stolen).get("/auth/me").status_code, 401, "the old token stays dead")
        self.assertEqual(self.db_value("SELECT token_version FROM creator_profiles WHERE id = :i", i=uid), 2)

    def test_the_admin_form_shows_a_too_long_password_as_a_validation_error(self):
        edit = self.admin_edit(self.admin_client(), self.user_id(), password_hash="x" * 80, is_active="y")
        self.assertEqual(edit.status_code, 400)
        self.assertIn("too long", edit.text)
        self.assertEqual(self.login()[1].status_code, 200, "the password was not changed")

    def test_token_version_is_not_an_admin_form_field(self):
        view = next(v for v in self.api_app.admin.views if v.__class__.__name__ == "CreatorProfileAdmin")
        self.assertNotIn("token_version", view.get_form_columns())

    # -- must_change_password ------------------------------------------------------------------------------------------------
    def test_must_change_password_blocks_everything_but_the_allowed_routes(self):
        client, response = self.login("must@example.test")
        self.assertEqual(response.status_code, 200, "signing in itself works")
        self.assertTrue(response.json()["must_change_password"])
        self.assertTrue(client.get("/auth/me").json()["must_change_password"])
        for method, path in (("GET", "/pitches/me"), ("GET", "/saved-brands/me"), ("GET", "/matches/me/v3"), ("GET", "/brands/1"),
                             ("GET", "/creator-profile/me"), ("GET", "/dashboard/me"), ("POST", "/matches/me/v3/refresh")):
            with self.subTest(path):
                blocked = client.request(method, path)
                self.assertEqual(blocked.status_code, 403)
                self.assertEqual(blocked.json()["detail"]["code"], "password_change_required")
        self.assertEqual(client.post("/auth/logout").status_code, 200)
        client, _ = self.login("must@example.test")
        done = client.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": "a brand new password"})
        self.assertEqual(done.status_code, 200)
        self.assertFalse(done.json()["must_change_password"])
        self.assertEqual(client.get("/pitches/me").status_code, 200, "unblocked after the change")
        self.assertFalse(self.db_value("SELECT must_change_password FROM creator_profiles WHERE email = 'must@example.test'"))

    # -- hashing ---------------------------------------------------------------------------------------------------------
    def test_a_hash_with_a_lower_cost_is_upgraded_on_login_and_then_left_alone(self):
        low = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=10)).decode()
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET password_hash = :h WHERE email = 'a@example.test'"), {"h": low})
        self.assertTrue(self.login(password="wrong")[1].status_code == 401)
        self.assertEqual(self.db_value("SELECT password_hash FROM creator_profiles WHERE email = 'a@example.test'"), low, "a failed login changes nothing")
        self.assertEqual(self.login()[1].status_code, 200)
        upgraded = self.db_value("SELECT password_hash FROM creator_profiles WHERE email = 'a@example.test'")
        self.assertTrue(upgraded.startswith("$2b$12$"), upgraded[:8])
        self.assertEqual(self.login()[1].status_code, 200)
        self.assertEqual(self.db_value("SELECT password_hash FROM creator_profiles WHERE email = 'a@example.test'"), upgraded)

    # -- lockouts ----------------------------------------------------------------------------------------------------------
    def test_the_admin_login_locks_a_client_out_after_five_failures(self):
        client = _client(self.api_app)
        for attempt in range(5):
            self.assertEqual(client.post("/admin/login", data={"username": "", "password": "wrong"}, follow_redirects=False).status_code, 400, attempt)
        locked = client.post("/admin/login", data={"username": "", "password": "admin-passkey-0123456789"}, follow_redirects=False)
        self.assertEqual(locked.status_code, 429, "even the right passkey is refused while locked")
        self.assertIn("Too many failed attempts", locked.text)   # SQLAdmin shows its own error page here (without the Retry-After header)
        # kept in the database, so a new client / process is still locked
        self.assertEqual(_client(self.api_app).post("/admin/login", data={"username": "", "password": "admin-passkey-0123456789"}, follow_redirects=False).status_code, 429)
        rows = self.db_value("SELECT count(*) FROM login_failures")
        self.assertEqual(rows, 5)
        self.assertNotIn("admin-passkey", " ".join(r[0] for r in self.engine.connect().execute(text("SELECT email_hash FROM login_failures"))))
        with self.engine.begin() as conn:   # the window passes
            conn.execute(text("UPDATE login_failures SET created_at = created_at - interval '20 minutes'"))
        self.assertEqual(self.admin_client().get("/admin/", follow_redirects=False).status_code, 200)

    def test_the_operator_check_locks_a_client_out_after_five_failures(self):
        post = lambda key: _client(self.api_app).post("/seed", json={"niche": "Beauty"}, headers={"Authorization": f"Bearer {key}"})  # noqa: E731
        for attempt in range(5):
            self.assertEqual(post("wrong").status_code, 401, attempt)
        locked = post("admin-passkey-0123456789")
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Retry-After", locked.headers)
        self.assertEqual(self.db_value("SELECT count(*) FROM login_failures"), 5)

    def test_one_clients_failures_do_not_lock_another_client(self):
        from api import login_limits
        with self.factory() as s:
            for _ in range(5):
                login_limits.record_failure(s, "admin:198.51.100.1", "198.51.100.1")
        self.assertEqual(self.admin_client().get("/admin/", follow_redirects=False).status_code, 200, "the test client has another address")

    def test_user_logins_and_admin_attempts_are_counted_apart(self):
        for _ in range(5):
            self.login(password="wrong")
        self.assertEqual(self.admin_client().get("/admin/", follow_redirects=False).status_code, 200)


if __name__ == "__main__":
    unittest.main()
