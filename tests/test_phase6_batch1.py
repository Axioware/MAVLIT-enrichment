"""
Phase 6, batch 1: startup recovery of interrupted jobs, an async /health, the shared limit on slow routes (503 + Retry-After, released in every
case, raised worker-thread limit), and the bound on retrying one OpenAI call. The database tests need a disposable local PostgreSQL
(MAVLIT_TEST_PG_URL).
"""

import asyncio
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")

import anyio.to_thread  # noqa: E402
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from api.concurrency import SlowRouteLimitMiddleware, is_slow_route  # noqa: E402

PASSWORD = "correct horse battery"
BASE_ENV = {"DATABASE_URL": "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable", "JWT_SECRET": "p" * 40, "ADMIN_SESSION_SECRET": "q" * 40}


def _python(code, **env):
    clean = {k: v for k, v in os.environ.items() if k not in ("SLOW_ROUTE_CONCURRENCY", "OPENAI_MAX_ATTEMPTS", "OPENAI_CALL_DEADLINE_SECONDS", "ENVIRONMENT")}
    clean.update(BASE_ENV)
    clean.update(env)
    result = subprocess.run([sys.executable, "-c", "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n" + code], cwd=ROOT, env=clean,
                            capture_output=True, text=True, timeout=120)
    return result.returncode, result.stdout.strip(), result.stderr


SLOW = [("POST", "/pitches"), ("POST", "/pitches/stream"), ("POST", "/rate-intelligence"), ("POST", "/contract-advice"),
        ("POST", "/creator-profile/me/posts"), ("GET", "/creator-profile/me/posts/12/thumbnail"),
        ("POST", "/creator-profile/me/generate-description"), ("POST", "/matches/me/v2/refresh"), ("POST", "/matches/me/v3/refresh"),
        ("POST", "/own-brands")]
NOT_SLOW = [("POST", "/auth/login"), ("POST", "/auth/logout"), ("POST", "/auth/logout-all"), ("GET", "/auth/me"), ("GET", "/health"), ("GET", "/health/db"),
            ("POST", "/admin/login"), ("GET", "/admin/"), ("GET", "/pitches/me"), ("POST", "/pitches/manual"), ("PATCH", "/pitches/3"),
            ("GET", "/matches/me"), ("GET", "/rate-intelligence/latest"), ("GET", "/creator-profile/me"), ("PUT", "/creator-profile/me"),
            ("GET", "/creator-profile/me/posts"), ("PATCH", "/creator-profile/me/posts/3"), ("POST", "/saved-brands"), ("GET", "/own-brands/jobs/x"),
            ("GET", "/matches/me/v3"), ("GET", "/pitches"), ("POST", "/early-access")]


class SlowRouteClassification(unittest.TestCase):
    def test_exactly_the_listed_routes_are_slow(self):
        for method, path in SLOW:
            self.assertTrue(is_slow_route(method, path), (method, path))
        for method, path in NOT_SLOW:
            self.assertFalse(is_slow_route(method, path), (method, path))

    def test_setting(self):
        code = "from api.concurrency import slow_route_concurrency as s; print(s())"
        self.assertEqual(_python(code)[1], "12")
        self.assertEqual(_python(code, SLOW_ROUTE_CONCURRENCY="5")[1], "5")
        for bad in ("many", "0", "-2"):
            result = _python(code, SLOW_ROUTE_CONCURRENCY=bad)
            self.assertNotEqual(result[0], 0, bad)
            self.assertIn("SLOW_ROUTE_CONCURRENCY", result[2])

    def test_the_real_app_has_the_middleware_and_an_async_health(self):
        import api.app as api_app
        self.assertIn(SlowRouteLimitMiddleware, [m.cls for m in api_app.app.user_middleware])
        self.assertTrue(asyncio.iscoroutinefunction(api_app.health))

    def test_new_variables_are_documented(self):
        example = (ROOT / ".env.example").read_text()
        for name in ("SLOW_ROUTE_CONCURRENCY", "OPENAI_MAX_ATTEMPTS", "OPENAI_CALL_DEADLINE_SECONDS"):
            self.assertIn(name, example)


