"""
Phase 6, batch 3: the capped match shortlist, the byte-bounded thumbnail cache, the read-only usage report (run against a disposable PostgreSQL)
and the operations document. The report test needs MAVLIT_TEST_PG_URL.
"""

import contextlib
import io
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")
REPORT = ROOT / "scripts" / "sql" / "usage_report.sql"
OPERATIONS = ROOT / "docs" / "OPERATIONS.md"

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

BASE_ENV = {"DATABASE_URL": "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable", "JWT_SECRET": "p" * 40, "ADMIN_SESSION_SECRET": "q" * 40}


def _python(code, **env):
    clean = {k: v for k, v in os.environ.items() if k not in ("MATCH_SHORTLIST_MAX", "THUMBNAIL_CACHE_MAX_BYTES", "THUMBNAIL_CACHE_MAX_ENTRIES", "ENVIRONMENT")}
    clean.update(BASE_ENV)
    clean.update(env)
    result = subprocess.run([sys.executable, "-c", "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n" + code], cwd=ROOT, env=clean,
                            capture_output=True, text=True, timeout=120)
    return result.returncode, result.stdout.strip(), result.stderr


class FakeQuery:
    """The shortlist query: rows are already in distance order, like the database would return them."""

    def __init__(self, rows):
        self.rows, self.limit_value = rows, None

    def count(self):
        return len(self.rows)

    def order_by(self, _):
        return self

    def limit(self, n):
        self.limit_value = n
        return self

    def all(self):
        return self.rows if self.limit_value is None else self.rows[: self.limit_value]


