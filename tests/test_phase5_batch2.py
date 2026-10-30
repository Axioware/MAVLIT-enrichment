"""
Phase 5, items 5.10 and 5.11: paging and heavy-query caps, the in-memory niche cache, the SQLAdmin export cap, Apify timeouts on request paths,
the pitch-stream deadline and the thumbnail size cap. The database tests need a disposable local PostgreSQL (MAVLIT_TEST_PG_URL).
"""

import contextlib
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

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

PASSWORD = "correct horse battery"
BASE_ENV = {"DATABASE_URL": "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable", "JWT_SECRET": "p" * 40, "ADMIN_SESSION_SECRET": "q" * 40}


def _python(code, **env):
    """Run `code` in a fresh interpreter with exactly this environment on top of a minimal one (no .env): (returncode, stdout, stderr)."""
    clean = {k: v for k, v in os.environ.items() if k not in {
        "MAX_OFFSET", "SAVED_BRANDS_PAGE_DEFAULT", "SAVED_BRANDS_PAGE_MAX", "BRAND_POSTS_LIMIT", "NICHE_CACHE_SECONDS", "NICHE_CACHE_MAX_KEYS",
        "ADMIN_EXPORT_MAX_ROWS", "APIFY_REQUEST_TIMEOUT_SECS", "PITCH_STREAM_DEADLINE_SECONDS", "THUMBNAIL_MAX_BYTES", "ENVIRONMENT"}}
    clean.update(BASE_ENV)
    clean.update(env)
    prelude = "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
    result = subprocess.run([sys.executable, "-c", prelude + code], cwd=ROOT, env=clean, capture_output=True, text=True, timeout=120)
    return result.returncode, result.stdout.strip(), result.stderr


class PagingSettings(unittest.TestCase):
    def test_defaults_and_overrides(self):
        code = "import api.paging as p; print(p.MAX_OFFSET, p.SAVED_BRANDS_PAGE_DEFAULT, p.SAVED_BRANDS_PAGE_MAX, p.BRAND_POSTS_LIMIT)"
        self.assertEqual(_python(code)[1], "10000 100 500 200")
        env = dict(MAX_OFFSET="50", SAVED_BRANDS_PAGE_DEFAULT="7", SAVED_BRANDS_PAGE_MAX="20", BRAND_POSTS_LIMIT="9")
        self.assertEqual(_python(code, **env)[1], "50 7 20 9")
        self.assertEqual(_python(code, SAVED_BRANDS_PAGE_DEFAULT="900")[1], "10000 500 500 200", "the default never exceeds the maximum")

    def test_bad_values_stop_startup(self):
        for name, bad in (("MAX_OFFSET", "many"), ("MAX_OFFSET", "-1"), ("SAVED_BRANDS_PAGE_MAX", "0"), ("BRAND_POSTS_LIMIT", "0")):
            code, _, err = _python("import api.paging", **{name: bad})
            self.assertNotEqual(code, 0, (name, bad))
            self.assertIn(name, err)

    def test_the_routes_use_the_settings(self):
        from fastapi.routing import APIRoute
        import api.app as api_app
        import api.pitches as pitches
        import api.saved_brands as saved
        from api import paging

        def offset_le(router_or_app, path):
            route = next(r for r in router_or_app.routes if isinstance(r, APIRoute) and r.path.endswith(path))
            info = next(p.field_info for p in route.dependant.query_params if p.name == "offset")
            return next(m.le for m in info.metadata if hasattr(m, "le"))

        self.assertEqual(offset_le(pitches.router, "/me"), paging.MAX_OFFSET)
        self.assertEqual(offset_le(saved.router, "/me"), paging.MAX_OFFSET)
        matches = next(r for r in api_app.app.router.routes if isinstance(r, APIRoute) and r.path == "/matches/me")
        info = next(p.field_info for p in matches.dependant.query_params if p.name == "offset")
        self.assertEqual(next(m.le for m in info.metadata if hasattr(m, "le")), paging.MAX_OFFSET)