class SlowRouteLimit(unittest.TestCase):
    """The middleware around a small ASGI app whose slow routes wait for a gate the test controls."""

    def setUp(self):
        self.entered = []

    def build(self, limit):
        gate = asyncio.Event()
        entered = self.entered

        async def inner(scope, receive, send):
            path = scope["path"]
            if path == "/contract-advice":
                raise RuntimeError("the route failed")
            if path == "/rate-intelligence":
                entered.append(path)
                await gate.wait()
            if path == "/pitches/stream":
                entered.append(path)
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
                await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
                await gate.wait()
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            body = b'{"ok": true}'
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})

        middleware = SlowRouteLimitMiddleware(inner, limit=limit)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware, raise_app_exceptions=False), base_url="http://test")
        return middleware, client, gate

    async def until(self, condition, seconds=3.0):
        deadline = time.monotonic() + seconds
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out waiting")
            await asyncio.sleep(0.01)

    def test_full_returns_503_at_once_other_routes_stay_up_and_slots_are_released(self):
        async def scenario():
            middleware, client, gate = self.build(2)
            async with client:
                first = asyncio.create_task(client.post("/rate-intelligence"))
                second = asyncio.create_task(client.post("/rate-intelligence"))
                await self.until(lambda: len(self.entered) == 2)
                started = time.monotonic()
                refused = await client.post("/rate-intelligence")
                self.assertLess(time.monotonic() - started, 0.5, "never queued")
                self.assertEqual(refused.status_code, 503)
                self.assertEqual(refused.headers["retry-after"], "5")
                self.assertEqual(refused.json(), {"detail": "Server is busy, please try again in a few seconds"})
                self.assertEqual(len(self.entered), 2, "the refused request never reached the route")
                for method, path in NOT_SLOW:   # the limit is full, and none of these notice
                    self.assertEqual((await client.request(method, path)).status_code, 200, (method, path))
                self.assertEqual(middleware.in_flight, 2)
                gate.set()
                self.assertEqual([(await first).status_code, (await second).status_code], [200, 200])
                self.assertEqual(middleware.in_flight, 0)
                self.assertEqual((await client.post("/rate-intelligence")).status_code, 200, "released: the next one is accepted")
        asyncio.run(scenario())

    def test_a_route_that_fails_releases_its_slot(self):
        async def scenario():
            middleware, client, gate = self.build(1)
            gate.set()
            async with client:
                for _ in range(3):
                    self.assertEqual((await client.post("/contract-advice")).status_code, 500)
                    self.assertEqual(middleware.in_flight, 0)
                self.assertEqual((await client.post("/rate-intelligence")).status_code, 200)
        asyncio.run(scenario())

    def test_a_cancelled_request_releases_its_slot(self):
        async def scenario():
            middleware, client, gate = self.build(1)
            async with client:
                task = asyncio.create_task(client.post("/rate-intelligence"))
                await self.until(lambda: middleware.in_flight == 1)
                task.cancel()   # the browser went away
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self.until(lambda: middleware.in_flight == 0)
                gate.set()
                self.assertEqual((await client.post("/rate-intelligence")).status_code, 200)
        asyncio.run(scenario())

    def test_a_cut_or_finished_stream_releases_its_slot(self):
        async def scenario():
            middleware, client, gate = self.build(1)
            async with client:
                # cut: the browser leaves in the middle of the stream
                stream = asyncio.create_task(client.post("/pitches/stream"))
                await self.until(lambda: middleware.in_flight == 1)
                refused = await client.post("/pitches/stream")
                self.assertEqual(refused.status_code, 503)
                stream.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await stream
                await self.until(lambda: middleware.in_flight == 0)
                # finished
                gate.set()
                self.assertEqual((await client.post("/pitches/stream")).status_code, 200)
                self.assertEqual(middleware.in_flight, 0)
        asyncio.run(scenario())

    def test_the_slot_is_released_when_the_response_is_complete_not_when_background_work_ends(self):
        async def scenario():
            background = asyncio.Event()

            async def inner(scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"{}"})
                await background.wait()   # work that continues after the response, like a background task

            middleware = SlowRouteLimitMiddleware(inner, limit=1)
            sent = []

            async def send(message):
                sent.append(message)

            async def receive():
                return {"type": "http.request", "body": b""}

            scope = {"type": "http", "method": "POST", "path": "/own-brands", "headers": [], "query_string": b""}
            task = asyncio.create_task(middleware(scope, receive, send))
            await self.until(lambda: len(sent) == 2)
            self.assertEqual(middleware.in_flight, 0, "response complete: the slot is free although the app has not returned")
            background.set()
            await task
            self.assertEqual(middleware.in_flight, 0, "and it is not released a second time")
        asyncio.run(scenario())


