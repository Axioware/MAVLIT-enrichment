"""
Phase 6, batch 2: bounded executors for background jobs, per-user job locks, the operator job registry, stale-job reporting, restart recovery
with the executors, and pitch streams that hold no worker thread. The database tests need a disposable local PostgreSQL (MAVLIT_TEST_PG_URL).
"""

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
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from api import jobs  # noqa: E402
from api.jobs import JobExecutor, QueueFull  # noqa: E402

PASSWORD = "correct horse battery"
BASE_ENV = {"DATABASE_URL": "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable", "JWT_SECRET": "p" * 40, "ADMIN_SESSION_SECRET": "q" * 40}
SETTINGS = ("USER_JOB_WORKERS", "USER_JOB_QUEUE_MAX", "OPERATOR_JOB_QUEUE_MAX", "OPERATOR_JOB_STALE_SECONDS", "OPERATOR_JOB_KEEP_SECONDS",
            "JOB_LOCK_STALE_SECONDS", "PITCH_STREAM_MAX")


def _python(code, **env):
    clean = {k: v for k, v in os.environ.items() if k not in SETTINGS + ("ENVIRONMENT",)}
    clean.update(BASE_ENV)
    clean.update(env)
    result = subprocess.run([sys.executable, "-c", "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n" + code], cwd=ROOT, env=clean,
                            capture_output=True, text=True, timeout=120)
    return result.returncode, result.stdout.strip(), result.stderr


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.02)


class ExecutorBasics(unittest.TestCase):
    def test_jobs_run_on_at_most_the_configured_workers(self):
        executor = JobExecutor("t", workers=3, queue_max=20)
        self.addCleanup(executor.shutdown, 1)
        gate, lock, now_running, peak, done = threading.Event(), threading.Lock(), [0], [0], []

        def job():
            with lock:
                now_running[0] += 1
                peak[0] = max(peak[0], now_running[0])
            gate.wait(5)
            with lock:
                now_running[0] -= 1
                done.append(1)
        for _ in range(9):
            executor.submit(job)
        wait_for(lambda: now_running[0] == 3)
        time.sleep(0.2)
        self.assertEqual((now_running[0], peak[0], executor.pending()), (3, 3, 6))
        gate.set()
        wait_for(lambda: len(done) == 9)
        self.assertEqual(peak[0], 3)

    def test_a_full_queue_raises_and_a_failing_job_does_not_kill_its_worker(self):
        executor = JobExecutor("t", workers=1, queue_max=2)
        self.addCleanup(executor.shutdown, 1)
        gate, ran = threading.Event(), []
        executor.submit(lambda: gate.wait(5))
        wait_for(lambda: executor.running == 1)
        executor.submit(lambda: ran.append("b"))
        executor.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertFalse(executor.has_room())
        with self.assertRaises(QueueFull):
            executor.submit(lambda: ran.append("never"))
        with self.assertLogs("api.jobs", "ERROR"):
            gate.set()
            wait_for(lambda: executor.pending() == 0 and executor.running == 0)
        executor.submit(lambda: ran.append("c"))
        wait_for(lambda: ran == ["b", "c"])
        self.assertTrue(executor.has_room())

    def test_shutdown_does_not_hang_on_a_busy_worker_and_the_executor_can_start_again(self):
        executor = JobExecutor("t", workers=1, queue_max=5)
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        executor.submit(lambda: stuck.wait(30))   # a job that does not finish
        wait_for(lambda: executor.running == 1)
        executor.submit(lambda: None)
        executor.submit(lambda: None)
        started = time.monotonic()
        dropped = executor.shutdown(timeout=0.5)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(dropped, 2, "the waiting jobs are dropped")
        ran = []
        executor.submit(lambda: ran.append(1))   # a later submit starts fresh workers
        wait_for(lambda: ran == [1])
        stuck.set()
        executor.shutdown(timeout=1)

    def test_shutdown_of_an_idle_executor_is_quick(self):
        executor = JobExecutor("t", workers=4, queue_max=5)
        executor.submit(lambda: None)
        time.sleep(0.1)
        started = time.monotonic()
        executor.shutdown()
        self.assertLess(time.monotonic() - started, 2.0)

    def test_the_defaults_and_the_settings(self):
        self.assertEqual((jobs.user_job_workers(), jobs.user_job_queue_max(), jobs.operator_job_queue_max(), jobs.operator_job_stale_seconds(),
                          jobs.operator_job_keep_seconds()), (4, 50, 10, 21600, 3600))
        self.assertEqual(jobs.user_jobs.workers, 4)
        self.assertEqual(jobs.operator_jobs.workers, 1)
        from api import usage_limits
        self.assertEqual(usage_limits.job_lock_stale_seconds(), 1800)
        with mock.patch.dict(os.environ, {"USER_JOB_WORKERS": "2", "USER_JOB_QUEUE_MAX": "7", "JOB_LOCK_STALE_SECONDS": "90"}):
            self.assertEqual((jobs.user_job_workers(), jobs.user_job_queue_max(), usage_limits.job_lock_stale_seconds()), (2, 7, 90))

    def test_bad_values_stop_startup_and_everything_is_documented(self):
        for name in ("USER_JOB_WORKERS", "USER_JOB_QUEUE_MAX", "OPERATOR_JOB_QUEUE_MAX", "OPERATOR_JOB_STALE_SECONDS", "OPERATOR_JOB_KEEP_SECONDS"):
            for bad in ("lots", "0"):
                code, _, err = _python("import api.jobs", **{name: bad})
                self.assertNotEqual(code, 0, (name, bad))
                self.assertIn(name, err)
        self.assertNotEqual(_python("import api.usage_limits", JOB_LOCK_STALE_SECONDS="5")[0], 0)
        self.assertNotEqual(_python("import api.pitches", PITCH_STREAM_MAX="0")[0], 0)
        example = (ROOT / ".env.example").read_text()
        for name in SETTINGS:
            self.assertIn(name, example, name)


