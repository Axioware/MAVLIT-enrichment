"""A brand added by a wrong Instagram handle (no such account / no posts) is taken back out of brands_raw.
Needs MAVLIT_TEST_PG_URL for the database part."""
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


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class WrongHandleLeavesNothingBehind(unittest.TestCase):
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
        cls.Session = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE own_brand_jobs, instagram_posts, brands_raw, creator_profiles RESTART IDENTITY CASCADE"))
            conn.execute(text("INSERT INTO creator_profiles (id, email) VALUES (1, 'a@example.test')"))

    def run_job(self, scraped):
        from pipeline import own_brand
        with self.Session() as db:
            job = own_brand.start_own_brand_job(db, 1, "https://www.instagram.com/vinylgonejdgsa")
            job_id = job.id
        with mock.patch.object(own_brand, "APIFY_TOKEN", "x"), mock.patch.object(own_brand, "SessionLocal", self.Session), \
                mock.patch.object(own_brand, "scrape_and_save_posts", return_value=scraped):
            own_brand.run_own_brand_job(job_id)
        return job_id

    def count(self, sql):
        with self.engine.connect() as conn:
            return conn.execute(text(sql)).scalar()

    def test_no_posts_removes_the_new_brand_and_keeps_the_failed_job(self):
        job_id = self.run_job(0)
        self.assertEqual(self.count("SELECT count(*) FROM brands_raw"), 0)
        with self.engine.connect() as conn:
            status, brand_id, error = conn.execute(text("SELECT status, brand_raw_id, error FROM own_brand_jobs WHERE id = :i"), {"i": job_id}).one()
        self.assertEqual(status, "failed")
        self.assertIsNone(brand_id)
        self.assertIn("No posts found", error)

    def test_instagram_unreachable_keeps_the_brand(self):
        self.run_job(None)
        self.assertEqual(self.count("SELECT count(*) FROM brands_raw"), 1)

    def test_a_brand_that_existed_before_is_never_removed(self):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO brands_raw (name, instagram_handle, source) VALUES ('Known', 'https://www.instagram.com/vinylgonejdgsa', 'wikidata')"))
        self.run_job(0)
        self.assertEqual(self.count("SELECT count(*) FROM brands_raw"), 1)

    def test_a_brand_another_job_uses_is_kept(self):
        from pipeline import own_brand
        with self.Session() as db:
            job = own_brand.start_own_brand_job(db, 1, "vinylgonejdgsa")
            brand_id = job.brand_raw_id
            job_id = job.id
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO creator_profiles (id, email) VALUES (2, 'b@example.test')"))
            conn.execute(text("INSERT INTO own_brand_jobs (id, creator_profile_id, brand_raw_id, instagram_handle) VALUES ('other', 2, :b, 'vinylgonejdgsa')"), {"b": brand_id})
        with self.Session() as db:
            self.assertFalse(own_brand.remove_unusable_brand(db, db.get(own_brand.OwnBrandJob, job_id)))
        self.assertEqual(self.count("SELECT count(*) FROM brands_raw"), 1)


if __name__ == "__main__":
    unittest.main()