class Lifespan(unittest.TestCase):
    def run_lifespan(self, run_migrations=True, recovery_error=None):
        import api.app as api_app
        order = []
        tokens = []

        def recover(*args, **kwargs):
            order.append("recover")
            tokens.append(anyio.to_thread.current_default_thread_limiter().total_tokens)
            if recovery_error:
                raise recovery_error
            return {}
        with mock.patch.object(api_app, "_run_migrations", lambda: order.append("migrate")), mock.patch.object(api_app, "recover_stuck_jobs", recover), \
                mock.patch.object(api_app, "RUN_MIGRATIONS", run_migrations):
            with TestClient(api_app.app) as client:
                self.assertEqual(client.get("/health").json(), {"status": "ok"})
        return order, tokens

    def test_recovery_runs_after_the_migrations_and_the_thread_limit_is_raised(self):
        order, tokens = self.run_lifespan()
        self.assertEqual(order, ["migrate", "recover"])
        self.assertEqual(tokens, [12 + 52])

    def test_recovery_runs_even_when_the_startup_migrations_are_off(self):
        self.assertEqual(self.run_lifespan(run_migrations=False)[0], ["recover"])

    def test_a_failed_recovery_does_not_stop_the_application(self):
        with self.assertLogs("api.app", "ERROR"):
            order, _ = self.run_lifespan(recovery_error=RuntimeError("db down"))
        self.assertEqual(order, ["migrate", "recover"])


