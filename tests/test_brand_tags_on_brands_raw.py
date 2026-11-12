"""brands_niches is gone: its tags live in brands_raw.tags. The migration moves them over and drops the table; the code reads and
writes brands_raw. Needs MAVLIT_TEST_PG_URL for the database part."""
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

CODE = [p for p in list((ROOT / "pipeline").rglob("*.py")) + list((ROOT / "api").rglob("*.py")) if "migrations.py" not in p.name]


class NothingRefersToTheOldTable(unittest.TestCase):
    def test_no_model_grant_or_query_is_left(self):
        from pipeline import db, db_grants
        self.assertFalse(hasattr(db, "BrandNiche"))
        self.assertIn("tags", db.BrandRaw.__table__.columns)
        self.assertNotIn("brands_niches", {t.name for t in db.Base.metadata.sorted_tables})
        self.assertNotIn("brands_niches", set(db_grants.ADMIN) | set(db_grants.CODE))
        for path in CODE:
            self.assertNotIn("brands_niches", path.read_text(), str(path))
        for sql in (ROOT / "scripts" / "sql").glob("*.sql"):
            self.assertNotIn("brands_niches", sql.read_text(), sql.name)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class MigrationMovesTheTags(unittest.TestCase):
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

    def test_tags_move_over_and_the_table_is_dropped(self):
        from pipeline import migrations
        with self.engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        migrations.run_migrations(self.engine)
        with self.engine.begin() as conn:   # an old database that still has the table (like production today)
            conn.execute(text("""CREATE TABLE brands_niches (id serial PRIMARY KEY, brand_raw_id int NOT NULL REFERENCES brands_raw(id),
                                 niche text NOT NULL, description text, tags jsonb, UNIQUE (brand_raw_id, niche))"""))
            conn.execute(text("INSERT INTO brands_raw (id, name, niche) VALUES (1, 'one', 'beauty'), (2, 'two', 'fitness'), (3, 'three', 'music'), (4, 'four', NULL)"))
            conn.execute(text("""INSERT INTO brands_niches (brand_raw_id, niche, description, tags) VALUES
                                 (1, 'beauty', 'long text', CAST('["skincare","makeup"]' AS jsonb)),
                                 (2, 'fitness', NULL, CAST('["yoga"]' AS jsonb)),
                                 (2, 'wellness', NULL, CAST('["spa","yoga"]' AS jsonb)),
                                 (3, 'music', NULL, NULL)"""))
        migrations.run_migrations(self.engine)
        with self.engine.connect() as conn:
            self.assertFalse(conn.execute(text("SELECT to_regclass('brands_niches') IS NOT NULL")).scalar())
            rows = {r[0]: r[1:] for r in conn.execute(text("SELECT id, tags, description FROM brands_raw ORDER BY id"))}
        self.assertEqual(rows[1], (["skincare", "makeup"], "long text"))   # one row: tags kept in order, missing description filled
        self.assertEqual(sorted(rows[2][0]), ["spa", "yoga"])                # two rows: the distinct tags of both
        self.assertEqual((rows[3][0], rows[4][0]), (None, None))
        migrations.run_migrations(self.engine)                                # running it again is harmless
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM brands_raw")).scalar(), 4)


if __name__ == "__main__":
    unittest.main()
