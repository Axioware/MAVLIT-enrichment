"""
Phase 5, batch 1: docs switched off in production, completed-profile gate, profile_incomplete body, input constraints, request-body size
limits, generic error text, status codes, per-user daily caps and the in-flight pitch lock, the saved-brands cap, and 409 on a profile save
while a description is generated. The database tests need a disposable local PostgreSQL (MAVLIT_TEST_PG_URL), like the other suites.
"""

import asyncio
import json
import os
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

PASSWORD = "correct horse battery"


def _client(api_app):
    return TestClient(api_app.app, raise_server_exceptions=False)


def _production_status(paths):
    """Import the app in a fresh interpreter with ENVIRONMENT=production (no .env) and return {path: status}."""
    env = {k: v for k, v in os.environ.items() if k not in ("ENVIRONMENT", "ADMIN_PASSKEY", "OPERATOR_PASSKEY")}
    env.update({"DATABASE_URL": "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable", "ENVIRONMENT": "production",
                "JWT_SECRET": "p" * 40, "ADMIN_SESSION_SECRET": "q" * 40, "ADMIN_PASSKEY": "r" * 20})
    code = ("import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
            "import json\nfrom fastapi.testclient import TestClient\nimport api.app as a\n"
            f"c = TestClient(a.app, raise_server_exceptions=False)\nprint(json.dumps({{p: c.get(p).status_code for p in {paths!r}}}))\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-800:]
    return json.loads(result.stdout.strip().splitlines()[-1])


class DocsSwitch(unittest.TestCase):
    def test_docs_and_schema_are_404_in_production_and_work_in_development(self):
        paths = ["/docs", "/redoc", "/openapi.json"]
        self.assertEqual(_production_status(paths), {p: 404 for p in paths})
        import api.app as api_app
        client = _client(api_app)
        for path in paths:
            self.assertEqual(client.get(path).status_code, 200, path)


class InputConstraints(unittest.TestCase):
    def invalid(self, model, **data):
        with self.assertRaises(ValidationError):
            model(**data)

    def test_login_and_change_password(self):
        from api.auth import ChangePasswordRequest, LoginRequest
        LoginRequest(email="a@example.test", password="x")
        self.invalid(LoginRequest, email="a" * 255, password="x")
        self.invalid(LoginRequest, email="a@example.test", password="")
        self.invalid(LoginRequest, email="a@example.test", password="p" * 201)
        self.invalid(ChangePasswordRequest, current_password="p" * 201, new_password="a long enough password")

    def test_pitch_requests(self):
        from api.schemas import ManualPitchRequest, PitchRequest, PitchUpdateRequest
        PitchRequest(is_custom=False, brand_id=1, story="s" * 3000, product_reference="p" * 500, past_brand_partnership="p" * 500,
                     content_link="l" * 500)
        for field, size in (("story", 3001), ("product_reference", 501), ("past_brand_partnership", 501), ("content_link", 501),
                            ("custom_brand_name", 201)):
            self.invalid(PitchRequest, is_custom=True, **{field: "x" * size})
        self.invalid(PitchRequest, is_custom=False, brand_id=0)
        self.invalid(ManualPitchRequest, sent_at="2026-01-01", status="bogus")
        self.invalid(ManualPitchRequest, sent_at="2026-01-01", custom_brand_name="x" * 201)
        self.invalid(PitchUpdateRequest, status="bogus")
        PitchUpdateRequest(status="sent")

    def test_rate_intelligence_and_contract(self):
        from api.schemas import ContractAdviceRequest, RateIntelligenceRequest
        RateIntelligenceRequest(brand_name="b", deliverable_type="Reel", duration_months=120)
        for kwargs in ({"brand_name": "b" * 201}, {"platform": "p" * 51}, {"deliverable_type": "d" * 101}, {"exclusivity": "e" * 101},
                       {"usage": "u" * 101}, {"duration_months": 0}, {"duration_months": 121}, {"brand_id": 0}):
            self.invalid(RateIntelligenceRequest, **{"deliverable_type": "Reel", **kwargs})
        ContractAdviceRequest(contract_text="c", deliverable_type="d" * 100)
        for field in ("deliverable_type", "exclusivity", "usage"):
            self.invalid(ContractAdviceRequest, contract_text="c", **{field: "x" * 101})

    def test_latest_estimate_query_parameters(self):
        from fastapi.routing import APIRoute
        import api.advisory as advisory
        route = next(r for r in advisory.router.routes if isinstance(r, APIRoute) and r.path == "/rate-intelligence/latest")
        params = {p.name: p.field_info for p in route.dependant.query_params}
        self.assertEqual([m.max_length for m in params["brand_name"].metadata if hasattr(m, "max_length")], [200])
        self.assertTrue(any(getattr(m, "ge", None) == 1 for m in params["brand_id"].metadata))
        self.assertTrue(any(getattr(m, "le", None) == 120 for m in params["duration_months"].metadata))

    def test_profile_request(self):
        from api.app import CreatorProfileRequest
        ok = dict(full_name="N" * 150, creator_handle="h" * 100)
        CreatorProfileRequest(**ok, age=13, instagram_followers=10**10, instagram_description="d" * 4000, excluded_categories=["c" * 100] * 30,
                              instagram_description_mode="generate", youtube_description_mode="manual")
        for kwargs in ({"full_name": "N" * 151}, {"creator_handle": "h" * 101}, {"age": 12}, {"age": 101}, {"gender": "g" * 51},
                       {"content_niche": "n" * 201}, {"instagram_handle": "h" * 101}, {"primary_platform": "p" * 101},
                       {"instagram_followers": -1}, {"follower_count": 10**10 + 1}, {"youtube_subscribers": -5},
                       {"instagram_description": "d" * 4001}, {"youtube_description": "d" * 4001},
                       {"excluded_categories": ["c"] * 31}, {"instagram_sub_niches": ["c" * 101]},
                       {"youtube_excluded_categories": ["c"] * 31}, {"instagram_description_mode": "bogus"},
                       {"youtube_description_mode": "bogus"}, {"youtube_channel_name": "c" * 201}, {"instagram_primary_niche": "n" * 201}):
            self.invalid(CreatorProfileRequest, **{**ok, **kwargs})

    def test_description_post_and_own_brand_requests(self):
        from api.app import CreatorDescriptionRequest
        from api.creator_posts import AddPostRequest
        from api.own_brands import OwnBrandRequest
        CreatorDescriptionRequest(platform="youtube", username="u" * 100, niche="n" * 200)
        self.invalid(CreatorDescriptionRequest, platform="tiktok", username="u", niche="n")
        self.invalid(CreatorDescriptionRequest, platform="instagram", username="u" * 101, niche="n")
        self.invalid(CreatorDescriptionRequest, platform="instagram", username="u", niche="n" * 201)
        self.invalid(AddPostRequest, post_url="https://www.instagram.com/p/" + "x" * 500)
        self.invalid(OwnBrandRequest, instagram="x" * 201)

    def test_operator_requests(self):
        from api.app import BulkAddCreatorsRequest, EnrichRequest, ScoreRequest, SeedRequest
        BulkAddCreatorsRequest(urls=["u" * 300] * 200, niche="n")
        self.invalid(BulkAddCreatorsRequest, urls=["u"] * 201, niche="n")
        self.invalid(BulkAddCreatorsRequest, urls=["u" * 301], niche="n")
        for model, field in ((SeedRequest, "limit"), (EnrichRequest, "limit_per_step"), (ScoreRequest, "limit")):
            base = {"niche": "n"} if model is SeedRequest else {}
            model(**base, **{field: 5000})
            for bad in (0, -1, 5001):
                self.invalid(model, **base, **{field: bad})
        EnrichRequest(steps=["shopify", "tranco"])
        self.invalid(EnrichRequest, steps=["rm -rf"])

    def test_brand_category_tags_query(self):
        import api.app as api_app
        client = _client(api_app)
        too_many = "&".join(f"niche=n{i}" for i in range(21))
        self.assertEqual(client.get("/brand-category-tags?" + too_many).status_code, 422)
        self.assertEqual(client.get("/brand-category-tags?niche=" + "n" * 101).status_code, 422)
        self.assertEqual(client.get("/brand-category-tags").status_code, 200)

    def test_a_stored_profile_over_the_new_caps_can_still_be_read(self):
        from api.schemas import CreatorProfileResponse
        self.assertEqual(CreatorProfileResponse.model_fields["instagram_description"].metadata, [], "responses carry no length limits")


class BodySizeLimits(unittest.TestCase):
    def setUp(self):
        from api.body_limits import BodySizeLimitMiddleware
        app = FastAPI()
        app.add_middleware(BodySizeLimitMiddleware)
        seen = []

        @app.post("/echo")
        async def echo(request: __import__("starlette.requests", fromlist=["Request"]).Request):
            seen.append(len(await request.body()))
            return {"bytes": seen[-1]}

        @app.put("/creator-profile/me")
        async def profile(request: __import__("starlette.requests", fromlist=["Request"]).Request):
            return {"bytes": len(await request.body())}

        @app.post("/admin/x")
        async def admin(request: __import__("starlette.requests", fromlist=["Request"]).Request):
            return {"bytes": len(await request.body())}

        self.client, self.seen = TestClient(app, raise_server_exceptions=False), seen

    def test_limits_per_route(self):
        from api.body_limits import limit_bytes
        kb = 1024
        self.assertEqual(limit_bytes("POST", "/auth/login"), 16 * kb)
        self.assertEqual(limit_bytes("PUT", "/creator-profile/me"), 32 * kb)
        self.assertEqual(limit_bytes("POST", "/creator-profile/me/generate-description"), 16 * kb)
        for method, path in (("POST", "/pitches"), ("POST", "/pitches/stream"), ("POST", "/pitches/manual")):
            self.assertEqual(limit_bytes(method, path), 32 * kb, path)
        self.assertEqual(limit_bytes("PATCH", "/pitches/7"), 64 * kb)
        self.assertEqual(limit_bytes("POST", "/contract-advice"), 64 * kb)
        self.assertEqual(limit_bytes("POST", "/content-creator-re/bulk-add"), 256 * kb)
        self.assertEqual(limit_bytes("POST", "/admin/login"), 0, "SQLAdmin forms are not limited")

    def test_environment_overrides(self):
        from api.body_limits import limit_bytes
        with mock.patch.dict(os.environ, {"BODY_LIMIT_KB": "1", "BODY_LIMIT_KB_STANDARD": "2", "BODY_LIMIT_KB_LARGE": "3", "BODY_LIMIT_KB_BULK": "4"}):
            self.assertEqual([limit_bytes("POST", "/auth/login"), limit_bytes("POST", "/pitches"), limit_bytes("POST", "/contract-advice"),
                              limit_bytes("POST", "/content-creator-re/bulk-add")], [1024, 2048, 3072, 4096])
        with mock.patch.dict(os.environ, {"BODY_LIMIT_KB": "0"}):
            self.assertEqual(limit_bytes("POST", "/auth/login"), 0, "0 means no limit")
        with mock.patch.dict(os.environ, {"BODY_LIMIT_KB": "lots"}):
            with self.assertRaises(ValueError):
                limit_bytes("POST", "/auth/login")

    def test_content_length_over_the_limit_is_413_json_and_the_route_never_runs(self):
        response = self.client.post("/echo", content=b"x" * (16 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertIn("too large", response.json()["detail"])
        self.assertEqual(self.seen, [])

    def test_at_the_limit_passes(self):
        self.assertEqual(self.client.post("/echo", content=b"x" * (16 * 1024)).json(), {"bytes": 16 * 1024})

    def test_a_chunked_body_without_content_length_is_counted(self):
        def chunks():
            for _ in range(20):
                yield b"x" * 1024
        response = self.client.post("/echo", content=chunks())
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(self.seen, [])
        small = self.client.post("/echo", content=(b"x" * 100 for _ in range(3)))
        self.assertEqual(small.json(), {"bytes": 300})

    def test_a_wrong_content_length_does_not_get_past_the_count(self):
        scope = {"type": "http", "method": "POST", "path": "/echo", "headers": [(b"content-length", b"10")], "query_string": b""}
        sent = []

        async def run():
            from api.body_limits import BodySizeLimitMiddleware
            parts = iter([{"type": "http.request", "body": b"x" * 20000, "more_body": False}])

            async def app(scope, receive, send):
                await receive()
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"{}"})

            async def receive():
                return next(parts)

            async def send(message):
                sent.append(message)

            await BodySizeLimitMiddleware(app)(scope, receive, send)

        asyncio.run(run())
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(len([m for m in sent if m["type"] == "http.response.start"]), 1, "the application's own answer is dropped")

    def test_exempt_and_per_route_limits(self):
        self.assertEqual(self.client.post("/admin/x", content=b"x" * 100_000).status_code, 200)
        self.assertEqual(self.client.put("/creator-profile/me", content=b"x" * 20_000).status_code, 200)
        self.assertEqual(self.client.put("/creator-profile/me", content=b"x" * 33_000).status_code, 413)

    def test_the_real_app_returns_413_for_a_big_login(self):
        import api.app as api_app
        response = _client(api_app).post("/auth/login", content=b"{" + b" " * 20_000 + b"}", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)


class UsageSettings(unittest.TestCase):
    CAPS = {"pitches": "PITCHES_PER_DAY", "rate_intelligence": "RATE_INTELLIGENCE_PER_DAY", "contract_advice": "CONTRACT_ADVICE_PER_DAY",
            "match_refresh": "MATCH_REFRESH_PER_DAY", "own_brands": "OWN_BRANDS_PER_DAY", "description_generation": "DESCRIPTION_GENERATIONS_PER_DAY"}
    DEFAULTS = {"pitches": 30, "rate_intelligence": 20, "contract_advice": 10, "match_refresh": 10, "own_brands": 10, "description_generation": 20}

    def test_defaults_and_overrides(self):
        from api import usage_limits
        for feature, name in self.CAPS.items():
            self.assertEqual(usage_limits.DAILY_CAPS[feature][0], name)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(name, None)
                self.assertEqual(usage_limits.daily_cap(feature), self.DEFAULTS[feature], name)
            with mock.patch.dict(os.environ, {name: "3"}):
                self.assertEqual(usage_limits.daily_cap(feature), 3, name)
            with mock.patch.dict(os.environ, {name: "0"}):
                self.assertEqual(usage_limits.daily_cap(feature), 0, f"{name}=0 turns it off")
            for bad in ("abc", "-1"):
                with mock.patch.dict(os.environ, {name: bad}):
                    with self.assertRaises(ValueError):
                        usage_limits.check_settings()

    def test_other_settings(self):
        from api import usage_limits
        self.assertEqual((usage_limits.saved_brands_max(), usage_limits.lock_stale_seconds()), (500, 300))
        with mock.patch.dict(os.environ, {"SAVED_BRANDS_MAX": "7", "PITCH_LOCK_STALE_SECONDS": "90"}):
            self.assertEqual((usage_limits.saved_brands_max(), usage_limits.lock_stale_seconds()), (7, 90))
        with mock.patch.dict(os.environ, {"PITCH_LOCK_STALE_SECONDS": "5"}):
            with self.assertRaises(ValueError):
                usage_limits.check_settings()

    def test_every_new_variable_is_documented(self):
        example = (ROOT / ".env.example").read_text()
        for name in list(self.CAPS.values()) + ["SAVED_BRANDS_MAX", "PITCH_LOCK_STALE_SECONDS", "BODY_LIMIT_KB", "BODY_LIMIT_KB_STANDARD",
                                                "BODY_LIMIT_KB_LARGE", "BODY_LIMIT_KB_BULK"]:
            self.assertIn(name, example, name)


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
        import api.pitches as api_pitches
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
        for patcher in (mock.patch.object(attempt_limits, "SessionLocal", factory), mock.patch.object(api_app, "SessionLocal", factory),
                        mock.patch.object(api_pitches, "SessionLocal", factory),
                        mock.patch.object(api_app, "_run_creator_signals_job"), mock.patch.object(api_app, "_run_ai_ranking_job"),
                        mock.patch.object(api_app, "_run_creator_description_job")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.factory = factory
        self._seed()

    def _seed(self):
        from pipeline.db import BrandRaw, CreatorProfile
        from pipeline.helpers.passwords import hash_password
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, brands_raw, feature_usage, feature_locks, login_failures RESTART IDENTITY CASCADE"))
        pw = hash_password(PASSWORD)
        self.ids = {}
        with self.factory() as s:
            for key, email, complete in (("a", "a@example.test", True), ("b", "b@example.test", True), ("c", "c@example.test", False)):
                row = CreatorProfile(email=email, password_hash=pw, is_active=True, full_name="Name" if complete else None, age=30 if complete else None,
                                     gender="female", primary_platform="instagram", follower_count=20000, content_niche="Beauty",
                                     instagram_handle="h", instagram_followers=20000, instagram_description="d")
                s.add(row)
                s.flush()
                self.ids[key] = row.id
            for n in range(1, 5):
                s.add(BrandRaw(name=f"Brand {n}"))
            s.commit()

    def client(self, who):
        import api.auth as auth
        client = _client(self.api_app)
        client.cookies.set("access_token", auth._make_jwt(self.ids[who], f"{who}@example.test", 0))
        return client

    def value(self, sql, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def uses(self, who, feature):
        return self.value("SELECT coalesce(sum(uses), 0) FROM feature_usage WHERE creator_profile_id = :c AND feature = :f", c=self.ids[who], f=feature)

    PITCH = {"is_custom": True, "custom_brand_name": "Acme", "story": "x"}

    # -- completed profile ---------------------------------------------------------------------------------------------------
    def test_incomplete_profile_is_refused_with_a_machine_readable_error(self):
        c = self.client("c")
        calls = [("post", "/pitches", self.PITCH), ("post", "/pitches/stream", self.PITCH), ("get", "/pitches/me", None),
                 ("post", "/pitches/manual", {"custom_brand_name": "x", "sent_at": "2026-01-01"}),
                 ("patch", "/pitches/1", {"status": "sent"}), ("delete", "/pitches/1", None),
                 ("post", "/rate-intelligence", {"brand_name": "b", "deliverable_type": "Reel"}), ("get", "/rate-intelligence/latest", None),
                 ("post", "/contract-advice", {"contract_text": "x"}), ("post", "/own-brands", {"instagram": "brand"}),
                 ("get", "/own-brands/jobs/x", None), ("post", "/saved-brands", {"brand_id": 1}), ("get", "/saved-brands/me", None),
                 ("delete", "/saved-brands/1", None)]
        for method, path, body in calls:
            response = getattr(c, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.json()["detail"], {"error": "profile_incomplete",
                                                          "message": "Complete your creator profile before accessing the application"}, path)
        for path in ("/auth/me", "/creator-profile/me", "/creator-profile/me/posts"):
            self.assertEqual(c.get(path).status_code, 200, path)

    # -- docs of the error text ----------------------------------------------------------------------------------------------
    def test_provider_text_never_reaches_the_client(self):
        from pipeline.helpers.gpt_llm import LLMUnavailableError
        secret = "OpenAI unavailable (credits or quota exhausted): HTTP 429 insufficient_quota sk-live-123"
        a = self.client("a")
        with mock.patch("api.pitches.generate_pitch", side_effect=LLMUnavailableError(secret)):
            r = a.post("/pitches", json=self.PITCH)
        self.assertEqual((r.status_code, r.json()["detail"]), (502, "Pitch generation is temporarily unavailable, try again"))
        with mock.patch("api.advisory.estimate_rate", side_effect=RuntimeError(secret)):
            r = a.post("/rate-intelligence", json={"brand_name": "b", "deliverable_type": "Reel"})
        self.assertEqual((r.status_code, r.json()["detail"]), (502, "Rate estimation is temporarily unavailable, try again"))
        with mock.patch("api.advisory.review_contract", side_effect=RuntimeError(secret)):
            r = a.post("/contract-advice", json={"contract_text": "x"})
        self.assertEqual((r.status_code, r.json()["detail"]), (502, "Contract review is temporarily unavailable, try again"))
        for error in (LLMUnavailableError(secret), RuntimeError("OpenAI request failed: " + secret)):
            def broken(*args, **kwargs):
                raise error
                yield   # makes it a generator
            with mock.patch("api.pitches.stream_pitch", broken), self.assertLogs("api.pitches", "WARNING") as logs:
                r = a.post("/pitches/stream", json=self.PITCH)
            events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
            self.assertEqual(events, [{"type": "error", "detail": "Pitch generation is temporarily unavailable, try again"}])
            self.assertNotIn("sk-live", r.text)
            self.assertIn("sk-live-123", "\n".join(logs.output), "the real text is logged server-side")

    # -- status codes --------------------------------------------------------------------------------------------------------
    def test_status_codes(self):
        a = self.client("a")
        r = a.post("/rate-intelligence", json={"deliverable_type": "Reel"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.uses("a", "rate_intelligence"), 0, "a rejected request does not use the cap")
        r = a.post("/rate-intelligence", json={"brand_id": 99999, "deliverable_type": "Reel"})
        self.assertEqual(r.status_code, 404, "a brand that really does not exist")
        with mock.patch("api.pitches.generate_pitch", side_effect=ValueError("brand_id=99999 not found")):
            self.assertEqual(a.post("/pitches", json={"is_custom": False, "brand_id": 99999}).status_code, 404)
        self.assertEqual(a.post("/saved-brands", json={"brand_id": 1}).status_code, 201)
        self.assertEqual(a.post("/saved-brands", json={"brand_id": 1}).status_code, 200, "a repeat save")
        self.assertEqual(self.value("SELECT count(*) FROM saved_brands WHERE creator_profile_id = :c", c=self.ids["a"]), 1)
        self.assertEqual(a.post("/saved-brands", json={"brand_id": 99999}).status_code, 404)

    def test_pitch_state_conflicts_are_409_and_bad_statuses_422(self):
        from pipeline.db import Pitch
        with self.factory() as s:
            pitch = Pitch(creator_profile_id=self.ids["a"], is_custom=True, brand_name="Acme", story="s", pitch_text="t", status="closed_won")
            manual = Pitch(creator_profile_id=self.ids["a"], is_custom=True, brand_name="M", story="", status="sent", is_manual=True)
            s.add_all([pitch, manual])
            s.commit()
            pitch_id, manual_id = pitch.id, manual.id
        a = self.client("a")
        self.assertEqual(a.patch(f"/pitches/{pitch_id}", json={"status": "sent"}).status_code, 409)
        self.assertEqual(a.patch(f"/pitches/{manual_id}", json={"pitch_text": "new"}).status_code, 409)
        self.assertEqual(a.patch(f"/pitches/{pitch_id}", json={"status": "bogus"}).status_code, 422)
        self.assertEqual(a.post("/pitches/manual", json={"custom_brand_name": "x", "sent_at": "2026-01-01", "status": "bogus"}).status_code, 422)

    # -- daily caps ----------------------------------------------------------------------------------------------------------
    def assert_cap(self, feature, env, call, patch_target=None, effect=None, between=None):
        """cap 2: the third call is 429 (with Retry-After), the real work is not started, user B is unaffected, and 0 turns the route off."""
        a, b = self.client("a"), self.client("b")
        with mock.patch.dict(os.environ, {env: "2"}):
            for _ in range(2):
                call(a)
                if between:
                    between()
            started = []
            ctx = mock.patch(patch_target, side_effect=lambda *x, **k: started.append(1) or effect) if patch_target else mock.MagicMock()
            with ctx:
                r = call(a)
            self.assertEqual(r.status_code, 429, r.text)
            self.assertIn("daily limit", r.json()["detail"])
            self.assertGreater(int(r.headers["Retry-After"]), 0)
            self.assertLessEqual(int(r.headers["Retry-After"]), 86400)
            self.assertEqual(started, [], "refused before any LLM / Apify work")
            self.assertEqual(self.uses("a", feature), 2)
            if between:
                between()
            self.assertNotEqual(call(b).status_code, 429, "user A's usage does not count against user B")
            self.assertEqual(self.uses("b", feature), 1)
        if between:
            between()
        with mock.patch.dict(os.environ, {env: "0"}):
            self.assertEqual(call(b).status_code, 403, "0 turns the route off")

    def test_pitch_cap_counts_pitches_and_streams_together(self):
        from pipeline.helpers.gpt_llm import LLMUnavailableError
        a, b = self.client("a"), self.client("b")
        with mock.patch.dict(os.environ, {"PITCHES_PER_DAY": "2"}), mock.patch("api.pitches.generate_pitch", side_effect=LLMUnavailableError("x")) as gen:
            self.assertEqual(a.post("/pitches", json=self.PITCH).status_code, 502)
            self.assertEqual(a.post("/pitches/stream", json=self.PITCH).status_code, 200)
            third = a.post("/pitches", json=self.PITCH)
            self.assertEqual(third.status_code, 429)
            self.assertEqual(a.post("/pitches/stream", json=self.PITCH).status_code, 429)
            self.assertEqual(gen.call_count, 1, "no LLM call once the cap is used up")
            self.assertEqual(b.post("/pitches", json=self.PITCH).status_code, 502, "B is not affected by A")
        self.assertEqual(self.uses("a", "pitches"), 2)
        self.assertEqual(self.uses("b", "pitches"), 1)
        with mock.patch.dict(os.environ, {"PITCHES_PER_DAY": "0"}):
            self.assertEqual(b.post("/pitches", json=self.PITCH).status_code, 403)

    def test_rate_intelligence_cap(self):
        self.assert_cap("rate_intelligence", "RATE_INTELLIGENCE_PER_DAY",
                        lambda c: c.post("/rate-intelligence", json={"brand_name": "b", "deliverable_type": "Reel"}),
                        "api.advisory.estimate_rate", RuntimeError("stop"))

    def test_contract_advice_cap(self):
        self.assert_cap("contract_advice", "CONTRACT_ADVICE_PER_DAY", lambda c: c.post("/contract-advice", json={"contract_text": "x"}),
                        "api.advisory.review_contract", RuntimeError("stop"))

    def test_own_brands_cap_and_a_typo_is_free(self):
        a = self.client("a")
        self.assertEqual(a.post("/own-brands", json={"instagram": "not a handle!"}).status_code, 400)
        self.assertEqual(self.uses("a", "own_brands"), 0)
        with mock.patch("api.own_brands.start_own_brand_job", side_effect=ValueError("stop")):
            self.assert_cap("own_brands", "OWN_BRANDS_PER_DAY", lambda c: c.post("/own-brands", json={"instagram": "brandname"}))

    def test_match_refresh_cap(self):
        def reset():
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE creator_profiles SET llm_v3_ranking_status = 'idle'"))
        self.assert_cap("match_refresh", "MATCH_REFRESH_PER_DAY", lambda c: c.post("/matches/me/v3/refresh"), between=reset)

    def test_description_generation_cap(self):
        def reset():
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE creator_profiles SET instagram_description_status = 'idle', instagram_description_job_id = NULL"))
        body = {"platform": "instagram", "username": "someone", "niche": "Beauty"}
        self.assert_cap("description_generation", "DESCRIPTION_GENERATIONS_PER_DAY",
                        lambda c: c.post("/creator-profile/me/generate-description", json=body), between=reset)

    def test_a_pending_description_job_is_returned_without_using_the_cap(self):
        a = self.client("a")
        body = {"platform": "instagram", "username": "someone", "niche": "Beauty"}
        self.assertEqual(a.post("/creator-profile/me/generate-description", json=body).status_code, 200)
        self.assertEqual(a.post("/creator-profile/me/generate-description", json=body).status_code, 200)
        self.assertEqual(self.uses("a", "description_generation"), 1)

    def test_the_cap_resets_on_the_next_utc_day(self):
        a = self.client("a")
        with mock.patch.dict(os.environ, {"CONTRACT_ADVICE_PER_DAY": "1"}), mock.patch("api.advisory.review_contract", side_effect=RuntimeError("x")):
            self.assertEqual(a.post("/contract-advice", json={"contract_text": "x"}).status_code, 502)
            self.assertEqual(a.post("/contract-advice", json={"contract_text": "x"}).status_code, 429)
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE feature_usage SET used_on = used_on - 1"))
            self.assertEqual(a.post("/contract-advice", json={"contract_text": "x"}).status_code, 502)

    def test_concurrent_requests_cannot_exceed_the_cap(self):
        from concurrent.futures import ThreadPoolExecutor
        from api import usage_limits
        with mock.patch.dict(os.environ, {"CONTRACT_ADVICE_PER_DAY": "3"}):
            def attempt(_):
                with self.factory() as s:
                    try:
                        usage_limits.consume(s, self.ids["a"], "contract_advice")
                        return True
                    except Exception:
                        return False
            with ThreadPoolExecutor(8) as pool:
                results = list(pool.map(attempt, range(12)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(self.uses("a", "contract_advice"), 3)

    # -- in-flight lock ------------------------------------------------------------------------------------------------------
    def lock_rows(self, who):
        return self.value("SELECT count(*) FROM feature_locks WHERE creator_profile_id = :c", c=self.ids[who])

    def test_one_pitch_in_flight_per_user(self):
        from pipeline.helpers.gpt_llm import LLMUnavailableError
        from api import usage_limits
        a, b = self.client("a"), self.client("b")
        with self.factory() as s:
            usage_limits.acquire_lock(s, self.ids["a"])   # A has a pitch running
        with mock.patch("api.pitches.generate_pitch", side_effect=LLMUnavailableError("x")) as gen:
            for path in ("/pitches", "/pitches/stream"):
                r = a.post(path, json=self.PITCH)
                self.assertEqual(r.status_code, 409, path)
                self.assertIn("already being written", r.json()["detail"])
            self.assertEqual(gen.call_count, 0)
            self.assertEqual(self.uses("a", "pitches"), 0, "a refused request does not use the cap")
            self.assertEqual(b.post("/pitches", json=self.PITCH).status_code, 502, "B can pitch while A is busy")
        self.assertEqual(self.lock_rows("a"), 1, "the refused request did not release A's own running pitch")

    def test_the_lock_is_released_on_success_error_and_a_cut_stream(self):
        from pipeline.helpers.gpt_llm import LLMUnavailableError
        a = self.client("a")
        with mock.patch("api.pitches.generate_pitch", side_effect=LLMUnavailableError("x")):
            a.post("/pitches", json=self.PITCH)
        self.assertEqual(self.lock_rows("a"), 0, "released after an error")
        with mock.patch("api.pitches.generate_pitch", side_effect=KeyError("boom")):
            self.assertEqual(a.post("/pitches", json=self.PITCH).status_code, 500)
        self.assertEqual(self.lock_rows("a"), 0, "released after an unexpected exception")

        def streaming(*args, **kwargs):
            yield "status", "working"
            yield "delta", "Hi"
        with mock.patch("api.pitches.stream_pitch", streaming):
            r = a.post("/pitches/stream", json=self.PITCH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.lock_rows("a"), 0, "released after the stream ended")
        with mock.patch("api.pitches.allow_contacts", side_effect=RuntimeError("fail before the stream")):
            a.post("/pitches/stream", json=self.PITCH)
        self.assertEqual(self.lock_rows("a"), 0, "released when the stream could not start")

    def test_a_stale_lock_is_taken_over(self):
        from api import usage_limits
        with self.factory() as s:
            usage_limits.acquire_lock(s, self.ids["a"])
            with self.assertRaises(Exception) as ctx:
                usage_limits.acquire_lock(s, self.ids["a"])
            self.assertEqual(ctx.exception.status_code, 409)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE feature_locks SET started_at = now() - interval '10 minutes'"))
        with self.factory() as s:
            usage_limits.acquire_lock(s, self.ids["a"])   # no error
        with mock.patch.dict(os.environ, {"PITCH_LOCK_STALE_SECONDS": "30"}), self.factory() as s:
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE feature_locks SET started_at = now() - interval '60 seconds'"))
            usage_limits.acquire_lock(s, self.ids["a"])   # the stale limit comes from the environment

    # -- saved brands cap ----------------------------------------------------------------------------------------------------
    def test_saved_brands_cap(self):
        a, b = self.client("a"), self.client("b")
        with mock.patch.dict(os.environ, {"SAVED_BRANDS_MAX": "2"}):
            self.assertEqual(a.post("/saved-brands", json={"brand_id": 1}).status_code, 201)
            self.assertEqual(a.post("/saved-brands", json={"brand_id": 2}).status_code, 201)
            full = a.post("/saved-brands", json={"brand_id": 3})
            self.assertEqual(full.status_code, 409)
            self.assertIn("up to 2", full.json()["detail"])
            self.assertEqual(a.post("/saved-brands", json={"brand_id": 2}).status_code, 200, "a repeat save is still fine when full")
            self.assertEqual(b.post("/saved-brands", json={"brand_id": 3}).status_code, 201, "B has its own limit")
            self.assertEqual(a.delete("/saved-brands/1").status_code, 200)
            self.assertEqual(a.post("/saved-brands", json={"brand_id": 3}).status_code, 201)

    # -- profile save while a description is generated -------------------------------------------------------------------------
    def profile_body(self):
        return {"full_name": "Name", "creator_handle": "h", "age": 30, "gender": "female", "primary_platform": "instagram", "follower_count": 20000,
                "content_niche": "Beauty", "instagram_handle": "h", "instagram_followers": 20000, "instagram_description": "d",
                "instagram_primary_niche": "Beauty"}

    def test_profile_save_is_409_while_a_description_job_is_pending(self):
        a = self.client("a")
        for platform in ("instagram", "youtube"):
            for status in ("queued", "running"):
                with self.engine.begin() as conn:
                    conn.execute(text(f"UPDATE creator_profiles SET {platform}_description_status = '{status}' WHERE id = :c"), {"c": self.ids["a"]})
                r = a.put("/creator-profile/me", json=self.profile_body())
                self.assertEqual(r.status_code, 409, (platform, status))
                self.assertIn("still being generated", r.json()["detail"])
            with self.engine.begin() as conn:
                conn.execute(text(f"UPDATE creator_profiles SET {platform}_description_status = 'completed' WHERE id = :c"), {"c": self.ids["a"]})
        self.assertEqual(a.put("/creator-profile/me", json=self.profile_body()).status_code, 200)

    def test_a_stored_profile_over_the_caps_is_readable_and_a_save_must_shorten_it(self):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET instagram_description = :d, full_name = :n WHERE id = :c"),
                         {"d": "d" * 6000, "n": "N" * 300, "c": self.ids["a"]})
        a = self.client("a")
        r = a.get("/creator-profile/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["instagram_description"]), 6000)
        too_long = {**self.profile_body(), "instagram_description": "d" * 6000, "full_name": "N" * 300}
        saved = a.put("/creator-profile/me", json=too_long)
        self.assertEqual(saved.status_code, 422)
        self.assertEqual({tuple(e["loc"])[-1] for e in saved.json()["detail"]}, {"instagram_description", "full_name"})
        self.assertEqual(a.put("/creator-profile/me", json={**self.profile_body(), "instagram_description": "short", "full_name": "Name"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