class OpenAiRetryBound(unittest.TestCase):
    def setUp(self):
        from pipeline.helpers import gpt_llm
        self.g = gpt_llm
        self.reset()
        self.addCleanup(self.reset)
        for patcher in (mock.patch.object(gpt_llm, "_record_usage"), mock.patch.object(gpt_llm.time, "sleep"),
                        mock.patch.object(gpt_llm, "OPENAI_KEY", "test-key")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def reset(self):
        self.g._consecutive_failures, self.g._stopped_until = 0, 0.0

    @staticmethod
    def ok():
        return httpx.Response(200, json={"usage": {}, "choices": []}, request=httpx.Request("POST", "https://api.openai.com/v1/x"))

    def test_one_failing_call_stops_after_two_attempts_even_when_other_calls_keep_succeeding(self):
        attempts = []

        def post(*args, **kwargs):
            attempts.append(kwargs["timeout"])
            if len(attempts) > 10:
                raise AssertionError("unbounded retry")
            self.g._consecutive_failures = 0   # another thread's call succeeded in between: the shared counter never reaches 2
            raise httpx.ConnectTimeout("no answer")
        with mock.patch.object(self.g.httpx, "post", post), self.assertLogs(self.g.logger, "ERROR"):
            with self.assertRaises(self.g.LLMUnavailableError):
                self.g._post_openai("/chat/completions", {}, 60.0, "test")
        self.assertEqual(len(attempts), 2)

    def test_the_same_with_real_threads(self):
        failing, stop = [], threading.Event()

        def post(*args, **kwargs):
            if threading.current_thread().name == "failing-call":
                failing.append(1)
                if len(failing) > 10:
                    raise AssertionError("unbounded retry")
                raise httpx.ReadTimeout("slow")
            return self.ok()

        def other_calls():
            while not stop.is_set():
                self.g._post_openai("/chat/completions", {}, 60.0, "other")

        outcome = []

        def failing_call():
            try:
                self.g._post_openai("/chat/completions", {}, 60.0, "failing")
            except Exception as exc:
                outcome.append(type(exc).__name__)
        with mock.patch.object(self.g.httpx, "post", post):
            helper = threading.Thread(target=other_calls, daemon=True)
            helper.start()
            caller = threading.Thread(target=failing_call, name="failing-call")
            caller.start()
            caller.join(10)
            stop.set()
            helper.join(5)
        self.assertFalse(caller.is_alive(), "the call ended")
        self.assertEqual(outcome, ["LLMUnavailableError"])
        self.assertLessEqual(len(failing), 2)

    def test_a_retry_that_succeeds_is_returned(self):
        results = iter([httpx.ConnectTimeout("x"), self.ok()])

        def post(*args, **kwargs):
            item = next(results)
            if isinstance(item, Exception):
                raise item
            return item
        with mock.patch.object(self.g.httpx, "post", post), self.assertLogs(self.g.logger, "ERROR"):
            self.assertEqual(self.g._post_openai("/chat/completions", {}, 60.0, "test")["usage"], {})

    def test_the_total_deadline(self):
        attempts, clock = [], [1000.0]

        def post(*args, **kwargs):
            attempts.append(kwargs["timeout"])
            clock[0] += 200   # the attempt took 200 s
            raise httpx.ReadTimeout("slow")
        with mock.patch.object(self.g.httpx, "post", post), mock.patch.object(self.g.time, "monotonic", lambda: clock[0]), self.assertLogs(self.g.logger, "ERROR"):
            with self.assertRaises(self.g.LLMUnavailableError):
                self.g._post_openai("/chat/completions", {}, 60.0, "test")
        self.assertEqual(len(attempts), 1, "past 150 s in all, no second attempt")

    def test_an_attempt_never_waits_longer_than_what_is_left(self):
        seen, clock = [], [0.0]

        def post(*args, **kwargs):
            seen.append(kwargs["timeout"])
            clock[0] += 100
            raise httpx.ReadTimeout("slow")
        with mock.patch.object(self.g.httpx, "post", post), mock.patch.object(self.g.time, "monotonic", lambda: clock[0]), \
                mock.patch.dict(os.environ, {"OPENAI_CALL_DEADLINE_SECONDS": "250"}), self.assertLogs(self.g.logger, "ERROR"):
            with self.assertRaises(self.g.LLMUnavailableError):
                self.g._post_openai("/chat/completions", {}, 180.0, "test")
        self.assertEqual(seen, [180.0, 150.0], "the second attempt is held to the 150 s left of the 250 s")

    def test_environment_overrides_and_bad_values(self):
        with mock.patch.dict(os.environ, {"OPENAI_MAX_ATTEMPTS": "1"}):
            attempts = []

            def post(*args, **kwargs):
                attempts.append(1)
                raise httpx.ConnectTimeout("x")
            with mock.patch.object(self.g.httpx, "post", post), self.assertLogs(self.g.logger, "ERROR"):
                with self.assertRaises(self.g.LLMUnavailableError):
                    self.g._post_openai("/chat/completions", {}, 60.0, "test")
            self.assertEqual(len(attempts), 1)
        self.assertEqual((self.g.max_attempts(), self.g.call_deadline_seconds()), (2, 150))
        for name, bad in (("OPENAI_MAX_ATTEMPTS", "0"), ("OPENAI_MAX_ATTEMPTS", "x"), ("OPENAI_CALL_DEADLINE_SECONDS", "0")):
            self.assertNotEqual(_python("import pipeline.helpers.gpt_llm", **{name: bad})[0], 0, (name, bad))

    def test_the_shared_counter_still_trips_the_breaker(self):
        def post(*args, **kwargs):
            raise httpx.ConnectTimeout("down")
        with mock.patch.object(self.g.httpx, "post", post), mock.patch.object(self.g, "_STOP_AFTER_FAILURES", 2), self.assertLogs(self.g.logger, "ERROR"):
            with self.assertRaises(self.g.LLMUnavailableError):
                self.g._post_openai("/chat/completions", {}, 60.0, "test")
            self.assertGreater(self.g._stopped_until, time.monotonic(), "two failures in a row stop further calls for the cooldown")
            with self.assertRaises(self.g.LLMUnavailableError):
                self.g._post_openai("/chat/completions", {}, 60.0, "test")


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
        from pipeline.db import CreatorProfile
        from pipeline.helpers.passwords import hash_password
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, brands_raw, feature_usage, feature_locks, login_failures RESTART IDENTITY CASCADE"))
        pw = hash_password(PASSWORD)
        self.ids = {}
        with self.factory() as s:
            for key in ("a", "b"):
                row = CreatorProfile(email=f"{key}@example.test", password_hash=pw, is_active=True, full_name="Name", age=30, gender="female",
                                     primary_platform="instagram", follower_count=20000, content_niche="Beauty", instagram_handle="h",
                                     instagram_followers=20000, instagram_description="d")
                s.add(row)
                s.flush()
                self.ids[key] = row.id
            s.commit()

    def token(self, who):
        import api.auth as auth
        return auth._make_jwt(self.ids[who], f"{who}@example.test", 0)

    def client(self, who="a"):
        client = TestClient(self.api_app.app, raise_server_exceptions=False)
        client.cookies.set("access_token", self.token(who))
        return client

    def row(self, who, *columns):
        with self.engine.connect() as conn:
            return conn.execute(text(f"SELECT {', '.join(columns)} FROM creator_profiles WHERE id = :c"), {"c": self.ids[who]}).one()

    # -- restart recovery --------------------------------------------------------------------------------------------------------
    def profile_body(self):
        return {"full_name": "Name", "creator_handle": "h", "age": 30, "gender": "female", "primary_platform": "instagram", "follower_count": 20000,
                "content_niche": "Beauty", "instagram_handle": "h", "instagram_followers": 20000, "instagram_description": "d",
                "instagram_primary_niche": "Beauty"}

    def test_restart_recovers_stuck_jobs_and_the_profile_can_be_saved_again(self):
        from api.recovery import RESTART_MESSAGE, recover_stuck_jobs
        with self.engine.begin() as conn:
            conn.execute(text("""UPDATE creator_profiles SET instagram_description_status = 'running', instagram_description_job_id = 'j1',
                                 youtube_description_status = 'queued', llm_ranking_status = 'queued', llm_v3_ranking_status = 'running'
                                 WHERE id = :a"""), {"a": self.ids["a"]})
            conn.execute(text("""UPDATE creator_profiles SET instagram_description_status = 'completed', youtube_description_status = 'idle',
                                 llm_ranking_status = 'failed', llm_v3_ranking_status = 'completed' WHERE id = :b"""), {"b": self.ids["b"]})
            conn.execute(text("INSERT INTO feature_locks (creator_profile_id, feature) VALUES (:a, 'pitch'), (:b, 'pitch')"), {"a": self.ids["a"], "b": self.ids["b"]})
        with self.engine.begin() as conn:   # the old process's locks are still in the table
            conn.execute(text("""INSERT INTO feature_locks (creator_profile_id, feature) VALUES (:a, 'description:instagram'), (:a, 'description:youtube'),
                                 (:a, 'ranking:v2'), (:a, 'ranking:v3')"""), {"a": self.ids["a"]})
        a = self.client("a")
        before = a.put("/creator-profile/me", json=self.profile_body())
        self.assertEqual(before.status_code, 409, "the stuck job blocks the save, which is what the recovery is for")

        changed = recover_stuck_jobs(self.factory)
        self.assertEqual(changed, {"instagram_description_status": 1, "youtube_description_status": 1, "llm_ranking_status": 1,
                                   "llm_v3_ranking_status": 1, "own_brand_jobs": 0, "feature_locks": 6})
        self.assertEqual(self.row("a", "instagram_description_status", "instagram_description_error", "youtube_description_status",
                                  "youtube_description_error", "llm_ranking_status", "llm_ranking_error", "llm_v3_ranking_status", "llm_v3_ranking_error"),
                         ("failed", RESTART_MESSAGE, "failed", RESTART_MESSAGE, "failed", RESTART_MESSAGE, "failed", RESTART_MESSAGE))
        self.assertEqual(RESTART_MESSAGE, "The server restarted, please try again")
        self.assertEqual(self.row("b", "instagram_description_status", "youtube_description_status", "llm_ranking_status", "llm_v3_ranking_status"),
                         ("completed", "idle", "failed", "completed"), "finished jobs are left alone")
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM feature_locks")).scalar(), 0)

        self.assertEqual(a.put("/creator-profile/me", json=self.profile_body()).status_code, 200, "the profile can be saved again")
        again = a.post("/creator-profile/me/generate-description", json={"platform": "instagram", "username": "x", "niche": "Beauty"})
        self.assertEqual((again.status_code, again.json()["status"]), (200, "queued"), "and a new description job starts instead of the dead one")
        self.assertEqual(recover_stuck_jobs(self.factory)["feature_locks"], 0)

    def test_the_recovery_runs_in_the_lifespan(self):
        import api.recovery as recovery
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET llm_v3_ranking_status = 'queued' WHERE id = :a"), {"a": self.ids["a"]})
        with mock.patch.object(self.api_app, "_run_migrations"), \
                mock.patch.object(self.api_app, "recover_stuck_jobs", lambda: recovery.recover_stuck_jobs(self.factory)):
            with TestClient(self.api_app.app):
                pass
        self.assertEqual(self.row("a", "llm_v3_ranking_status")[0], "failed")

    # -- a real server: slow routes stubbed, /health and login stay quick ----------------------------------------------------------
    def start_server(self):
        import uvicorn
        import api.recovery as recovery
        patchers = (mock.patch.object(self.api_app, "_run_migrations"),
                    mock.patch.object(self.api_app, "recover_stuck_jobs", lambda: recovery.recover_stuck_jobs(self.factory)))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        server = uvicorn.Server(uvicorn.Config(self.api_app.app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
        server.install_signal_handlers = lambda: None
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            self.assertLess(time.monotonic(), deadline, "the test server did not start")
            time.sleep(0.05)

        def stop():
            server.should_exit = True
            thread.join(15)
        self.addCleanup(stop)
        return server.servers[0].sockets[0].getsockname()[1]

    @staticmethod
    def request(port, method, path, body=None, cookie=None, timeout=30):
        started = time.monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"access_token={cookie}"
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data, time.monotonic() - started

    def test_45_slow_requests_while_health_and_login_answer_in_under_a_second(self):
        def slow_estimate(*args, **kwargs):
            time.sleep(3)   # a stubbed slow LLM
            raise RuntimeError("stub")
        port = self.start_server()
        results, lock = [], threading.Lock()

        def slow_call():
            outcome = self.request(port, "POST", "/rate-intelligence", {"brand_name": "b", "deliverable_type": "Reel"}, self.token("a"))
            with lock:
                results.append(outcome)
        with mock.patch("api.advisory.estimate_rate", slow_estimate), mock.patch.dict(os.environ, {"RATE_INTELLIGENCE_PER_DAY": "1000"}):
            threads = [threading.Thread(target=slow_call) for _ in range(45)]
            for thread in threads:
                thread.start()
            time.sleep(1.0)   # the 12 slots are full and the rest have been refused
            probes = []
            for _ in range(3):
                health = self.request(port, "GET", "/health")
                login = self.request(port, "POST", "/auth/login", {"email": "b@example.test", "password": PASSWORD})
                probes.append((health[0], health[3], login[0], login[3]))
            for thread in threads:
                thread.join(30)
        for health_status, health_seconds, login_status, login_seconds in probes:
            self.assertEqual((health_status, login_status), (200, 200))
            self.assertLess(health_seconds, 1.0)
            self.assertLess(login_seconds, 1.0)
        statuses = [r[0] for r in results]
        self.assertEqual(len(statuses), 45)
        self.assertEqual(set(statuses), {502, 503}, "ran (stub error) or refused, nothing else")
        refused = [r for r in results if r[0] == 503]
        self.assertGreaterEqual(len(refused), 25)
        self.assertGreaterEqual(statuses.count(502), 12, "the 12 slots were used")
        self.assertTrue(all(r[1].get("retry-after") == "5" and r[3] < 1.5 for r in refused), "refused at once, with Retry-After: 5")
        self.assertEqual(json.loads(refused[0][2]), {"detail": "Server is busy, please try again in a few seconds"})

    def test_health_answers_while_every_worker_thread_is_busy(self):
        def busy():   # sync, so it runs in the worker threads; not a slow route, so the slow-route limit does not apply
            time.sleep(4)
            return {"ok": True}

        def ping():
            return {"ok": True}
        routes = self.api_app.app.router.routes
        count = len(routes)
        self.api_app.app.add_api_route("/__busy", busy)
        self.api_app.app.add_api_route("/__ping", ping)
        self.addCleanup(lambda: [routes.pop() for _ in range(len(routes) - count)])
        port = self.start_server()
        threads = [threading.Thread(target=self.request, args=(port, "GET", "/__busy")) for _ in range(90)]   # more than the 64 worker threads
        for thread in threads:
            thread.start()
        time.sleep(1.0)
        health = self.request(port, "GET", "/health")
        sync_ping = self.request(port, "GET", "/__ping")
        for thread in threads:
            thread.join(30)
        self.assertEqual(health[0], 200)
        self.assertLess(health[3], 1.0, "/health needs no worker thread")
        self.assertGreater(sync_ping[3], 1.0, "a sync route did have to wait: the threads really were all busy")


if __name__ == "__main__":
    unittest.main()
