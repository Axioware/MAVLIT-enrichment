"""
REQUIRES A DISPOSABLE POSTGRESQL DATABASE (with the pgvector extension available, and a superuser role). Skipped unless
MAVLIT_TEST_PG_URL is set. These tests DROP AND RECREATE the public schema of that database and run the real migrations
in it, so they refuse to run unless the database is local (unix socket, localhost or 127.0.0.1) and its name contains
"test" or "scratch". They never use DATABASE_URL: the runtime URL is forced to an unreachable address below, which also
proves the migration path does not fall back to the runtime engine.

Example (throwaway cluster):
    initdb -D /tmp/mpgdata -A trust -U postgres
    pg_ctl -D /tmp/mpgdata -o "-c listen_addresses='' -c unix_socket_directories=/tmp/mpg -c port=55432" start
    createdb -h /tmp/mpg -p 55432 -U postgres mavlit_test_scratch
    MAVLIT_TEST_PG_URL='postgresql+psycopg2://postgres@/mavlit_test_scratch?host=/tmp/mpg&port=55432' \
        python -m pytest tests/test_migration_postgres.py -q
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")
# The runtime engine must never be reachable from these tests.
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"
os.environ["RUN_MIGRATIONS"] = ""
os.environ.pop("MIGRATION_DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402


def _assert_disposable(raw: str) -> None:
    url = make_url(raw)
    host = url.query.get("host") or url.host or ""
    local = host in ("", "localhost", "127.0.0.1") or str(host).startswith("/")
    named = any(word in (url.database or "") for word in ("test", "scratch"))
    if not (local and named):
        raise RuntimeError("MAVLIT_TEST_PG_URL must be a LOCAL database whose name contains 'test' or 'scratch'; refusing to run")


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class RealPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _assert_disposable(TEST_URL)
        from pipeline import migrations
        cls.migrations = migrations
        cls.engine = create_engine(TEST_URL, poolclass=NullPool)
        with cls.engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        migrations.run_migrations(cls.engine)   # builds the whole schema in the disposable database

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def _scalar(self, sql, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def _set_youtube(self, brand_id: int, value):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE brand_match_profile SET youtube_last_sponsorship = :v WHERE brand_raw_id = :b"), {"v": value, "b": brand_id})

    def _column_type(self):
        return self._scalar("SELECT data_type FROM information_schema.columns WHERE table_name = 'brand_match_profile' AND column_name = 'youtube_last_sponsorship'")

    def _filenode(self):
        return self._scalar("SELECT relfilenode FROM pg_class WHERE relname = 'brand_match_profile'")

    def _make_brand(self) -> int:
        from pipeline.db import BrandProfile, BrandRaw
        with Session(self.engine) as db:
            brand = BrandRaw(name=f"b{time.time_ns()}")
            db.add(brand)
            db.flush()
            db.add(BrandProfile(brand_raw_id=brand.id))
            db.commit()
            return brand.id

    def test_youtube_last_sponsorship_survives_repeated_migrations(self):
        brand_id = self._make_brand()
        stamp = datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc)
        self._set_youtube(brand_id, stamp)
        filenode = self._filenode()
        self.migrations.run_migrations(self.engine)
        self.migrations.run_migrations(self.engine)
        value = self._scalar("SELECT youtube_last_sponsorship FROM brand_match_profile WHERE brand_raw_id = :b", b=brand_id)
        self.assertEqual(value, stamp)
        self.assertEqual(self._filenode(), filenode, "the table must not be rewritten when the column is already TIMESTAMPTZ")
        self.assertEqual(self._column_type(), "timestamp with time zone")

    def test_an_old_text_column_is_converted_with_its_values(self):
        brand_id = self._make_brand()
        try:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE brand_match_profile ALTER COLUMN youtube_last_sponsorship TYPE TEXT USING youtube_last_sponsorship::text"))
                conn.execute(text("UPDATE brand_match_profile SET youtube_last_sponsorship = '2026-07-01T10:30:00+00:00' WHERE brand_raw_id = :b"), {"b": brand_id})
            self.migrations.run_migrations(self.engine)
            self.migrations.run_migrations(self.engine)
            self.assertEqual(self._column_type(), "timestamp with time zone")
            self.assertEqual(self._scalar("SELECT youtube_last_sponsorship FROM brand_match_profile WHERE brand_raw_id = :b", b=brand_id),
                             datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc))
        finally:
            self.migrations.run_migrations(self.engine)

    def test_an_unparseable_text_value_stops_and_keeps_everything(self):
        brand_id = self._make_brand()
        try:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE brand_match_profile ALTER COLUMN youtube_last_sponsorship TYPE TEXT USING youtube_last_sponsorship::text"))
                conn.execute(text("UPDATE brand_match_profile SET youtube_last_sponsorship = 'not a date' WHERE brand_raw_id = :b"), {"b": brand_id})
            with self.assertRaises(self.migrations.MigrationSchemaError):
                self.migrations.run_migrations(self.engine)
            self.assertEqual(self._column_type(), "text")
            self.assertEqual(self._scalar("SELECT youtube_last_sponsorship FROM brand_match_profile WHERE brand_raw_id = :b", b=brand_id), "not a date")
        finally:
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE brand_match_profile SET youtube_last_sponsorship = NULL"))
            self.migrations.run_migrations(self.engine)

    def test_a_held_lock_makes_the_migration_fail_fast_and_clean(self):
        holder = self.engine.connect()
        try:
            holder.execute(text("LOCK TABLE brands_raw IN ACCESS EXCLUSIVE MODE"))   # held by an open transaction
            holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar()
            started = time.monotonic()
            with self.assertRaises(self.migrations.MigrationLockTimeout):
                self.migrations.run_migrations(self.engine, lock_timeout_ms=300)
            self.assertLess(time.monotonic() - started, 15)
            leftovers = self._scalar(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND state = 'idle in transaction' AND pid <> :holder", holder=holder_pid)
            self.assertEqual(leftovers, 0, "a failed migration must not leave its transaction open")
        finally:
            holder.rollback()
            holder.close()
        self.migrations.run_migrations(self.engine, lock_timeout_ms=5000)   # the retry succeeds once the lock is gone

    def test_the_command_migrates_with_the_env_file_and_never_uses_the_runtime_engine(self):
        from pipeline import migrate
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "migrate.env"
            path.write_text(f"MIGRATION_DATABASE_URL={TEST_URL}\n")
            path.chmod(0o600)
            self.assertEqual(migrate.main(["--env-file", str(path)]), 0)   # DATABASE_URL points at an unreachable address


if __name__ == "__main__":
    unittest.main()