class OperatorJobs(unittest.TestCase):
    def setUp(self):
        import api.app as api_app
        self.app = api_app
        with api_app._jobs_lock:
            api_app._jobs.clear()
        self.addCleanup(api_app._jobs.clear)
        self.addCleanup(jobs.operator_jobs.shutdown, 1)
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)

    def blocking(self, job_id, *args):
        self.app._jobs[job_id].update({"status": "running"})
        self.gate.wait(10)
        self.app._jobs[job_id].update({"status": "done"})

    def test_one_job_of_a_kind_at_a_time_and_other_kinds_wait_in_the_queue(self):
        from fastapi import HTTPException
        first = self.app._start_operator_job("seed", {"niche": "x"}, self.blocking)
        wait_for(lambda: self.app._jobs[first]["status"] == "running")
        with self.assertRaises(HTTPException) as refused:
            self.app._start_operator_job("seed", {"niche": "y"}, self.blocking)
        self.assertEqual(refused.exception.status_code, 409)
        other = self.app._start_operator_job("enrich", {"results": {}}, self.blocking)   # one worker: it waits its turn
        self.assertEqual(self.app._get_job(other)["status"], "queued")
        self.gate.set()
        wait_for(lambda: self.app._get_job(other)["status"] == "done")
        again = self.app._start_operator_job("seed", {"niche": "z"}, lambda job_id, *a: self.app._jobs[job_id].update({"status": "done"}))
        wait_for(lambda: self.app._get_job(again)["status"] == "done")

    def test_finished_jobs_are_forgotten_after_an_hour_and_stale_ones_fail(self):
        from fastapi import HTTPException
        done = self.app._start_operator_job("score", {"scored": 0}, lambda job_id, *a: self.app._jobs[job_id].update({"status": "done"}))
        wait_for(lambda: self.app._jobs[done].get("finished_at") is not None)
        self.assertEqual(self.app._get_job(done)["status"], "done")
        self.app._jobs[done]["finished_at"] -= 3601
        with self.assertRaises(HTTPException) as gone:
            self.app._get_job(done)
        self.assertEqual(gone.exception.status_code, 404)
        self.assertNotIn(done, self.app._jobs)
        self.app._jobs["old"] = {"kind": "enrich", "status": "running", "created_at": time.time() - 7 * 3600}
        report = self.app._get_job("old")
        self.assertEqual((report["status"], report["error"]), ("error", jobs.STALE_MESSAGE))
        # and it no longer blocks a new job of its kind
        fresh = self.app._start_operator_job("enrich", {"results": {}}, lambda job_id, *a: self.app._jobs[job_id].update({"status": "done"}))
        wait_for(lambda: self.app._get_job(fresh)["status"] == "done")

    def test_a_full_operator_queue_is_503_and_leaves_no_entry(self):
        from fastapi import HTTPException
        with mock.patch.object(jobs, "operator_jobs", JobExecutor("op", 1, 1)) as small, mock.patch.object(self.app, "operator_jobs", small):
            self.addCleanup(small.shutdown, 1)
            small.submit(lambda: self.gate.wait(10))
            wait_for(lambda: small.running == 1)
            small.submit(lambda: None)
            with self.assertRaises(HTTPException) as refused:
                self.app._start_operator_job("seed", {"niche": "x"}, self.blocking)
        self.assertEqual((refused.exception.status_code, refused.exception.headers["Retry-After"]), (503, "5"))
        self.assertEqual(self.app._jobs, {})

    def test_the_http_routes(self):
        from api.operator import require_operator
        self.app.app.dependency_overrides[require_operator] = lambda: None
        self.addCleanup(self.app.app.dependency_overrides.pop, require_operator, None)
        client = TestClient(self.app.app, raise_server_exceptions=False)
        with mock.patch.object(self.app, "_run_seed_job", self.blocking):
            first = client.post("/seed", json={"niche": "tea"})
            self.assertEqual((first.status_code, first.json()["status"]), (200, "queued"))
            self.assertEqual(client.post("/seed", json={"niche": "coffee"}).status_code, 409)
            wait_for(lambda: client.get(f"/seed/status/{first.json()['job_id']}").json()["status"] == "running")
            self.gate.set()
            wait_for(lambda: client.get(f"/seed/status/{first.json()['job_id']}").json()["status"] == "done")
        self.assertEqual(client.get("/seed/status/nope").status_code, 404)


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
        self.started = []   # (job name, args) of every job the executors really ran
        self.gate = threading.Event()   # held closed: the jobs stay alive (and keep their locks)
        self.addCleanup(self.gate.set)

        def fake(name):
            def job(*args, **kwargs):
                self.started.append((name, args))
                self.gate.wait(20)
            return job
        for patcher in (mock.patch.object(attempt_limits, "SessionLocal", factory), mock.patch.object(api_app, "SessionLocal", factory),
                        mock.patch.object(api_pitches, "SessionLocal", factory),
                        mock.patch.object(api_app, "_run_creator_signals_job", fake("signals")),
                        mock.patch.object(api_app, "_run_ai_ranking_job", fake("ranking")),
                        mock.patch.object(api_app, "_run_creator_description_job", fake("description")),
                        mock.patch("api.own_brands.run_own_brand_job", fake("own_brand"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(jobs.user_jobs.shutdown, 1)
        self.addCleanup(self.gate.set)
        self.factory = factory
        self._seed()

    def _seed(self):
        from pipeline.db import CreatorProfile
        from pipeline.helpers.passwords import hash_password
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, brands_raw, own_brand_jobs, feature_usage, feature_locks, login_failures RESTART IDENTITY CASCADE"))
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

    def value(self, sql, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def locks(self, who="a"):
        with self.engine.connect() as conn:
            return sorted(r[0] for r in conn.execute(text("SELECT feature FROM feature_locks WHERE creator_profile_id = :c"), {"c": self.ids[who]}))

    def uses(self, who, feature):
        return self.value("SELECT coalesce(sum(uses), 0) FROM feature_usage WHERE creator_profile_id = :c AND feature = :f", c=self.ids[who], f=feature)

    def clicks(self, count, call):
        """`count` simultaneous requests, each from its own client, started together. Returns their responses."""
        barrier, out = threading.Barrier(count), []

        def click():
            client = self.client("a")
            barrier.wait()
            out.append(call(client))
        threads = [threading.Thread(target=click) for _ in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return out

    def small_queue(self, workers=1, queue_max=1, fill=None):
        """Replace the user-job executor with a tiny, already full one (one running job, queue_max waiting)."""
        executor = JobExecutor("small", workers, queue_max)
        patches = (mock.patch.object(jobs, "user_jobs", executor), mock.patch.object(self.api_app, "user_jobs", executor))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(executor.shutdown, 1)
        for _ in range(workers):
            executor.submit(lambda: self.gate.wait(20))
        wait_for(lambda: executor.running == workers)
        for _ in range(queue_max if fill is None else fill):
            executor.submit(lambda: self.gate.wait(20))
        self.assertEqual(executor.has_room(), fill is not None and fill < queue_max)
        return executor

    # -- simultaneous clicks make one job -------------------------------------------------------------------------------------------
    def test_simultaneous_ranking_refreshes_start_one_job(self):
        results = self.clicks(8, lambda c: c.post("/matches/me/v3/refresh"))
        self.assertEqual([r.status_code for r in results], [200] * 8)
        wait_for(lambda: len(self.started) >= 1)
        time.sleep(0.3)
        self.assertEqual([name for name, _ in self.started], ["ranking"], "one job, not eight")
        self.assertEqual(self.uses("a", "match_refresh"), 1)
        self.assertEqual(self.locks(), ["ranking:v3"])
        self.assertTrue({r.json()["status"] for r in results} <= {"idle", "queued", "running"})
        self.gate.set()
        wait_for(lambda: self.locks() == [])   # released when the job ends

    def test_simultaneous_description_requests_start_one_job(self):
        body = {"platform": "instagram", "username": "someone", "niche": "Beauty"}
        results = self.clicks(8, lambda c: c.post("/creator-profile/me/generate-description", json=body))
        wait_for(lambda: len(self.started) >= 1)
        time.sleep(0.3)
        self.assertEqual([name for name, _ in self.started], ["description"])
        self.assertEqual(self.uses("a", "description_generation"), 1)
        self.assertTrue(all(r.status_code in (200, 409) for r in results), [r.status_code for r in results])
        self.assertGreaterEqual([r.status_code for r in results].count(200), 1)
        self.assertEqual(self.locks(), ["description:instagram"])
        # instagram and youtube are separate jobs
        yt = self.client().post("/creator-profile/me/generate-description", json={**body, "platform": "youtube"})
        self.assertEqual(yt.status_code, 200)
        self.assertEqual(self.locks(), ["description:instagram", "description:youtube"])

    def test_simultaneous_own_brand_requests_start_one_job_and_another_brand_waits(self):
        from pipeline.db import OwnBrandJob
        made = []

        def start(db, creator_id, raw_handle):
            job = OwnBrandJob(id=f"job-{len(made)}", creator_profile_id=creator_id, instagram_handle=raw_handle, status="queued")
            db.add(job)
            db.commit()
            made.append(job.id)
            return job
        with mock.patch("api.own_brands.start_own_brand_job", start):
            results = self.clicks(8, lambda c: c.post("/own-brands", json={"instagram": "brandone"}))
            wait_for(lambda: len(self.started) >= 1)
            time.sleep(0.3)
            self.assertEqual(len(made), 1, "one job row")
            self.assertEqual([name for name, _ in self.started], ["own_brand"])
            self.assertEqual({r.status_code for r in results}, {200})
            self.assertEqual({r.json()["job_id"] for r in results}, {"job-0"}, "everyone got the same job")
            self.assertEqual(self.uses("a", "own_brands"), 1)
            other = self.client().post("/own-brands", json={"instagram": "brandtwo"})
            self.assertEqual(other.status_code, 409, "a different brand waits for the first")
            self.assertEqual(len(made), 1)
            self.assertEqual(self.uses("a", "own_brands"), 1, "and is not counted")
            self.assertEqual(self.client("b").post("/own-brands", json={"instagram": "brandtwo"}).status_code, 200, "another user is not blocked")
            self.gate.set()
            wait_for(lambda: self.locks("a") == [])
            self.assertEqual(self.client().post("/own-brands", json={"instagram": "brandtwo"}).status_code, 200, "after the first ends")

    def test_a_known_brand_runs_no_job_and_keeps_no_lock(self):
        ready = SimpleNamespace(id="j", status="ready", stage=None, error=None, brand_raw_id=None)
        with mock.patch("api.own_brands.start_own_brand_job", return_value=ready):
            self.assertEqual(self.client().post("/own-brands", json={"instagram": "known"}).status_code, 200)
        self.assertEqual((self.locks(), self.started), ([], []))

    # -- isolation -------------------------------------------------------------------------------------------------------------------
    def test_one_users_job_does_not_block_another(self):
        self.assertEqual(self.client("a").post("/matches/me/v3/refresh").status_code, 200)
        wait_for(lambda: len(self.started) == 1)
        b = self.client("b").post("/matches/me/v3/refresh")
        self.assertEqual(b.status_code, 200)
        wait_for(lambda: len(self.started) == 2)
        self.assertEqual((self.locks("a"), self.locks("b")), (["ranking:v3"], ["ranking:v3"]))
        self.assertEqual((self.uses("a", "match_refresh"), self.uses("b", "match_refresh")), (1, 1))

    # -- a full queue -----------------------------------------------------------------------------------------------------------------
    def test_a_full_queue_is_503_with_retry_after_and_costs_nothing(self):
        self.small_queue()
        a = self.client()
        checks = [("/matches/me/v3/refresh", None, "match_refresh"),
                  ("/creator-profile/me/generate-description", {"platform": "instagram", "username": "x", "niche": "Beauty"}, "description_generation"),
                  ("/own-brands", {"instagram": "brandone"}, "own_brands")]
        for path, body, feature in checks:
            response = a.post(path, **({"json": body} if body else {}))
            self.assertEqual(response.status_code, 503, path)
            self.assertEqual(response.headers["Retry-After"], "5", path)
            self.assertEqual(response.json()["detail"], "Server is busy, please try again in a few seconds")
            self.assertEqual(self.uses("a", feature), 0, f"{path} is not charged to the daily cap")
        self.assertEqual(self.locks(), [])
        self.assertEqual(self.value("SELECT llm_v3_ranking_status FROM creator_profiles WHERE id = :c", c=self.ids["a"]), "idle")
        self.assertEqual(self.value("SELECT count(*) FROM own_brand_jobs"), 0)

    def test_losing_the_race_for_the_last_place_undoes_everything(self):
        a = self.client()
        with mock.patch.object(jobs.user_jobs, "submit", side_effect=QueueFull("x")):
            for path, body, feature in (("/matches/me/v3/refresh", None, "match_refresh"),
                                        ("/creator-profile/me/generate-description", {"platform": "instagram", "username": "x", "niche": "Beauty"},
                                         "description_generation")):
                response = a.post(path, **({"json": body} if body else {}))
                self.assertEqual(response.status_code, 503, path)
                self.assertEqual(self.uses("a", feature), 0, "refunded")
                self.assertEqual(self.locks(), [], "lock released")
            self.assertEqual(self.value("SELECT llm_v3_ranking_status FROM creator_profiles WHERE id = :c", c=self.ids["a"]), "idle")
            self.assertEqual(self.value("SELECT instagram_description_status FROM creator_profiles WHERE id = :c", c=self.ids["a"]), "idle")
            with mock.patch("api.own_brands.start_own_brand_job", lambda db, cid, h: SimpleNamespace(id="j", status="queued", stage=None, error=None, brand_raw_id=None)):
                self.assertEqual(a.post("/own-brands", json={"instagram": "brandone"}).status_code, 503)
        self.assertEqual((self.uses("a", "own_brands"), self.locks()), (0, []))

    def test_a_profile_save_with_a_full_queue_still_saves(self):
        self.small_queue()
        body = {"full_name": "New Name", "creator_handle": "h", "age": 30, "gender": "female", "primary_platform": "instagram", "follower_count": 20000,
                "content_niche": "Beauty", "instagram_handle": "h", "instagram_followers": 20000, "instagram_description": "d", "instagram_primary_niche": "Beauty"}
        self.assertEqual(self.client().put("/creator-profile/me", json=body).status_code, 200)
        self.assertEqual(self.value("SELECT full_name FROM creator_profiles WHERE id = :c", c=self.ids["a"]), "New Name")
        self.assertEqual(self.locks(), [])

    # -- profile save and its signals job --------------------------------------------------------------------------------------------
    def profile_body(self):
        return {"full_name": "Name", "creator_handle": "h", "age": 30, "gender": "female", "primary_platform": "instagram", "follower_count": 20000,
                "content_niche": "Beauty", "instagram_handle": "h", "instagram_followers": 20000, "instagram_description": "d", "instagram_primary_niche": "Beauty"}

    def test_a_profile_save_queues_one_signals_job_and_skips_it_while_a_ranking_runs(self):
        a = self.client()
        self.assertEqual(a.put("/creator-profile/me", json=self.profile_body()).status_code, 200)
        wait_for(lambda: len(self.started) == 1)
        self.assertEqual(self.started[0][0], "signals")
        self.assertEqual(self.locks(), ["ranking:v3"], "v2 is paused")
        self.assertEqual(self.value("SELECT llm_v3_ranking_status FROM creator_profiles WHERE id = :c", c=self.ids["a"]), "queued")
        self.assertEqual(a.put("/creator-profile/me", json=self.profile_body()).status_code, 200)   # saved again while the first job runs
        time.sleep(0.3)
        self.assertEqual(len(self.started), 1, "no second job: the running one holds the ranking lock")
        self.gate.set()
        wait_for(lambda: self.locks() == [])

    # -- stale jobs ------------------------------------------------------------------------------------------------------------------
    def set_status(self, who, **columns):
        sets = ", ".join(f"{k} = :{k}" for k in columns)
        with self.engine.begin() as conn:
            conn.execute(text(f"UPDATE creator_profiles SET {sets} WHERE id = :c"), {**columns, "c": self.ids[who]})

    def set_lock(self, who, feature, age):
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM feature_locks WHERE creator_profile_id = :c AND feature = :f"), {"c": self.ids[who], "f": feature})
            conn.execute(text("INSERT INTO feature_locks (creator_profile_id, feature, started_at) VALUES (:c, :f, now() - :age * interval '1 second')"),
                         {"c": self.ids[who], "f": feature, "age": age})

    def test_a_running_job_without_a_live_lock_is_reported_failed(self):
        a = self.client()
        self.set_status("a", llm_v3_ranking_status="running", instagram_description_status="running", youtube_description_status="queued")
        self.set_lock("a", "ranking:v3", 2 * 3600)   # stale: started two hours ago
        self.set_lock("a", "description:instagram", 60)   # alive
        profile = a.get("/creator-profile/me").json()
        self.assertEqual(profile["instagram_description_status"], "running", "a live job is left alone")
        self.assertEqual(profile["youtube_description_status"], "failed", "no lock at all: not alive")
        self.assertEqual(profile["youtube_description_error"], jobs.STALE_MESSAGE)
        matches = a.get("/matches/me/v3").json()
        self.assertEqual(matches["status"], "failed", "the ranking whose lock is two hours old")
        self.assertEqual(self.value("SELECT llm_v3_ranking_status FROM creator_profiles WHERE id = :c", c=self.ids["a"]), "failed")
        self.assertEqual(self.value("SELECT llm_v3_ranking_error FROM creator_profiles WHERE id = :c", c=self.ids["a"]), jobs.STALE_MESSAGE)

    def test_a_stale_job_no_longer_blocks_a_new_one(self):
        self.set_status("a", llm_v3_ranking_status="running", instagram_description_status="running", instagram_description_job_id="dead")
        self.set_lock("a", "ranking:v3", 3 * 3600)
        self.set_lock("a", "description:instagram", 3 * 3600)
        a = self.client()
        self.assertEqual(a.post("/matches/me/v3/refresh").status_code, 200)
        wait_for(lambda: [n for n, _ in self.started] == ["ranking"])
        self.assertEqual(self.locks(), ["description:instagram", "ranking:v3"])
        again = a.post("/creator-profile/me/generate-description", json={"platform": "instagram", "username": "x", "niche": "Beauty"})
        self.assertEqual(again.status_code, 200)
        self.assertNotEqual(again.json()["job_id"], "dead")
        wait_for(lambda: len(self.started) == 2)

    def test_the_lock_stale_time_comes_from_the_environment(self):
        self.set_status("a", llm_v3_ranking_status="running")
        self.set_lock("a", "ranking:v3", 120)
        a = self.client()
        self.assertEqual(a.get("/matches/me/v3").json()["status"], "running", "two minutes old, limit 30 minutes")
        with mock.patch.dict(os.environ, {"JOB_LOCK_STALE_SECONDS": "60"}):
            self.assertEqual(a.get("/matches/me/v3").json()["status"], "failed", "two minutes old, limit one minute")

    # -- restart: jobs waiting in memory are lost and marked failed -----------------------------------------------------------------------
    def test_jobs_waiting_in_the_queue_are_lost_on_shutdown_and_marked_failed_by_the_recovery(self):
        from api.recovery import RESTART_MESSAGE, recover_stuck_jobs
        executor = self.small_queue(workers=1, queue_max=4, fill=3)
        a = self.client()
        self.assertEqual(a.post("/matches/me/v3/refresh").status_code, 200)   # waits in the queue behind the full ones
        self.assertEqual(self.value("SELECT llm_v3_ranking_status FROM creator_profiles WHERE id = :c", c=self.ids["a"]), "queued")
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO own_brand_jobs (id, creator_profile_id, instagram_handle, status) VALUES ('j1', :c, 'brandone', 'queued')"), {"c": self.ids["a"]})
        dropped = executor.shutdown(timeout=0.5)   # the process stops
        self.assertGreaterEqual(dropped, 1, "the waiting jobs are gone from memory")
        changed = recover_stuck_jobs(self.factory)   # the next start
        self.assertEqual(self.value("SELECT llm_v3_ranking_status FROM creator_profiles WHERE id = :c", c=self.ids["a"]), "failed")
        self.assertEqual(self.value("SELECT llm_v3_ranking_error FROM creator_profiles WHERE id = :c", c=self.ids["a"]), RESTART_MESSAGE)
        self.assertEqual(self.value("SELECT status FROM own_brand_jobs WHERE id = 'j1'"), "failed")
        self.assertEqual(self.value("SELECT error FROM own_brand_jobs WHERE id = 'j1'"), RESTART_MESSAGE)
        self.assertEqual((changed["own_brand_jobs"], self.locks()), (1, []))

    def test_the_application_shuts_its_executors_down_without_hanging(self):
        seen = []
        with mock.patch.object(self.api_app, "_run_migrations"), mock.patch.object(self.api_app, "recover_stuck_jobs"):
            started = time.monotonic()
            with TestClient(self.api_app.app) as client:
                client.cookies.set("access_token", self.token("a"))
                self.assertEqual(client.post("/matches/me/v3/refresh").status_code, 200)
                wait_for(lambda: len(self.started) == 1)   # a job is running and will not finish
            seen.append(time.monotonic() - started)
        self.assertLess(seen[0], 12, "leaving the lifespan waits a few seconds for the running job, then gives up")

    # -- streams hold no worker thread -------------------------------------------------------------------------------------------------
    def start_server(self, app):
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
        server.install_signal_handlers = lambda: None
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        wait_for(lambda: server.started, 20)

        def stop():
            server.should_exit = True
            thread.join(15)
        self.addCleanup(stop)
        return server.servers[0].sockets[0].getsockname()[1]

    @staticmethod
    def http(port, method, path, body=None, cookie=None):
        started = time.monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"access_token={cookie}"
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data, time.monotonic() - started

    def test_45_open_streams_hold_no_worker_thread_and_health_login_and_sync_routes_stay_fast(self):
        from api.auth import router as auth_router
        from api.concurrency import SlowRouteLimitMiddleware
        from api.pitches import router as pitches_router
        from pipeline.db import CreatorProfile, get_db
        from pipeline.helpers.passwords import hash_password
        import api.auth as auth
        pw = hash_password(PASSWORD)
        with self.factory() as s:   # 45 more creators, one stream each
            extra = [CreatorProfile(email=f"s{n}@example.test", password_hash=pw, is_active=True, full_name="Name", age=30, gender="female",
                                    primary_platform="instagram", follower_count=20000, content_niche="Beauty", instagram_handle="h",
                                    instagram_followers=20000, instagram_description="d") for n in range(45)]
            s.add_all(extra)
            s.commit()
            ids = [row.id for row in extra]
        app2 = FastAPI()
        app2.include_router(auth_router)
        app2.include_router(pitches_router)
        app2.add_middleware(SlowRouteLimitMiddleware, limit=60)

        @app2.get("/health")
        async def health():
            return {"status": "ok"}

        @app2.get("/__ping")
        def ping():   # a sync route: it needs a worker thread
            return {"ok": True}

        @app2.get("/__tokens")
        async def tokens():
            return {"borrowed": anyio.to_thread.current_default_thread_limiter().borrowed_tokens}

        def override():
            with self.factory() as session:
                yield session
        app2.dependency_overrides[get_db] = override

        def slow_stream(*args, **kwargs):
            yield "status", "reading"
            time.sleep(3)   # an LLM that is slow to answer
            yield "delta", "Hello"
        port = self.start_server(app2)
        results, lock = [], threading.Lock()

        def stream(creator_id):
            outcome = self.http(port, "POST", "/pitches/stream", {"is_custom": True, "custom_brand_name": "Acme", "story": "x"}, auth._make_jwt(creator_id, "x@x", 0))
            with lock:
                results.append(outcome)
        with mock.patch("api.pitches.stream_pitch", slow_stream), mock.patch.dict(os.environ, {"PITCH_STREAM_MAX": "60"}):
            threads = [threading.Thread(target=stream, args=(creator_id,)) for creator_id in ids]
            for t in threads:
                t.start()
            time.sleep(1.2)   # all 45 are open, waiting for the slow LLM
            probes = {"health": self.http(port, "GET", "/health"), "ping": self.http(port, "GET", "/__ping"),
                      "login": self.http(port, "POST", "/auth/login", {"email": "a@example.test", "password": PASSWORD}),
                      "tokens": self.http(port, "GET", "/__tokens")}
            for t in threads:
                t.join(30)
        for name in ("health", "ping", "login"):
            self.assertEqual(probes[name][0], 200, name)
            self.assertLess(probes[name][2], 1.0, f"{name} answered in {probes[name][2]:.2f}s")
        self.assertLessEqual(json.loads(probes["tokens"][1])["borrowed"], 3, "45 open streams borrow no worker threads")
        self.assertEqual(len(results), 45)
        self.assertEqual({r[0] for r in results}, {200})
        self.assertTrue(all(b"Hello" in r[1] for r in results), "every stream delivered its text")

    def test_at_most_six_streams_at_once_and_the_slot_is_released(self):
        import api.pitches as pitches
        pitches.stream_slots.clear()
        self.addCleanup(pitches.stream_slots.clear)
        release = threading.Event()
        self.addCleanup(release.set)

        def waiting(*args, **kwargs):
            yield "status", "reading"
            release.wait(20)
            yield "delta", "done"
        users = [self.ids["a"], self.ids["b"]]
        extra = []
        from pipeline.db import CreatorProfile
        with self.factory() as s:
            for n in range(6):
                row = CreatorProfile(email=f"x{n}@example.test", password_hash="x", is_active=True, full_name="N", age=30, gender="female",
                                     primary_platform="instagram", follower_count=20000, content_niche="Beauty", instagram_handle="h",
                                     instagram_followers=20000, instagram_description="d")
                s.add(row)
                s.flush()
                extra.append(row.id)
            s.commit()
        import api.auth as auth
        from api.concurrency import SlowRouteLimitMiddleware  # noqa: F401
        port = self.start_server(self.api_app.app)
        outcomes = []

        def open_stream(creator_id):
            outcomes.append(self.http(port, "POST", "/pitches/stream", {"is_custom": True, "custom_brand_name": "Acme", "story": "x"},
                                      auth._make_jwt(creator_id, "x@x", 0)))
        with mock.patch("api.pitches.stream_pitch", waiting):
            threads = [threading.Thread(target=open_stream, args=(creator_id,)) for creator_id in (users + extra)[:6]]
            for t in threads:
                t.start()
            wait_for(lambda: len(pitches.stream_slots._held) == 6)
            seventh = self.http(port, "POST", "/pitches/stream", {"is_custom": True, "custom_brand_name": "Acme", "story": "x"}, auth._make_jwt(extra[-1], "x@x", 0))
            self.assertEqual(seventh[0], 503)
            self.assertEqual(self.uses_for(extra[-1]), 0, "a refused stream costs nothing")
            release.set()
            for t in threads:
                t.join(30)
        self.assertEqual({o[0] for o in outcomes}, {200})
        wait_for(lambda: len(pitches.stream_slots._held) == 0)
        with mock.patch("api.pitches.stream_pitch", waiting):
            again = self.http(port, "POST", "/pitches/stream", {"is_custom": True, "custom_brand_name": "Acme", "story": "x"}, auth._make_jwt(extra[-1], "x@x", 0))
        self.assertEqual(again[0], 200, "the slots were released")

    def uses_for(self, creator_id):
        return self.value("SELECT coalesce(sum(uses), 0) FROM feature_usage WHERE creator_profile_id = :c AND feature = 'pitches'", c=creator_id)


if __name__ == "__main__":
    unittest.main()
