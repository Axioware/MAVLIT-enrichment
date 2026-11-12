"""The scratch tables test_brands_with_instagram_posts and test_niches (and the scripts that filled them) are gone: the migration drops them, and nothing in the code
refers to them. Needs MAVLIT_TEST_PG_URL for the database part."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

TABLES = ("test_brands_with_instagram_posts", "test_niches")
SCRIPTS = ("test_active_sponsorships.py", "scripts/populate_test_niches.py", "scripts/embed_test_niches.py", "scripts/niche_cosine_similarity.py")


class NothingRefersToThem(unittest.TestCase):
    def test_no_model_view_grant_or_script_is_left(self):
        import api.app as api_app
        from pipeline import db, db_grants
        for name in ("TestBrandsWithInstagramPosts", "TestNiche"):
            self.assertFalse(hasattr(db, name), name)
        self.assertFalse({t.name for t in db.Base.metadata.sorted_tables} & set(TABLES))
        self.assertFalse(set(db_grants.ADMIN) & set(TABLES))
        for name in ("TestBrandsWithInstagramPostsAdmin", "TestNicheAdmin"):
            self.assertFalse(hasattr(api_app, name), name)
        self.assertFalse([v for v in api_app.admin.views if getattr(v, "identity", "") in ("test-brands-with-instagram-posts", "test-niche")])
        for script in SCRIPTS:
            self.assertFalse((ROOT / script).exists(), script)
        for sql in (ROOT / "scripts" / "sql").glob("*.sql"):
            for table in TABLES:
                self.assertNotIn(table, sql.read_text(), sql.name)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class MigrationDropsTheTables(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        host = url.query.get("host") or url.host or ""
        if not ((host in ("", "localhost", "127.0.0.1") or str(host).startswith("/")) and any(w in (url.database or "") for w in ("test", "scratch"))):
            raise RuntimeError("MAVLIT_TEST_PG_URL must be a LOCAL database whose name contains 'test' or 'scratch'; refusing to run")
        cls.engine = create_engine(TEST_URL, poolclass=NullPool)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def exists(self, table):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}).scalar()

    def test_an_old_database_that_still_has_the_tables_loses_them_and_nothing_else(self):
        from pipeline import migrations
        with self.engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        migrations.run_migrations(self.engine)
        self.assertFalse(any(self.exists(t) for t in TABLES))                                  # a fresh database never has them
        with self.engine.begin() as conn:                                                      # an old one (like production today) does
            conn.execute(text("CREATE TABLE test_brands_with_instagram_posts (id serial PRIMARY KEY, brand_raw_id int NOT NULL, brand_name text)"))
            conn.execute(text("INSERT INTO test_brands_with_instagram_posts (brand_raw_id, brand_name) VALUES (1, 'x')"))
            conn.execute(text("CREATE TABLE test_niches (niche text PRIMARY KEY, description text)"))
            conn.execute(text("INSERT INTO test_niches VALUES ('Music', 'd')"))
            conn.execute(text("INSERT INTO brands_raw (name) VALUES ('Real brand')"))
        self.assertTrue(all(self.exists(t) for t in TABLES))
        migrations.run_migrations(self.engine)
        self.assertFalse(any(self.exists(t) for t in TABLES))                                  # dropped
        for kept in ("creator_niches", "brand_match_profile"):                # the real niche and matching tables are untouched
            self.assertTrue(self.exists(kept), kept)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM brands_raw")).scalar(), 1)   # real data untouched
        migrations.run_migrations(self.engine)                                                 # and running it again is harmless
        self.assertFalse(any(self.exists(t) for t in TABLES))