class NicheCache(unittest.TestCase):
    def setUp(self):
        from api.ttl_cache import niche_cache
        niche_cache.clear()
        self.addCleanup(niche_cache.clear)

    def test_ttl_and_keys(self):
        from api.ttl_cache import TTLCache
        now = [1000.0]
        cache, calls = TTLCache(clock=lambda: now[0]), []
        compute = lambda: calls.append(1) or len(calls)
        self.assertEqual(cache.get_or_compute("a", compute), 1)
        self.assertEqual(cache.get_or_compute("a", compute), 1, "served from the cache")
        self.assertEqual(cache.get_or_compute("b", compute), 2, "another key is computed on its own")
        now[0] += 299
        self.assertEqual(cache.get_or_compute("a", compute), 1)
        now[0] += 2   # 301 s after it was stored
        self.assertEqual(cache.get_or_compute("a", compute), 3, "expired after 5 minutes")

    def test_at_most_200_keys_oldest_dropped(self):
        from api.ttl_cache import TTLCache
        cache = TTLCache()
        for n in range(250):
            cache.get_or_compute(("k", n), lambda n=n: n)
        self.assertEqual(len(cache), 200)
        calls = []
        cache.get_or_compute(("k", 0), lambda: calls.append(1))   # the oldest were dropped
        cache.get_or_compute(("k", 249), lambda: calls.append(1))   # the newest are kept
        self.assertEqual(len(calls), 1)

    def test_environment_overrides(self):
        from api.ttl_cache import TTLCache
        now = [0.0]
        with mock.patch.dict(os.environ, {"NICHE_CACHE_SECONDS": "10", "NICHE_CACHE_MAX_KEYS": "2"}):
            cache = TTLCache(clock=lambda: now[0])
            for key in "abc":
                cache.get_or_compute(key, lambda: 1)
            self.assertEqual(len(cache), 2)
            calls = []
            cache.get_or_compute("c", lambda: calls.append(1))
            now[0] = 11
            cache.get_or_compute("c", lambda: calls.append(1))
            self.assertEqual(len(calls), 1, "10 s TTL")
        with mock.patch.dict(os.environ, {"NICHE_CACHE_SECONDS": "0"}):
            cache, calls = TTLCache(), []
            cache.get_or_compute("x", lambda: calls.append(1))
            cache.get_or_compute("x", lambda: calls.append(1))
            self.assertEqual(len(calls), 2, "0 turns the cache off")
        for name, bad in (("NICHE_CACHE_SECONDS", "x"), ("NICHE_CACHE_MAX_KEYS", "0")):
            self.assertNotEqual(_python("import api.ttl_cache", **{name: bad})[0], 0, name)

    def test_the_five_public_endpoints_are_cached_by_their_parameters(self):
        import api.app as api_app
        client = TestClient(api_app.app, raise_server_exceptions=False)
        factory = mock.MagicMock(side_effect=lambda: mock.MagicMock())
        with mock.patch.object(api_app, "SessionLocal", factory):
            for path in ("/brand-niches", "/brand-category-niches", "/creator-sub-niches", "/brand-niche-tags"):
                before = factory.call_count
                self.assertEqual(client.get(path).status_code, 200, path)
                self.assertEqual(client.get(path).status_code, 200, path)
                self.assertEqual(factory.call_count - before, 1, f"{path}: the second call came from the cache")
            before = factory.call_count
            for _ in range(3):
                self.assertEqual(client.get("/brand-category-tags?niche=Music&niche=Beauty").status_code, 200)
            self.assertEqual(factory.call_count - before, 1)
            client.get("/brand-category-tags?niche=Fitness")
            self.assertEqual(factory.call_count - before, 2, "other parameters are another key")
            client.get("/brand-category-tags?niche=Beauty&niche=Music")
            self.assertEqual(factory.call_count - before, 3, "the order of parameters is part of the key (the answer is keyed by name)")

    def test_many_parameter_combinations_cannot_grow_the_cache(self):
        import api.app as api_app
        from api.ttl_cache import niche_cache
        client = TestClient(api_app.app, raise_server_exceptions=False)
        with mock.patch.object(api_app, "SessionLocal", mock.MagicMock()):
            for n in range(230):
                client.get(f"/brand-category-tags?niche=n{n}")
        self.assertLessEqual(len(niche_cache), 200)


class AdminExportCap(unittest.TestCase):
    def test_every_view_that_exports_is_capped(self):
        import api.app as api_app
        exporting = [v for v in api_app.admin.views if getattr(v, "can_export", False)]
        self.assertGreater(len(exporting), 10)
        for view in exporting:
            self.assertEqual(view.export_max_rows, 10000, view.identity)
        for view in api_app.admin.views:
            if not getattr(view, "can_export", True):
                self.assertIn(view.identity, {"instagram-post", "instagram-user", "contract-review", "brand-contact", "creator-profile"})

    def test_override_and_bad_value(self):
        code = "import api.app as a; print({v.export_max_rows for v in a.admin.views if getattr(v, 'can_export', False)})"
        self.assertEqual(_python(code, ADMIN_EXPORT_MAX_ROWS="25")[1], "{25}")
        self.assertNotEqual(_python(code, ADMIN_EXPORT_MAX_ROWS="0")[0], 0)


