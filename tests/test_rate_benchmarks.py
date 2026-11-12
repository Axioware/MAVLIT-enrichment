"""
The rate data of Rate Intelligence lives in the rate_benchmarks table (it was pipeline/data/rate_buckets_with_views_by_niche.csv): the migration fills it once from
pipeline/rate_benchmark_seed.py, the estimate reads it, and the admin page edits it. The database tests need MAVLIT_TEST_PG_URL.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402


class SeedData(unittest.TestCase):
    def test_the_seed_is_the_72_rows_of_the_old_csv_and_is_valid(self):
        from pipeline import rate_intelligence as ri
        from pipeline.rate_benchmark_seed import COLUMNS, ROWS
        self.assertEqual(len(ROWS), 72)
        keys = [r[:3] for r in ROWS]
        self.assertEqual(len(set(keys)), 72)                                              # one row per niche x bucket x deliverable
        niches, buckets, deliverables = set(ri._DATASET_NICHE.values()), {n for _, n in ri.FOLLOWER_BUCKETS}, set(ri._DATASET_DELIVERABLE.values())
        self.assertEqual(({k[0] for k in keys}, {k[1] for k in keys}, {k[2] for k in keys}), (niches, buckets, deliverables))
        self.assertEqual(len(keys), len(niches) * len(buckets) * len(deliverables))     # nothing missing: 3 x 6 x 4
        for row in ROWS:
            rec = dict(zip(COLUMNS, row))
            self.assertEqual(len(row), len(COLUMNS))
            self.assertTrue(0 <= rec["min_usd"] <= rec["median_usd"] <= rec["max_usd"], rec)
            self.assertGreater(rec["creators_in_bucket"], 0)
        beauty = next(dict(zip(COLUMNS, r)) for r in ROWS if r[:3] == ("Beauty", "5k-25k", "Reel"))
        self.assertEqual((beauty["creators_in_bucket"], beauty["price_points"], beauty["min_usd"], beauty["median_usd"], beauty["views_median"]), (280, 301, 13.33, 350.0, 8300.0))

    def test_the_csv_is_gone_and_the_code_no_longer_reads_a_file(self):
        self.assertFalse((ROOT / "pipeline" / "data" / "rate_buckets_with_views_by_niche.csv").exists())
        source = (ROOT / "pipeline" / "rate_intelligence.py").read_text()
        for gone in ("import csv", "open(", "_DATA_FILE", ".csv\""):
            self.assertNotIn(gone, source)
        self.assertIn("RateBenchmark", source)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class FromTheDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        host = url.query.get("host") or url.host or ""
        if not ((host in ("", "localhost", "127.0.0.1") or str(host).startswith("/")) and any(w in (url.database or "") for w in ("test", "scratch"))):
            raise RuntimeError("MAVLIT_TEST_PG_URL must be a LOCAL database whose name contains 'test' or 'scratch'; refusing to run")
        cls.engine = create_engine(TEST_URL, poolclass=NullPool)
        cls.Session = sessionmaker(bind=cls.engine, autoflush=False, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        from pipeline import migrations, rate_intelligence
        with self.engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        migrations.run_migrations(self.engine)
        rate_intelligence.invalidate_rate_cache()
        self.addCleanup(rate_intelligence.invalidate_rate_cache)
        self.db = self.Session()
        self.addCleanup(self.db.close)

    def count(self):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT count(*) FROM rate_benchmarks")).scalar()

    # -- the migration ---------------------------------------------------------------------------------------------------
    def test_the_migration_fills_the_table_once_and_never_overwrites_an_edit_or_brings_back_a_delete(self):
        from pipeline import migrations
        self.assertEqual(self.count(), 72)
        migrations.run_migrations(self.engine)                                            # a second start: nothing doubles
        self.assertEqual(self.count(), 72)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE rate_benchmarks SET median_usd = 999 WHERE niche = 'Beauty' AND follower_bucket = '5k-25k' AND deliverable = 'Reel'"))
            conn.execute(text("DELETE FROM rate_benchmarks WHERE niche = 'Music & Dance' AND follower_bucket = '600k+' AND deliverable = 'Live'"))
        migrations.run_migrations(self.engine)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT median_usd FROM rate_benchmarks WHERE niche = 'Beauty' AND follower_bucket = '5k-25k' AND deliverable = 'Reel'")).scalar(), 999)
        self.assertEqual(self.count(), 71)                                                # the edit stayed and the deleted row did not come back

    # -- the estimate reads the table ------------------------------------------------------------------------------------
    def test_rate_data_comes_from_the_table_and_an_edit_shows_after_the_cache_is_cleared(self):
        from pipeline import rate_intelligence as ri
        self.assertEqual(ri.rate_row(self.db, "Beauty", "5k-25k", "Reel")["median"], 350.0)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE rate_benchmarks SET median_usd = 400 WHERE niche = 'Beauty' AND follower_bucket = '5k-25k' AND deliverable = 'Reel'"))
        self.assertEqual(ri.rate_row(self.db, "Beauty", "5k-25k", "Reel")["median"], 350.0)   # still the cached copy (at most a minute old)
        ri.invalidate_rate_cache()
        self.assertEqual(ri.rate_row(self.db, "Beauty", "5k-25k", "Reel")["median"], 400.0)
        text_row, ladder = ri._rate_data_text(self.db, "Beauty", "5k-25k", "Reel", 10000)
        self.assertIn("median $400", text_row)
        self.assertEqual(ladder.count("\n"), 5)                                           # the six-bucket ladder
        self.assertIsNone(ri.rate_row(self.db, "Fashion", "5k-25k", "Reel"))              # a niche the data does not cover: no row, as before

    def test_the_cache_expires_after_a_minute(self):
        from pipeline import rate_intelligence as ri
        ri.invalidate_rate_cache()
        with mock.patch.object(ri.time, "monotonic", return_value=1000.0):
            ri.rate_row(self.db, "Beauty", "5k-25k", "Reel")
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE rate_benchmarks SET median_usd = 123 WHERE niche = 'Beauty' AND follower_bucket = '5k-25k' AND deliverable = 'Reel'"))
        with mock.patch.object(ri.time, "monotonic", return_value=1000.0 + ri.RATE_CACHE_SECONDS - 1):
            self.assertEqual(ri.rate_row(self.db, "Beauty", "5k-25k", "Reel")["median"], 350.0)
        with mock.patch.object(ri.time, "monotonic", return_value=1000.0 + ri.RATE_CACHE_SECONDS + 1):
            self.assertEqual(ri.rate_row(self.db, "Beauty", "5k-25k", "Reel")["median"], 123.0)

    def test_an_empty_table_gives_the_no_data_wording_instead_of_an_error(self):
        from pipeline import rate_intelligence as ri
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM rate_benchmarks"))
        ri.invalidate_rate_cache()
        text_row, ladder = ri._rate_data_text(self.db, "Beauty", "5k-25k", "Reel", 10000)
        self.assertEqual((text_row, ladder), ("No rate data for Beauty, 5k-25k, Reel.", "none"))
        self.assertFalse(ri.views_adjustment(self.db, "Beauty", "5k-25k", "Reel", 90000)["applied"])

    def test_the_views_check_uses_the_table(self):
        from pipeline import rate_intelligence as ri
        adj = ri.views_adjustment(self.db, "Beauty", "5k-25k", "Reel", 40000)             # well above the bucket's median views: priced from the next bucket
        self.assertTrue(adj["applied"])
        self.assertEqual(adj["next_bucket"], "25k-50k")

    def test_a_whole_estimate_puts_the_table_rows_in_the_prompt(self):
        from pipeline import rate_intelligence as ri
        from pipeline.db import CreatorProfile
        creator = CreatorProfile(email="c@example.com", full_name="C", creator_handle="c", instagram_handle="c", instagram_followers=10000, content_niche="Beauty")
        self.db.add(creator)
        self.db.commit()
        prompts = []

        def llm(prompt, **kw):
            prompts.append(prompt)
            return {"rate_min": 100, "rate_typical": 200, "rate_max": 300, "confidence": "low"}

        with mock.patch.object(ri, "call_gpt_json", side_effect=llm):
            estimate = ri.estimate_rate(self.db, creator, brand_name="Some Outside Brand", deliverable_type="Reel")
        self.assertEqual(len(prompts), 1)
        self.assertIn("280 creators, 301 price points: min $13, median $350", prompts[0])   # the Beauty / 5k-25k / Reel row, as the CSV gave it
        self.assertEqual(estimate.details["data_row"]["median"], 350.0)



class NoAdminPage(unittest.TestCase):
    """The rate data has no page in the backend admin: it stays in the database, the estimate reads it, and the table owner changes it with SQL."""

    def test_there_is_no_admin_view_and_no_leftover_code_for_one(self):
        import api.app as api_app
        from pipeline import db_grants
        self.assertFalse([v for v in api_app.admin.views if getattr(getattr(v, "model", None), "__tablename__", "") == "rate_benchmarks"])
        self.assertFalse(hasattr(api_app, "RateBenchmarkAdmin"))
        self.assertIsNone(db_grants.ADMIN["rate_benchmarks"])                                  # the grants plan says "no admin page" ...
        self.assertEqual(db_grants.table_privileges()["rate_benchmarks"], "S")                 # ... so the API role can only read the table
        self.assertEqual(db_grants.table_privileges(include_admin=False)["rate_benchmarks"], "S")

    def test_the_estimate_still_reads_the_table_and_the_other_rate_pages_are_untouched(self):
        import api.app as api_app
        from pipeline import rate_intelligence as ri
        self.assertTrue(callable(ri.invalidate_rate_cache) and ri.RATE_CACHE_SECONDS == 60)
        self.assertTrue([v for v in api_app.admin.views if getattr(getattr(v, "model", None), "__tablename__", "") == "rate_estimates"])   # the estimates log page stays