class MatchShortlist(unittest.TestCase):
    def run_matches(self, brand_count, **kwargs):
        from pipeline.matching import matcher
        rows = [(SimpleNamespace(id=n, name=f"Brand {n}", website=None, niche="beauty", latest_product=None), None, n / 1000) for n in range(1, brand_count + 1)]
        query = FakeQuery(rows)
        db = mock.MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(id=1, embedding=[0.1])
        scored = []

        def score(db, creator, brand, reasons):
            scored.append(brand.id)
            return {"total_score": 1 - brand.id / 100, "niche_tier": 1, "dimensions": {}}
        with mock.patch.object(matcher, "_hard_filtered_query", return_value=(query, "distance")), mock.patch.object(matcher, "collect_match_reasons", return_value=[]), \
                mock.patch.object(matcher, "score_match", score), mock.patch.object(matcher, "generate_match_reasons", return_value=[]):
            out = matcher.get_matches(db, 1, include_total=True, **kwargs)
        return out, query, scored

    def test_results_for_a_small_set_are_exactly_what_they_were(self):
        (page, total), query, scored = self.run_matches(8)
        with mock.patch.dict(os.environ, {"MATCH_SHORTLIST_MAX": "100000"}):   # effectively no cap: the old behaviour
            (reference, reference_total), _, _ = self.run_matches(8)
        self.assertEqual((page, total), (reference, reference_total))
        self.assertEqual(total, 8)
        self.assertEqual([r["brand_raw_id"] for r in page], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(sorted(scored), list(range(1, 9)))
        self.assertEqual(query.limit_value, 500, "the default cap is asked of the database")

    def test_paging_a_small_set_is_unchanged(self):
        (page, total), _, _ = self.run_matches(8, limit=3, offset=2)
        self.assertEqual(([r["brand_raw_id"] for r in page], total), ([3, 4, 5], 8))

    def test_a_large_set_is_cut_to_the_nearest_and_scored_only_for_those(self):
        with mock.patch.dict(os.environ, {"MATCH_SHORTLIST_MAX": "5"}):
            (page, total), query, scored = self.run_matches(40)
        self.assertEqual(query.limit_value, 5)
        self.assertEqual(sorted(scored), [1, 2, 3, 4, 5], "the 5 nearest only")
        self.assertEqual(total, 5, "the total a client pages through stops at the shortlist")
        self.assertEqual([r["brand_raw_id"] for r in page], [1, 2, 3, 4, 5])
        with mock.patch.dict(os.environ, {"MATCH_SHORTLIST_MAX": "5"}):
            (past_end, _), _, _ = self.run_matches(40, offset=5)
        self.assertEqual(past_end, [])

    def test_a_set_exactly_at_the_cap_is_complete(self):
        with mock.patch.dict(os.environ, {"MATCH_SHORTLIST_MAX": "12"}):
            (page, total), _, _ = self.run_matches(12, limit=100)
        self.assertEqual((len(page), total), (12, 12))

    def test_no_debug_print_on_every_request(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.run_matches(3)
        self.assertNotIn("length", buffer.getvalue())

    def test_the_setting(self):
        code = "from pipeline.matching.matcher import match_shortlist_max as m; print(m())"
        self.assertEqual(_python(code)[1], "500")
        self.assertEqual(_python(code, MATCH_SHORTLIST_MAX="75")[1], "75")
        for bad in ("many", "0", "-3"):
            result = _python(code, MATCH_SHORTLIST_MAX=bad)
            self.assertNotEqual(result[0], 0, bad)
            self.assertIn("MATCH_SHORTLIST_MAX", result[2])


class ThumbnailCacheBounds(unittest.TestCase):
    def setUp(self):
        import api.creator_posts as cp
        self.cp = cp
        cp._thumb_cache.clear()
        self.addCleanup(cp._thumb_cache.clear)
        self.clock = [1000.0]
        patcher = mock.patch.object(cp.time, "time", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, code, size):
        self.clock[0] += 1
        self.cp._remember_thumbnail(code, b"x" * size, "image/jpeg")

    def total(self):
        return sum(len(entry[1]) for entry in self.cp._thumb_cache.values())

    def test_defaults(self):
        self.assertEqual((self.cp.thumbnail_cache_max_bytes(), self.cp.thumbnail_cache_max_entries()), (64 * 1024 * 1024, 300))

    def test_total_bytes_are_bounded_and_the_oldest_go_first(self):
        with mock.patch.dict(os.environ, {"THUMBNAIL_CACHE_MAX_BYTES": "3000"}):
            for code in "ABC":
                self.add(code, 1000)
            self.assertEqual((sorted(self.cp._thumb_cache), self.total()), (["A", "B", "C"], 3000))
            self.add("D", 1000)   # one more: the oldest (A) makes room
            self.assertEqual(sorted(self.cp._thumb_cache), ["B", "C", "D"])
            self.add("E", 2500)   # needs room for 2500: B, C and D all go
            self.assertEqual(sorted(self.cp._thumb_cache), ["E"])
            self.assertLessEqual(self.total(), 3000)

    def test_entries_are_bounded_too(self):
        with mock.patch.dict(os.environ, {"THUMBNAIL_CACHE_MAX_ENTRIES": "3"}):
            for code in "ABCDE":
                self.add(code, 10)
            self.assertEqual(sorted(self.cp._thumb_cache), ["C", "D", "E"])

    def test_a_picture_larger_than_the_whole_cache_is_not_kept(self):
        with mock.patch.dict(os.environ, {"THUMBNAIL_CACHE_MAX_BYTES": "2048"}):
            self.add("A", 500)
            self.add("HUGE", 5000)
            self.assertEqual(sorted(self.cp._thumb_cache), ["A"], "and it does not push out what is there")

    def test_storing_a_code_again_replaces_it_instead_of_counting_twice(self):
        with mock.patch.dict(os.environ, {"THUMBNAIL_CACHE_MAX_BYTES": "2500"}):
            self.add("A", 1000)
            self.add("B", 1000)
            self.add("A", 1000)   # A again: 2000 bytes in all, nothing evicted
            self.assertEqual((sorted(self.cp._thumb_cache), self.total()), (["A", "B"], 2000))

    def test_the_fetch_path_uses_the_bounded_cache(self):
        import contextlib as ctx
        chunks = [b"x" * 1500]

        @ctx.contextmanager
        def stream(method, url, **kwargs):
            yield SimpleNamespace(status_code=200, headers={"content-type": "image/jpeg"}, iter_bytes=lambda: iter(chunks))
        with mock.patch.dict(os.environ, {"THUMBNAIL_CACHE_MAX_BYTES": "3000"}), mock.patch.object(self.cp.httpx, "stream", stream):
            for code in ("P1", "P2", "P3"):
                self.clock[0] += 1
                self.assertEqual(len(self.cp._fetch_thumbnail(code)[0]), 1500)
        self.assertEqual(sorted(self.cp._thumb_cache), ["P2", "P3"], "3 x 1500 bytes do not fit in 3000: the oldest was dropped")

    def test_bad_settings_stop_startup(self):
        for name, bad in (("THUMBNAIL_CACHE_MAX_BYTES", "big"), ("THUMBNAIL_CACHE_MAX_BYTES", "10"), ("THUMBNAIL_CACHE_MAX_ENTRIES", "0")):
            code, _, err = _python("import api.creator_posts", **{name: bad})
            self.assertNotEqual(code, 0, (name, bad))
            self.assertIn(name, err)


def statements():
    """The report's statements: comments removed, split at the semicolons."""
    body = "\n".join(line for line in REPORT.read_text().splitlines() if not line.strip().startswith("--"))
    return [part.strip() for part in body.split(";") if part.strip()]


class ReportFile(unittest.TestCase):
    def test_it_is_nine_selects_and_nothing_else(self):
        found = statements()
        self.assertEqual(len(found), 9)
        for statement in found:
            self.assertRegex(statement, r"^(SELECT|WITH)\b")
        code = "\n".join(line for line in REPORT.read_text().splitlines() if not line.strip().startswith("--"))
        self.assertIsNone(re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|COPY|VACUUM)\b", code, re.IGNORECASE))

    def test_it_never_selects_an_email(self):
        code = "\n".join(line for line in REPORT.read_text().splitlines() if not line.strip().startswith("--"))
        self.assertNotRegex(code, r"\bemail\b(?!_hash)")
        self.assertNotIn("creator_profiles.email", code)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class ReportAgainstPostgres(unittest.TestCase):
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
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, brands_raw, llm_usage, apify_usage, feature_usage, feature_locks, contact_reveals, login_failures, own_brand_jobs RESTART IDENTITY CASCADE"))

    def run_report(self):
        """Every statement in one READ ONLY transaction (a write would raise). Returns [(column names, rows)] per statement."""
        out = []
        with self.engine.connect() as conn:
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            for statement in statements():
                result = conn.execute(text(statement))
                out.append((list(result.keys()), [tuple(r) for r in result.fetchall()]))
            conn.rollback()
        return out

    def test_every_statement_runs_on_an_empty_database(self):
        report = self.run_report()
        self.assertEqual(len(report), 9)
        self.assertTrue(all(rows == [] for _, rows in report))

    def seed(self):
        with self.engine.begin() as conn:
            for n in range(1, 4):
                conn.execute(text("INSERT INTO creator_profiles (email, password_hash, is_active) VALUES (:e, 'x', true)"), {"e": f"person{n}@example.test"})
            conn.execute(text("INSERT INTO brands_raw (name) VALUES ('Brand A'), ('Brand B')"))
            # 1: LLM cost, user 2 spends the most in the last day, user 3 only long ago
            conn.execute(text("""INSERT INTO llm_usage (model, endpoint, prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens,
                                                         creator_profile_id, feature, cost_usd, created_at) VALUES
                ('m', '/chat/completions', 0, 0, 0, 0, 1, 'pitch', 0.50, now() - interval '2 hours'),
                ('m', '/chat/completions', 0, 0, 0, 0, 2, 'pitch', 3.00, now() - interval '1 hour'),
                ('m', '/chat/completions', 0, 0, 0, 0, 2, 'match_v3', 1.25, now() - interval '3 days'),
                ('m', '/chat/completions', 0, 0, 0, 0, 3, 'pitch', 9.00, now() - interval '30 days'),
                ('m', '/chat/completions', 0, 0, 0, 0, NULL, 'x', 5.00, now())"""))
            # 2: user 1 at 80% of the pitch cap (24 of 30), user 2 below it, user 3 at 100% of contract advice
            conn.execute(text("""INSERT INTO feature_usage (creator_profile_id, feature, used_on, uses) VALUES
                (1, 'pitches', (now() AT TIME ZONE 'utc')::date, 24), (2, 'pitches', (now() AT TIME ZONE 'utc')::date, 23),
                (3, 'contract_advice', (now() AT TIME ZONE 'utc')::date, 10), (1, 'pitches', (now() AT TIME ZONE 'utc')::date - 1, 30)"""))
            # 3: Apify
            conn.execute(text("""INSERT INTO apify_usage (creator_profile_id, feature, platform, handle, posts_requested, posts_scraped, created_at) VALUES
                (1, 'description', 'instagram', 'h', 40, 38, now()), (1, 'pitch', 'instagram', 'h', 3, 3, now()), (2, 'own_brand', 'instagram', 'b', 40, 40, now()),
                (2, 'own_brand', 'instagram', 'b', 40, 40, now() - interval '3 days')"""))
            # 4: contact reveals today
            conn.execute(text("""INSERT INTO contact_reveals (creator_profile_id, brand_raw_id, revealed_on) VALUES
                (1, 1, (now() AT TIME ZONE 'utc')::date), (1, 2, (now() AT TIME ZONE 'utc')::date), (2, 1, (now() AT TIME ZONE 'utc')::date - 1)"""))
            # 5, 6: failed logins: one address tries 6 times against one account and 2 more accounts once
            conn.execute(text("""INSERT INTO login_failures (email_hash, ip, created_at)
                SELECT 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '203.0.113.9', now() - interval '5 minutes' FROM generate_series(1, 6)"""))
            conn.execute(text("""INSERT INTO login_failures (email_hash, ip, created_at) VALUES
                ('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', '203.0.113.9', now()), ('cccccccccccccccccccccccccccccccc', '203.0.113.9', now()),
                ('dddddddddddddddddddddddddddddddd', '198.51.100.7', now() - interval '3 hours')"""))
            # 7: a lock held for 10 minutes, one for 1 minute
            conn.execute(text("""INSERT INTO feature_locks (creator_profile_id, feature, started_at) VALUES
                (1, 'pitch', now() - interval '10 minutes'), (2, 'pitch', now() - interval '1 minute'),
                (3, 'ranking:v3', now() - interval '45 minutes'), (1, 'description:youtube', now() - interval '2 minutes')"""))
            # 8: stuck jobs: user 1 youtube description running with a live lock (fine); user 2 ranking v2 queued with no lock; user 3 ranking v3 running with a 45 min lock
            conn.execute(text("UPDATE creator_profiles SET youtube_description_status = 'running' WHERE id = 1"))
            conn.execute(text("UPDATE creator_profiles SET llm_ranking_status = 'queued' WHERE id = 2"))
            conn.execute(text("UPDATE creator_profiles SET llm_v3_ranking_status = 'running' WHERE id = 3"))
            # 9: own-brand jobs
            conn.execute(text("""INSERT INTO own_brand_jobs (id, creator_profile_id, instagram_handle, status, updated_at) VALUES
                ('old', 1, 'brand', 'running', now() - interval '20 minutes'), ('fresh', 2, 'brand', 'running', now()), ('done', 3, 'brand', 'ready', now() - interval '1 day')"""))

    def test_the_report_finds_what_it_should(self):
        self.seed()
        columns, rows = zip(*self.run_report())

        self.assertEqual(columns[0], ["creator_profile_id", "usd_24h", "usd_7d", "calls_24h", "calls_7d"])
        self.assertEqual([(r[0], float(r[1]), float(r[2])) for r in rows[0]], [(2, 3.0, 4.25), (1, 0.5, 0.5)], "user 3's cost is older than 7 days; no-user rows are left out")

        self.assertEqual({(r[0], r[1], r[2], r[3]) for r in rows[1]}, {(1, "pitches", 24, 30), (3, "contract_advice", 10, 10)}, "23 of 30 is under 80%; yesterday does not count")

        self.assertEqual([(r[0], r[1], r[2]) for r in rows[2]], [(1, 2, 41), (2, 1, 40)], "only the last 24 hours")

        self.assertEqual([tuple(r) for r in rows[3]], [(1, 2)], "yesterday's reveal does not count")

        by_address = {r[0]: r for r in rows[4]}
        self.assertEqual((by_address["203.0.113.9"][1], by_address["203.0.113.9"][2]), (8, 3))
        self.assertNotIn("198.51.100.7", by_address, "three hours ago is outside the last hour")

        self.assertEqual(columns[5][0], "account_hash")
        self.assertEqual([(r[0], r[1], r[2]) for r in rows[5]], [("aaaaaaaaaaaa", 6, 1)], "only the account with 5 or more failures, as 12 characters")

        self.assertEqual({(r[0], r[1]) for r in rows[6]}, {(1, "pitch"), (3, "ranking:v3")}, "older than 3 minutes")

        self.assertEqual({(r[0], r[1], r[2]) for r in rows[7]}, {(2, "ranking:v2", "queued"), (3, "ranking:v3", "running")})
        why = {r[0]: r[4] for r in rows[7]}
        self.assertEqual((why[2], why[3]), ("no lock: the job is not alive", "lock older than 30 minutes"))

        self.assertEqual([(r[0], r[1]) for r in rows[8]], [(1, "old")])

    def test_the_report_prints_no_email_address(self):
        self.seed()
        for names, rows in self.run_report():
            self.assertFalse([n for n in names if "email" in n.lower()], names)
            for row in rows:
                for value in row:
                    self.assertNotIn("@", str(value))
                    self.assertNotIn("example.test", str(value))

    def test_the_report_cannot_write(self):
        self.seed()
        with self.engine.connect() as conn:
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            with self.assertRaises(Exception):
                conn.execute(text("DELETE FROM login_failures"))   # proves the read-only transaction the report test uses really blocks writes
            conn.rollback()
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM login_failures")).scalar(), 9)


class OperationsDocument(unittest.TestCase):
    def setUp(self):
        self.doc = OPERATIONS.read_text()

    def test_it_has_the_requested_settings_and_a_rollback_for_each(self):
        for needle in ("MemoryHigh=2G", "MemoryMax=2.5G", "CPUQuota=250%", "TasksMax=512", "LimitNOFILE=4096", "OOMScoreAdjust=300",
                       "MemoryHigh=384M", "MemoryMax=512M", "CPUQuota=100%", "TasksMax=256", "OOMScoreAdjust=-900",
                       "shared_buffers = 2GB", "effective_cache_size = 4500MB", "work_mem = 16MB",
                       "--memory 768m", "--cpus 1", "--pids-limit 256"):
            self.assertIn(needle, self.doc, needle)
        sections = re.split(r"^### |^## ", self.doc, flags=re.MULTILINE)
        for title in ("mavlit (the API)", "mavlit-frontend (Node)", "PostgreSQL (protected from the OOM killer)", "4. PostgreSQL settings", "5. SearXNG container limits",
                      "6. Swap", "7. Bind addresses"):
            section = next((s for s in sections if s.startswith(title) or s.startswith(title.split(". ", 1)[-1])), None)
            self.assertIsNotNone(section, title)
            self.assertIn("Rollback", section, f"{title} has a rollback line")

    def test_it_documents_the_report_the_alerts_and_the_bind_address(self):
        for needle in ("scripts/sql/usage_report.sql", "default_transaction_read_only=on", "--host 127.0.0.1", "gmaps_scraper", "ss -ltnp",
                       "LLM spend", "A held lock", "A stuck job", "503 rate", "10 or more from one address in 15 minutes"):
            self.assertIn(needle, self.doc, needle)

    def test_it_applies_nothing_by_itself(self):
        self.assertIn("Nothing in this file has been applied", self.doc)


if __name__ == "__main__":
    unittest.main()