class ApifyRequestTimeouts(unittest.TestCase):
    class FakeClient:
        calls = []

        def __init__(self, token):
            pass

        def actor(self, actor_id):
            outer = self

            class Actor:
                def call(self, **kwargs):
                    ApifyRequestTimeouts.FakeClient.calls.append(kwargs)
                    return {"status": "SUCCEEDED", "defaultDatasetId": "d"}
            return Actor()

        def dataset(self, _id):
            return SimpleNamespace(iterate_items=lambda: iter([]))

    def setUp(self):
        self.FakeClient.calls = []
        patcher = mock.patch("pipeline.helpers.apify.ApifyClient", self.FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_actor(self):
        from pipeline.helpers.apify import run_apify_actor
        run_apify_actor("actor", {}, label="t", require_success=True)
        return self.FakeClient.calls[-1]

    def test_batch_runs_keep_the_long_limits_and_request_paths_get_90_seconds(self):
        from pipeline.helpers.apify import request_path
        self.assertEqual({k: v for k, v in self.run_actor().items() if k.endswith("secs")}, {"timeout_secs": 600, "wait_secs": 630})
        with request_path():
            self.assertEqual({k: v for k, v in self.run_actor().items() if k.endswith("secs")}, {"timeout_secs": 90, "wait_secs": 90})
        self.assertEqual(self.run_actor()["timeout_secs"], 600, "back to the batch limit after the request path")

    def test_environment_override(self):
        from pipeline.helpers.apify import request_path
        with mock.patch.dict(os.environ, {"APIFY_REQUEST_TIMEOUT_SECS": "45"}), request_path():
            self.assertEqual((self.run_actor()["timeout_secs"], self.run_actor()["wait_secs"]), (45, 45))
        for bad in ("soon", "5"):
            self.assertNotEqual(_python("import pipeline.helpers.apify", APIFY_REQUEST_TIMEOUT_SECS=bad)[0], 0, bad)

    def test_post_scraping_for_a_pitch_or_a_profile_post_uses_the_request_limit(self):
        from pipeline import pitch_posts
        db, creator = mock.MagicMock(), SimpleNamespace(id=1)
        with mock.patch.object(pitch_posts, "APIFY_TOKEN", "x"), mock.patch("pipeline.helpers.apify.APIFY_TOKEN", "x"), \
                mock.patch.object(pitch_posts, "record_apify_scrape"):
            pitch_posts._scrape_posts(db, creator, {"abc": "https://www.instagram.com/p/abc/"})
        self.assertEqual((self.FakeClient.calls[-1]["timeout_secs"], self.FakeClient.calls[-1]["wait_secs"]), (90, 90))

    def test_description_generation_uses_the_request_limit(self):
        import api.app as api_app
        from pipeline.helpers import apify
        seen = []
        row = mock.MagicMock(instagram_description_job_id="job", instagram_handle="h")

        def generate(*args, **kwargs):
            seen.append(apify._limits())
            raise RuntimeError("stop here")
        db = mock.MagicMock()
        db.query.return_value.filter.return_value.first.return_value = row
        with mock.patch.object(api_app, "BatchSessionLocal", return_value=db), mock.patch.object(api_app, "generate_instagram_description", generate), \
                mock.patch.object(api_app, "SessionLocal", return_value=mock.MagicMock()):
            api_app._run_creator_description_job(1, "instagram", "someone", "Beauty", "job")
        self.assertEqual(seen, [(90, 90)])
        self.assertEqual(apify._limits(), (600, 630), "and the batch limit again afterwards")

    def test_own_brand_lookup_uses_the_request_limit(self):
        from pipeline import own_brand
        from pipeline.helpers import apify
        seen = []
        job = SimpleNamespace(id=5, status="queued", creator_profile_id=1, instagram_handle="brand", brand_raw_id=5)
        db = mock.MagicMock()
        db.get.return_value = job
        db.__enter__.return_value = db

        def scrape(*args, **kwargs):
            seen.append(apify._limits())
            return None
        with mock.patch.object(own_brand, "SessionLocal", return_value=db), mock.patch.object(own_brand, "missing_fields", return_value=["name"]), \
                mock.patch.object(own_brand, "_post_count", return_value=0), mock.patch.object(own_brand, "APIFY_TOKEN", "x"), \
                mock.patch.object(own_brand, "scrape_and_save_posts", scrape), mock.patch.object(own_brand, "_set_stage"), \
                mock.patch.object(own_brand, "_fail") as fail:
            own_brand.run_own_brand_job("job-1")
        self.assertEqual(seen, [(90, 90)])
        fail.assert_called_once()


class ThumbnailCap(unittest.TestCase):
    def setUp(self):
        import api.creator_posts as cp
        self.cp = cp
        cp._thumb_cache.clear()
        self.addCleanup(cp._thumb_cache.clear)

    @staticmethod
    def fake_stream(chunks, headers=None, status=200):
        seen = {}

        @contextlib.contextmanager
        def stream(method, url, **kwargs):
            seen.update(kwargs, method=method, url=url)
            yield SimpleNamespace(status_code=status, headers={"content-type": "image/jpeg", **(headers or {})}, iter_bytes=lambda: iter(chunks))
        return stream, seen

    def test_a_small_picture_is_returned_with_the_10_second_timeout(self):
        stream, seen = self.fake_stream([b"abc", b"def"])
        with mock.patch.object(self.cp.httpx, "stream", stream):
            self.assertEqual(self.cp._fetch_thumbnail("CODE1"), (b"abcdef", "image/jpeg"))
        self.assertEqual((seen["timeout"], seen["follow_redirects"]), (10, True))
        self.assertIn("CODE1", self.cp._thumb_cache)

    def test_more_than_2_mb_is_refused_by_content_length_or_by_counting(self):
        big = 2 * 1024 * 1024 + 1
        stream, _ = self.fake_stream([b"x"], headers={"content-length": str(big)})
        with mock.patch.object(self.cp.httpx, "stream", stream), self.assertRaises(self.cp.ThumbnailTooLarge):
            self.cp._fetch_thumbnail("BIG1")
        stream, _ = self.fake_stream([b"x" * (1024 * 1024)] * 3)   # no Content-Length: counted while reading
        with mock.patch.object(self.cp.httpx, "stream", stream), self.assertRaises(self.cp.ThumbnailTooLarge):
            self.cp._fetch_thumbnail("BIG2")
        stream, _ = self.fake_stream([b"x" * (1024 * 1024)] * 2)   # exactly 2 MB is fine
        with mock.patch.object(self.cp.httpx, "stream", stream):
            self.assertEqual(len(self.cp._fetch_thumbnail("EXACT")[0]), 2 * 1024 * 1024)
        self.assertNotIn("BIG1", self.cp._thumb_cache)
        self.assertNotIn("BIG2", self.cp._thumb_cache)

    def test_environment_override(self):
        stream, _ = self.fake_stream([b"x" * 5000])
        with mock.patch.dict(os.environ, {"THUMBNAIL_MAX_BYTES": "4096"}), mock.patch.object(self.cp.httpx, "stream", stream):
            with self.assertRaises(self.cp.ThumbnailTooLarge):
                self.cp._fetch_thumbnail("ENV1")
        for bad in ("big", "10"):
            self.assertNotEqual(_python("import api.creator_posts", THUMBNAIL_MAX_BYTES=bad)[0], 0, bad)

    def test_other_failures_are_still_just_no_picture(self):
        for status, headers in ((404, None), (200, {"content-type": "text/html"})):
            stream, _ = self.fake_stream([b"x"], headers=headers, status=status)
            with mock.patch.object(self.cp.httpx, "stream", stream):
                self.assertIsNone(self.cp._fetch_thumbnail("NONE"))


class StreamDeadlineSetting(unittest.TestCase):
    def test_setting(self):
        import api.pitches as pitches
        self.assertEqual(pitches.stream_deadline_seconds(), 150)
        with mock.patch.dict(os.environ, {"PITCH_STREAM_DEADLINE_SECONDS": "40"}):
            self.assertEqual(pitches.stream_deadline_seconds(), 40)
        for bad in ("soon", "0"):
            self.assertNotEqual(_python("import api.pitches", PITCH_STREAM_DEADLINE_SECONDS=bad)[0], 0, bad)


class EnvDocumented(unittest.TestCase):
    def test_every_new_variable_is_in_env_example(self):
        example = (ROOT / ".env.example").read_text()
        for name in ("MAX_OFFSET", "SAVED_BRANDS_PAGE_DEFAULT", "SAVED_BRANDS_PAGE_MAX", "BRAND_POSTS_LIMIT", "NICHE_CACHE_SECONDS", "NICHE_CACHE_MAX_KEYS",
                     "ADMIN_EXPORT_MAX_ROWS", "APIFY_REQUEST_TIMEOUT_SECS", "PITCH_STREAM_DEADLINE_SECONDS", "THUMBNAIL_MAX_BYTES"):
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
                        mock.patch.object(api_pitches, "SessionLocal", factory)):
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

    def client(self, who="a"):
        import api.auth as auth
        client = TestClient(self.api_app.app, raise_server_exceptions=False)
        client.cookies.set("access_token", auth._make_jwt(self.ids[who], f"{who}@example.test", 0))
        return client

    def value(self, sql, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    # -- offsets ---------------------------------------------------------------------------------------------------------------
    def test_offset_is_capped_at_10000(self):
        c = self.client()
        for path in ("/matches/me", "/pitches/me", "/saved-brands/me"):
            self.assertEqual(c.get(f"{path}?offset=10001").status_code, 422, path)
            self.assertNotEqual(c.get(f"{path}?offset=10000").status_code, 422, path)
        self.assertEqual(c.get("/pitches/me?offset=10000").json()["pitches"], [])

    # -- saved brands pages ------------------------------------------------------------------------------------------------------
    def save_many(self, who, n):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO brands_raw (name) SELECT 'Brand ' || g FROM generate_series(1, :n) g"), {"n": n})
            conn.execute(text("""INSERT INTO saved_brands (creator_profile_id, brand_raw_id, created_at)
                                 SELECT :c, id, now() - (id || ' minutes')::interval FROM brands_raw"""), {"c": self.ids[who]})

    def test_saved_brands_are_paged_and_stay_a_plain_list(self):
        self.save_many("a", 150)
        c = self.client()
        default = c.get("/saved-brands/me")
        self.assertEqual(default.status_code, 200)
        self.assertIsInstance(default.json(), list)
        self.assertEqual(len(default.json()), 100, "default page")
        self.assertEqual(default.headers["X-Total-Count"], "150")
        self.assertEqual(set(default.json()[0]), {"brand_id", "name", "niche", "short_bio", "saved_at"} & set(default.json()[0]) | set(default.json()[0]))
        everything = c.get("/saved-brands/me?limit=500").json()
        self.assertEqual(len(everything), 150)
        self.assertEqual(everything[:100], default.json(), "the first page is the start of the full list (newest first)")
        page = c.get("/saved-brands/me?limit=20&offset=140").json()
        self.assertEqual([b["brand_id"] for b in page], [b["brand_id"] for b in everything[140:150]])
        self.assertEqual(c.get("/saved-brands/me?limit=500&offset=200").json(), [])
        saved_at = [b["saved_at"] for b in everything]
        self.assertEqual(saved_at, sorted(saved_at, reverse=True))

    def test_saved_brands_limits_and_isolation(self):
        self.save_many("a", 5)
        c = self.client()
        for query in ("limit=0", "limit=501", "limit=-1", "offset=-1", "offset=10001"):
            self.assertEqual(c.get("/saved-brands/me?" + query).status_code, 422, query)
        self.assertEqual(c.get("/saved-brands/me?limit=500").status_code, 200)
        b = self.client("b").get("/saved-brands/me")
        self.assertEqual((b.json(), b.headers["X-Total-Count"]), ([], "0"))

    # -- partnership posts limit -------------------------------------------------------------------------------------------------
    def test_brand_posts_are_limited_per_table_and_newest_first(self):
        from pipeline.db import BrandRaw, InstagramPost, TestCreatorBrandPartnershipPost
        with self.factory() as s:
            brand = BrandRaw(name="Posts Brand")
            s.add(brand)
            s.flush()
            for n in range(1, 8):
                s.add(InstagramPost(brand_raw_id=brand.id, instagram_handle="brand", post_id=f"ig{n}", post_url=f"https://i/p/ig{n}", timestamp=f"2026-01-0{n}T10:00:00Z",
                                    paid_partnership=True, sponsorship_confidence=95, tagged_users=[f"creator{n}"]))
                s.add(TestCreatorBrandPartnershipPost(brand_raw_id=brand.id, creator_username=f"re{n}", post_url=f"https://i/p/re{n}",
                                                      post_timestamp=f"2026-02-0{n}T10:00:00Z", sponsorship_confidence=95))
            s.commit()
            brand_id = brand.id
        c = self.client()
        with mock.patch.object(self.api_app, "BRAND_POSTS_LIMIT", 3):
            posts = c.get(f"/matches/me/brands/{brand_id}/posts").json()["posts"]
        urls = [p["post_url"] for p in posts]
        self.assertEqual(len(urls), 6, "3 from each of the two tables")
        self.assertEqual(urls, ["https://i/p/re7", "https://i/p/re6", "https://i/p/re5", "https://i/p/ig7", "https://i/p/ig6", "https://i/p/ig5"])
        self.assertEqual(len(c.get(f"/matches/me/brands/{brand_id}/posts").json()["posts"]), 14, "the default limit (200) shows them all")

    # -- pitch stream deadline ---------------------------------------------------------------------------------------------------
    PITCH = {"is_custom": True, "custom_brand_name": "Acme", "story": "x"}

    def lock_rows(self):
        return self.value("SELECT count(*) FROM feature_locks WHERE creator_profile_id = :c", c=self.ids["a"])

    def test_a_stalled_pitch_stream_is_stopped_at_the_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)
        finished = []

        def stalled(*args, **kwargs):
            yield "status", "Reading the linked posts"
            release.wait(30)   # an LLM / scrape call that does not answer
            yield "delta", "too late"
            finished.append(1)

        c = self.client()
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"PITCH_STREAM_DEADLINE_SECONDS": "1"}), mock.patch("api.pitches.stream_pitch", stalled), \
                self.assertLogs("api.pitches", "WARNING") as logs:
            response = c.post("/pitches/stream", json=self.PITCH)
        elapsed = time.monotonic() - started
        events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
        self.assertEqual(events, [{"type": "status", "text": "Reading the linked posts"},
                                  {"type": "error", "detail": "Pitch generation is temporarily unavailable, try again"}])
        self.assertLess(elapsed, 10)
        self.assertIn("deadline", "\n".join(logs.output))
        self.assertEqual(self.lock_rows(), 0, "the in-flight lock is released")
        release.set()   # the stuck worker wakes up, sees the cancel flag and stops without sending anything more
        time.sleep(0.3)
        self.assertEqual(finished, [])
        with mock.patch("api.pitches.generate_pitch", side_effect=RuntimeError("no LLM in tests")):
            self.assertEqual(self.client().post("/pitches", json=self.PITCH).status_code, 502, "not 409: the user can pitch again")

    def test_a_fast_stream_is_unaffected_and_a_crash_is_a_generic_error(self):
        c = self.client()

        def quick(*args, **kwargs):
            yield "status", "hello"
            yield "delta", "Hi"

        with mock.patch("api.pitches.stream_pitch", quick):
            events = [json.loads(line[5:]) for line in c.post("/pitches/stream", json=self.PITCH).text.splitlines() if line.startswith("data:")]
        self.assertEqual(events, [{"type": "status", "text": "hello"}, {"type": "delta", "text": "Hi"}])
        self.assertEqual(self.lock_rows(), 0)

        def crash(*args, **kwargs):
            raise KeyError("secret internals")
            yield

        with mock.patch("api.pitches.stream_pitch", crash), self.assertLogs("api.pitches", "ERROR"):
            response = c.post("/pitches/stream", json=self.PITCH)
        self.assertNotIn("secret internals", response.text)
        self.assertIn("temporarily unavailable", response.text)
        self.assertEqual(self.lock_rows(), 0)

    # -- thumbnail route ---------------------------------------------------------------------------------------------------------
    def test_an_oversized_thumbnail_is_a_502_error(self):
        from pipeline.db import CreatorInstagramPost
        with self.factory() as s:
            row = CreatorInstagramPost(creator_profile_id=self.ids["a"], instagram_handle="h", post_url="https://www.instagram.com/p/ABC123/", post_id="x1")
            s.add(row)
            s.commit()
            post_id = row.id
        with mock.patch("api.creator_posts._fetch_thumbnail", side_effect=__import__("api.creator_posts", fromlist=["x"]).ThumbnailTooLarge()):
            r = self.client().get(f"/creator-profile/me/posts/{post_id}/thumbnail")
        self.assertEqual((r.status_code, r.json()["detail"]), (502, "This post's picture is too large to show"))


if __name__ == "__main__":
    unittest.main()
