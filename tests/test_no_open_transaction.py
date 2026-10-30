"""
No transaction may be open while a request/background path waits on an LLM or Apify (checklist 2.10): the API closes idle-in-transaction
sessions after 60 s, so each site must commit before the slow call. Each test stubs the slow call and records, at that moment, whether the
Session is in a transaction and whether PostgreSQL reports the backend as 'idle in transaction'. Also covers the "batch pool exhausted" message.

Needs a disposable local PostgreSQL (MAVLIT_TEST_PG_URL, same rules as tests/test_migration_postgres.py); the pool test needs none.

Run:  python -m pytest tests/test_no_open_transaction.py -q
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"
os.environ["JWT_SECRET"] = os.environ.get("JWT_SECRET") or "test-jwt-secret"
os.environ["RUN_MIGRATIONS"] = ""
os.environ.pop("MIGRATION_DATABASE_URL", None)
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import TimeoutError as PoolTimeoutError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool, QueuePool  # noqa: E402


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class NoTransactionDuringSlowCalls(unittest.TestCase):
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
        cls.Session = sessionmaker(bind=cls.engine, autoflush=False, expire_on_commit=False)   # as SessionLocal

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        from pipeline.db import CreatorProfile
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, brands_raw RESTART IDENTITY CASCADE"))
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.creator = CreatorProfile(email="c@example.com", full_name="C", creator_handle="c", instagram_handle="c",
                                      instagram_followers=20000, content_niche="Beauty")
        self.db.add(self.creator)
        self.db.commit()
        self.snapshots: list[dict] = []

    def snap(self, label="call"):
        """Called from inside the stubbed slow call."""
        with self.engine.connect() as other:
            idle = other.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state LIKE 'idle in transaction%'")).scalar()
        self.snapshots.append({"label": label, "in_transaction": self.db.in_transaction(), "idle_in_tx_backends": idle})

    def assert_clean(self, expected_calls=1):
        self.assertEqual(len(self.snapshots), expected_calls, self.snapshots)
        for s in self.snapshots:
            self.assertFalse(s["in_transaction"], f"{s['label']}: a transaction was open during the slow call")
            self.assertEqual(s["idle_in_tx_backends"], 0, f"{s['label']}: the server saw an idle-in-transaction backend")

    def test_contract_review(self):
        from pipeline import contract_advice

        def llm(prompt, **kw):
            self.snap("contract llm")
            return {"looks_good": True, "issues": [], "summary": "ok"}

        with mock.patch.object(contract_advice, "call_gpt_json", side_effect=llm):
            contract_advice.review_contract(self.db, self.creator, "Contract text")
        self.assert_clean()

    def test_rate_estimate(self):
        from pipeline import rate_intelligence

        def llm(prompt, **kw):
            self.snap("rate llm")
            return {"rate_min": 100, "rate_typical": 200, "rate_max": 300, "confidence": "low"}

        with mock.patch.object(rate_intelligence, "call_gpt_json", side_effect=llm):
            rate_intelligence.estimate_rate(self.db, self.creator, brand_name="Some Outside Brand", deliverable_type="Reel")
        self.assert_clean()

    def _pitch_kwargs(self, **extra):
        return dict(is_custom=True, brand_id=None, custom_brand_name="Acme", story="s", product_reference=None,
                    past_brand_partnership=None, content_link=None, **extra)

    def test_generate_pitch(self):
        from pipeline import pitching

        def llm(prompt, **kw):
            self.snap("pitch llm")
            return "Hello"

        with mock.patch.object(pitching, "call_gpt_text", side_effect=llm):
            pitching.generate_pitch(self.db, self.creator, **self._pitch_kwargs())
        self.assert_clean()

    def test_stream_pitch_holds_no_transaction_while_streaming(self):
        from pipeline import pitching

        def stream(prompt, **kw):
            self.snap("stream start")
            yield "Hel"
            self.snap("stream middle")
            yield "lo"

        with mock.patch.object(pitching, "stream_gpt_text", side_effect=stream):
            events = list(pitching.stream_pitch(self.db, self.creator, **self._pitch_kwargs()))
        self.assertEqual(events[-1][0], "done")
        self.assert_clean(expected_calls=2)

    def test_pitch_stream_with_post_urls_scrapes_without_a_transaction(self):
        from pipeline import pitch_posts, pitching

        def apify(*a, **kw):
            self.snap("apify")
            return []

        with mock.patch.object(pitch_posts, "APIFY_TOKEN", "x"), mock.patch.object(pitch_posts, "run_apify_actor", side_effect=apify), \
                mock.patch.object(pitching, "stream_gpt_text", side_effect=lambda *a, **k: iter(["ok"])):
            list(pitching.stream_pitch(self.db, self.creator, **{**self._pitch_kwargs(), "content_link": "https://www.instagram.com/p/ABC123/"}))
        self.assert_clean()

    def test_adding_a_creator_post_scrapes_without_a_transaction(self):
        from pipeline import creator_posts, pitch_posts

        def apify(*a, **kw):
            self.snap("apify")
            return []

        with mock.patch.object(pitch_posts, "APIFY_TOKEN", "x"), mock.patch.object(pitch_posts, "run_apify_actor", side_effect=apify):
            with self.assertRaises(LookupError):   # nothing came back from the stub
                creator_posts.add_post(self.db, self.creator, "https://www.instagram.com/p/ABC123/")
        self.assert_clean()

    def test_own_brand_job_llm_call(self):
        from pipeline import own_brand
        from pipeline.db import BrandRaw, InstagramPost, OwnBrandJob
        brand = BrandRaw(name="Acme (@acme)", instagram_handle="acme")
        self.db.add(brand)
        self.db.commit()
        self.db.add(InstagramPost(brand_raw_id=brand.id, instagram_handle="acme", post_id="p1", caption="hi", biography="bio"))
        self.db.add(OwnBrandJob(id="job1", creator_profile_id=self.creator.id, brand_raw_id=brand.id, instagram_handle="acme"))
        self.db.commit()

        def llm(prompt, **kw):
            self.snap("own brand llm")
            return {"niche": "Beauty"}

        with mock.patch.object(own_brand, "SessionLocal", self.Session), mock.patch.object(own_brand, "OPENAI_KEY", "x"), \
                mock.patch.object(own_brand, "call_gpt_json", side_effect=llm):
            own_brand.run_own_brand_job("job1")
        # the job runs in its own session: the snapshot's in_transaction refers to self.db, so check the server side only
        self.assertEqual(len(self.snapshots), 1)
        self.assertEqual(self.snapshots[0]["idle_in_tx_backends"], 0, self.snapshots)


class StreamRouteEndsItsTransaction(unittest.TestCase):
    def test_request_session_is_committed_before_the_stream_starts(self):
        import api.pitches as pitches
        from api.schemas import PitchRequest
        db, user = mock.MagicMock(), mock.MagicMock(id=7)
        body = PitchRequest(is_custom=True, custom_brand_name="Acme", story="s")
        response = pitches.stream_pitch_endpoint(body, db=db, current_user=user)
        # the lock and the daily-cap count commit too; what matters is that the last thing the request session does is a commit
        db.commit.assert_called()
        self.assertEqual(db.method_calls[-1], mock.call.commit())
        self.assertEqual(response.media_type, "text/event-stream")


class BatchPoolExhausted(unittest.TestCase):
    def _exhausted_pool_error(self):
        engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.1)
        held = engine.connect()   # the one connection is taken by "another job"
        self.addCleanup(held.close)
        with self.assertRaises(PoolTimeoutError) as ctx:
            engine.connect()
        return ctx.exception

    def test_safe_error_is_the_retry_message(self):
        import api.app as api_app
        self.assertEqual(api_app._safe_error(self._exhausted_pool_error()), "Another job is running, retry shortly.")

    def test_operator_job_reports_it_instead_of_a_raw_error(self):
        import api.app as api_app
        api_app._jobs["j"] = {"status": "running"}
        session = mock.MagicMock()
        with mock.patch.object(api_app, "BatchSessionLocal", return_value=session), \
                mock.patch.object(api_app, "run_brand_scoring", side_effect=self._exhausted_pool_error()):
            api_app._run_scoring_job("j", 5)
        self.assertEqual(api_app._jobs["j"], {"status": "error", "error": "Another job is running, retry shortly."})

    def test_ranking_job_marks_the_creator_failed_through_the_api_pool(self):
        import api.app as api_app
        batch = mock.MagicMock()
        batch.query.side_effect = self._exhausted_pool_error()
        row = mock.MagicMock()
        api_session = mock.MagicMock()
        api_session.__enter__.return_value = api_session
        api_session.query.return_value.filter.return_value.first.return_value = row
        with mock.patch.object(api_app, "BatchSessionLocal", return_value=batch), mock.patch.object(api_app, "SessionLocal", return_value=api_session):
            api_app._run_ai_ranking_job(1, "v3")
        self.assertEqual((row.llm_v3_ranking_status, row.llm_v3_ranking_error), ("failed", "Another job is running, retry shortly."))
        api_session.commit.assert_called_once()
        batch.close.assert_called_once()

    def test_description_job_marks_the_creator_failed_through_the_api_pool(self):
        import api.app as api_app
        batch = mock.MagicMock()
        batch.query.side_effect = self._exhausted_pool_error()
        row = mock.MagicMock(instagram_description_job_id="job9")
        api_session = mock.MagicMock()
        api_session.__enter__.return_value = api_session
        api_session.query.return_value.filter.return_value.first.return_value = row
        with mock.patch.object(api_app, "BatchSessionLocal", return_value=batch), mock.patch.object(api_app, "SessionLocal", return_value=api_session):
            api_app._run_creator_description_job(1, "instagram", "u", "Beauty", "job9")
        self.assertEqual((row.instagram_description_status, row.instagram_description_error), ("failed", "Another job is running, retry shortly."))

    def test_followup_marks_the_job_failed_so_it_can_be_claimed_again(self):
        from pipeline import own_brand
        batch = mock.MagicMock()
        batch.__enter__.return_value = batch
        batch.get.side_effect = self._exhausted_pool_error()
        job = mock.MagicMock()
        api_session = mock.MagicMock()
        api_session.__enter__.return_value = api_session
        api_session.get.return_value = job
        with mock.patch.object(own_brand, "BatchSessionLocal", return_value=batch), mock.patch.object(own_brand, "SessionLocal", return_value=api_session):
            own_brand.run_followup("job1")   # never raises
        self.assertEqual(job.followup_status, "failed")
        self.assertIsNone(job.followup_started_at)
        self.assertEqual(job.followup_log, [{"step": "all", "error": "Another job is running, retry shortly."}])

    def test_an_exhausted_api_pool_is_a_503(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import api.app as api_app
        err = self._exhausted_pool_error()
        small = FastAPI()
        small.add_exception_handler(PoolTimeoutError, api_app._pool_exhausted)

        @small.get("/x")
        def x():
            raise err

        r = TestClient(small, raise_server_exceptions=False).get("/x")
        self.assertEqual((r.status_code, r.json()), (503, {"detail": "Server busy, retry shortly"}))
        self.assertNotIn("QueuePool", r.text)
        self.assertIn(PoolTimeoutError, api_app.app.exception_handlers)


if __name__ == "__main__":
    unittest.main()
