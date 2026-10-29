"""
Differential test of the migration extraction, on a LEGACY database.

For each scenario it builds one legacy-shaped database (tests/legacy_migration_support.py), clones it twice with CREATE DATABASE ... TEMPLATE, runs
the ORIGINAL migration (taken verbatim from git, see MAVLIT_MIGRATION_BASELINE_REV) on one clone and pipeline.migrations.run_migrations() on the other,
and compares the two end to end: tables, columns, indexes, constraints, views, grants, sequences and every row. The documented intentional
differences are asserted separately instead of being hidden. It also checks that each legacy path really did something, so two implementations that
both did nothing would not pass.

REQUIRES explicit configuration, and is skipped otherwise (it never falls back to a default database):
    MAVLIT_TEST_PG_URL        superuser URL of a disposable, local cluster; its database name must match ^mavlit_(test|scratch)_[a-z0-9_]+$
    MAVLIT_EXPECT_PG_MAJOR    the PostgreSQL major version you mean to test (e.g. 18); the test refuses to run if the server is a different one
    MAVLIT_MIGRATION_BASELINE_REV   optional, default HEAD: the revision whose api/app.py still holds the original _run_migrations
The cluster may contain no database other than postgres, the templates and mavlit_(test|scratch|difftest)_*. The test creates only databases named
mavlit_difftest_<random>_* and one role named mavlit_difftest_<random>_reader, and drops only those.

Run:  MAVLIT_EXPECT_PG_MAJOR=18 MAVLIT_TEST_PG_URL='postgresql+psycopg2://postgres@127.0.0.1:<port>/mavlit_test_scratch' python -m pytest tests/test_migration_differential.py -q -s
"""

import os
import re
import sys
import unittest
import uuid
from pathlib import Path

TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")
EXPECTED_MAJOR = os.environ.get("MAVLIT_EXPECT_PG_MAJOR")
BASELINE_REV = os.environ.get("MAVLIT_MIGRATION_BASELINE_REV", "HEAD")
ROOT = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"   # the runtime engine can never be reached
os.environ["RUN_MIGRATIONS"] = ""
os.environ.pop("MIGRATION_DATABASE_URL", None)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

SAFE_MAINTENANCE_NAME = re.compile(r"^mavlit_(test|scratch)_[a-z0-9_]+$")
ALLOWED_OTHER_DATABASES = re.compile(r"^(postgres|template0|template1|mavlit_(test|scratch|difftest)_[a-z0-9_]*)$")
CONFIGURED = bool(TEST_URL and EXPECTED_MAJOR)


def _engine(url, database=None, **kwargs):
    url = make_url(url)
    if database:
        url = url.set(database=database)
    return create_engine(url, poolclass=NullPool, **kwargs)


def assert_isolated(maintenance: "Engine", url) -> int:
    """Refuse to continue unless this is a local, test-only, single-purpose cluster of the PostgreSQL major that was asked for."""
    parsed = make_url(url)
    host = parsed.query.get("host") or parsed.host or ""
    if not (host in ("", "localhost", "127.0.0.1") or str(host).startswith("/")):
        raise RuntimeError("MAVLIT_TEST_PG_URL must point at a local cluster; refusing to run")
    if not SAFE_MAINTENANCE_NAME.match(parsed.database or ""):
        raise RuntimeError(f"the database name {parsed.database!r} does not clearly identify a disposable test database (mavlit_test_* / mavlit_scratch_*); refusing to run")
    with maintenance.connect() as conn:
        number = conn.execute(text("SELECT current_setting('server_version_num')::int")).scalar()
        if number // 10000 != int(EXPECTED_MAJOR):
            raise RuntimeError(f"MAVLIT_EXPECT_PG_MAJOR={EXPECTED_MAJOR} but the server is PostgreSQL {number // 10000} ({number}); refusing to run")
        if not conn.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar():
            raise RuntimeError("the test user must be a superuser of the disposable cluster")
        foreign = [r[0] for r in conn.execute(text("SELECT datname FROM pg_database ORDER BY 1")) if not ALLOWED_OTHER_DATABASES.match(r[0])]
        if foreign:
            raise RuntimeError(f"the cluster contains non-test databases {foreign}; refusing to run next to data that is not disposable")
    return number


@unittest.skipUnless(CONFIGURED, "set MAVLIT_TEST_PG_URL (a disposable local cluster) and MAVLIT_EXPECT_PG_MAJOR to run the differential migration tests")
class GuardRails(unittest.TestCase):
    def test_the_target_is_verified_before_anything_is_created(self):
        engine = _engine(TEST_URL)
        self.addCleanup(engine.dispose)
        number = assert_isolated(engine, TEST_URL)
        print(f"\n  differential migration tests run on server_version_num {number} (PostgreSQL {number // 10000})")

    def test_a_wrong_expected_major_is_refused(self):
        engine = _engine(TEST_URL)
        self.addCleanup(engine.dispose)
        with unittest.mock.patch.dict(globals(), {"EXPECTED_MAJOR": "99"}):
            with self.assertRaises(RuntimeError) as ctx:
                assert_isolated(engine, TEST_URL)
        self.assertIn("refusing", str(ctx.exception))

    def test_unsafe_names_and_hosts_are_refused(self):
        engine = _engine(TEST_URL)
        self.addCleanup(engine.dispose)
        for url in ("postgresql+psycopg2://postgres@127.0.0.1/mavlit_enrichment_test", "postgresql+psycopg2://postgres@127.0.0.1/postgres",
                    "postgresql+psycopg2://postgres@db.example.com/mavlit_test_scratch", "postgresql+psycopg2://postgres@127.0.0.1/test"):
            with self.assertRaises(RuntimeError, msg=url):
                assert_isolated(engine, url)


import unittest.mock  # noqa: E402  (used by GuardRails)

if CONFIGURED:
    import legacy_migration_support as support  # noqa: E402
    from pipeline import migrations  # noqa: E402
    from pipeline.db import Base  # noqa: E402
    from pipeline.helpers import prompts as P  # noqa: E402


class _Differential:
    """One scenario: seed -> two clones -> original migration on one, extracted migration on the other -> compare."""
    SCENARIO = ""

    @classmethod
    def setUpClass(cls):
        cls.created: list[str] = []
        cls.maintenance = _engine(TEST_URL, isolation_level="AUTOCOMMIT")
        cls.addClassCleanup(cls.maintenance.dispose)
        cls.major = assert_isolated(cls.maintenance, TEST_URL) // 10000
        cls.run_id = uuid.uuid4().hex[:10]
        cls.reader = f"mavlit_difftest_{cls.run_id}_reader"
        cls.addClassCleanup(cls._cleanup)
        with cls.maintenance.connect() as conn:
            conn.execute(text(f"CREATE ROLE {cls.reader} NOLOGIN"))
        cls.baseline_rev = BASELINE_REV
        try:
            probe, baseline = support.load_baseline_migration(_engine(TEST_URL), cls.baseline_rev)
        except support.BaselineUnavailable as exc:
            raise unittest.SkipTest(str(exc))
        cls.baseline_text = baseline

        seed = cls._create_database("seed")
        seed_engine = _engine(TEST_URL, seed)
        with seed_engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(seed_engine)
        support.build_legacy_database(seed_engine, baseline, cls.SCENARIO, cls.reader)
        with seed_engine.connect() as conn:
            cls.seed_snapshot = support.snapshot(conn)
        seed_engine.dispose()

        cls.head_name, cls.new_name = cls._create_database("head", template=seed), cls._create_database("new", template=seed)
        cls.head_engine, cls.new_engine = _engine(TEST_URL, cls.head_name), _engine(TEST_URL, cls.new_name)
        cls.addClassCleanup(cls.head_engine.dispose)
        cls.addClassCleanup(cls.new_engine.dispose)
        run_head, _ = support.load_baseline_migration(cls.head_engine, cls.baseline_rev)
        run_head()
        migrations.run_migrations(cls.new_engine)
        cls.head, cls.new = cls._snap(cls.head_engine), cls._snap(cls.new_engine)
        # a second start of each: the migrations are meant to be repeatable
        run_head()
        migrations.run_migrations(cls.new_engine)
        cls.head_again, cls.new_again = cls._snap(cls.head_engine), cls._snap(cls.new_engine)

    @classmethod
    def _create_database(cls, role: str, template: str | None = None) -> str:
        name = f"mavlit_difftest_{cls.run_id}_{cls.SCENARIO}_{role}"
        with cls.maintenance.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")))
        cls.created.append(name)
        return name

    @classmethod
    def _cleanup(cls):
        with cls.maintenance.connect() as conn:
            for name in reversed(cls.created):   # only what this class created
                assert name.startswith(f"mavlit_difftest_{cls.run_id}_")
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f"DROP ROLE IF EXISTS {cls.reader}"))

    @classmethod
    def _snap(cls, engine):
        with engine.connect() as conn:
            return support.snapshot(conn)

    # ---- helpers -------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _rows(snapshot, table):
        import json
        return [json.loads(r) for r in snapshot["data"][table]]

    def _comparable(self, snap):
        """Everything except the documented intentional differences, which have their own tests below."""
        import copy
        import json
        snap = copy.deepcopy(snap)
        snap["data"]["brand_match_profile"] = [json.dumps({k: v for k, v in json.loads(r).items() if k != "youtube_last_sponsorship"}, sort_keys=True) for r in snap["data"]["brand_match_profile"]]
        snap["acls"] = [row for row in snap["acls"] if row[0] != "creator_llm_cost"]
        return snap

    # ---- the differential ----------------------------------------------------------------------------------------------------------
    def test_the_legacy_fixture_really_is_legacy(self):
        seed = self.seed_snapshot
        for present in ("creator_profiles_test", "login_credentials", "tiktok_posts", "twitter_posts", "instagram_commenters", "contacts", "brands", "rate_intelligence_logs"):
            self.assertIn(present, seed["tables"])
        self.assertNotIn("creator_niches", seed["tables"])
        types = {(t, c): ty for t, c, ty, *_ in seed["columns"]}
        self.assertEqual(types[("brand_match_profile", "youtube_last_sponsorship")], "text")
        self.assertEqual(types[("brand_match_profile", "embedding")], "vector")
        self.assertTrue(any(r[1] == "instagram_users_username_key" for r in seed["constraints"]))
        self.assertIn("proposal_sent", " ".join(r for r in seed["data"]["pitches"]))
        self.assertNotIn("is_manual", [c for t, c, *_ in seed["columns"] if t == "pitches"])

    def test_schema_matches_the_original_migration(self):
        for section in ("extensions", "tables", "columns", "indexes", "constraints", "views", "sequences"):
            self.assertEqual(self.head[section], self.new[section], section)

    def test_every_row_matches_the_original_migration(self):
        head, new = self._comparable(self.head), self._comparable(self.new)
        for table in head["data"]:
            self.assertEqual(head["data"][table], new["data"][table], f"rows of {table}")
        self.assertEqual(head["acls"], new["acls"], "table and sequence grants")

    def test_a_second_start_matches_too(self):
        self.assertEqual(self._comparable(self.head_again), self._comparable(self.new_again))
        # Repeatable: nothing changes on a second run, except that INSERT ... ON CONFLICT DO NOTHING (the brands_niches backfill) consumes
        # sequence values even when it inserts nothing. The original does exactly the same, so the sequences are compared by how far each moved.
        without_sequences = lambda snap: {k: v for k, v in self._comparable(snap).items() if k != "sequences"}   # noqa: E731
        self.assertEqual(without_sequences(self.new), without_sequences(self.new_again), "the extracted migration is repeatable")
        moved = lambda before, after: {a[0]: a[1] - b[1] for a, b in zip(after["sequences"], before["sequences"])}   # noqa: E731
        self.assertEqual(moved(self.head, self.head_again), moved(self.new, self.new_again), "the same sequences advance by the same amounts")

    # ---- the documented intentional differences ----------------------------------------------------------------------------------------
    def test_youtube_last_sponsorship_is_preserved_instead_of_cleared(self):
        def column(snap):
            return {r["brand_raw_id"]: r["youtube_last_sponsorship"] for r in self._rows(snap, "brand_match_profile")}
        self.assertEqual({t: ty for t, c, ty, *_ in self.new["columns"] if c == "youtube_last_sponsorship"}, {"brand_match_profile": "timestamp with time zone"})
        self.assertEqual(set(column(self.head).values()), {None}, "the original clears every value")
        values = column(self.new)
        from datetime import datetime, timezone
        self.assertEqual(datetime.fromisoformat(values[1]), datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc), "the same instant, whatever zone the session prints it in")
        self.assertIsNotNone(values[2])
        self.assertTrue(values[2].startswith("2026-08-02"))
        self.assertIsNone(values[3], "an empty string becomes NULL")
        self.assertEqual(column(self.new_again), values, "and a second start leaves them alone")

    def test_the_view_keeps_the_grants_it_had(self):
        acl = dict((n, a) for n, _, a in self.head["acls"])["creator_llm_cost"] or ""
        self.assertNotIn(self.reader, acl, "the original loses every grant of the view")
        new_acl = dict((n, a) for n, _, a in self.new["acls"])["creator_llm_cost"]
        self.assertIn(f"{self.reader}=r/", new_acl)
        self.assertEqual(dict((n, d) for n, d in self.head["views"])["creator_llm_cost"], dict((n, d) for n, d in self.new["views"])["creator_llm_cost"], "same definition")

    # ---- each legacy path really ran (so two implementations that did nothing could not pass) ---------------------------------------------
    def test_legacy_objects_were_renamed_and_removed(self):
        tables = self.new["tables"]
        self.assertIn("creator_niches", tables)
        for gone in ("creator_profiles_test", "login_credentials", "tiktok_posts", "twitter_posts", "instagram_commenters", "contacts", "brands", "rate_intelligence_logs"):
            self.assertNotIn(gone, tables)
        names = {r[1] for r in self.new["indexes"]} | {r[1] for r in self.new["constraints"]}
        for gone in ("instagram_users_username_key", "uq_instagram_users_username_default", "uq_creator_profiles_google_id", "uq_creator_profiles_test_instagram_user_id", "ix_creator_profiles_test_username"):
            self.assertNotIn(gone, names)
        for created in ("uq_creator_niches_username", "uq_creator_niches_username_normalized", "ix_creator_niches_username", "uq_brands_raw_wikidata_id", "uq_creator_profiles_email", "uq_brand_contact_person", "ix_llm_usage_creator_profile_id"):
            self.assertIn(created, names)
        dropped = re.findall(r"ALTER TABLE (\w+) DROP COLUMN IF EXISTS (\w+)", self.baseline_text)
        have = {(t, c) for t, c, *_ in self.new["columns"]}
        self.assertEqual([pair for pair in dropped if pair in have], [], "legacy columns")

    def test_creator_niche_duplicates_keep_only_the_newest(self):
        rows = self._rows(self.new, "creator_niches")
        self.assertEqual(sorted(r["id"] for r in rows), [2, 4], "ids 1 and 3 were older duplicates of 'alice'")

    def test_proposal_sent_became_generated(self):
        statuses = {r["id"]: r["status"] for r in self._rows(self.new, "pitches")}
        self.assertEqual(statuses, {1: "generated", 2: "sent", 3: "generated", 4: "generated"})
        default = [d for t, c, ty, nn, d, _ in self.new["columns"] if (t, c) == ("pitches", "status")][0]
        self.assertIn("generated", default)

    def test_embeddings_of_the_wrong_size_were_reset_and_the_column_retyped(self):
        for table in ("brand_match_profile", "creator_profiles"):
            self.assertIn("vector(1024)", [ty for t, c, ty, *_ in self.new["columns"] if (t, c) == (table, "embedding")])
        profiles = {r["id"]: r["embedding"] for r in self._rows(self.new, "creator_profiles")}
        self.assertIsNone(profiles[1], "768 dimensions: reset")
        self.assertIsNotNone(profiles[2], "1024 dimensions: kept")
        self.assertIsNone(profiles[3])
        brands = {r["brand_raw_id"]: r["embedding"] for r in self._rows(self.new, "brand_match_profile")}
        self.assertIsNone(brands[1])
        self.assertIsNotNone(brands[2])

    def test_login_credentials_were_copied_without_overwriting(self):
        hashes = {r["id"]: r["password_hash"] for r in self._rows(self.new, "creator_profiles")}
        self.assertEqual(hashes, {1: "hash-one", 2: "already-set", 3: None})

    def test_new_columns_got_their_defaults_over_existing_rows_and_brands_niches_were_backfilled(self):
        self.assertEqual({r["refferls"] for r in self._rows(self.new, "brands_raw")}, {False})
        self.assertEqual({r["is_manual"] for r in self._rows(self.new, "pitches")}, {False})
        self.assertEqual(sorted((r["brand_raw_id"], r["niche"]) for r in self._rows(self.new, "brands_niches")), [(1, "beauty"), (2, "fitness"), (3, "music")])
        nullable = {(t, c): not nn for t, c, ty, nn, *_ in self.new["columns"]}
        for pair in (("brands_raw", "name"), ("brands_raw", "niche"), ("instagram_posts", "post_id"), ("creator_profiles", "full_name"), ("rate_estimates", "brand_raw_id")):
            self.assertTrue(nullable[pair], pair)

    def test_the_prompt_rules_behave_as_in_the_original(self):
        prompts = {r["name"]: r["content"] for r in self._rows(self.new, "prompts")}
        self.assertEqual(prompts["my_custom_prompt"], "an operator's own prompt: never touched")
        for name, default in ((P.FULL_PROMPT_NAME, P.FULL_DEFAULT_PROMPT), (P.TAGS_PROMPT_NAME, P.TAGS_DEFAULT_PROMPT), (P.LLM_BRAND_RANKING_V3_PROMPT_NAME, P.LLM_BRAND_RANKING_V3_DEFAULT_PROMPT)):
            self.assertEqual(prompts[name], default, "missing defaults are seeded")
        self.assertNotIn("instagram_creator_profile", prompts)
        self.check_scenario_prompts(prompts)

    def check_scenario_prompts(self, prompts):
        raise NotImplementedError


@unittest.skipUnless(CONFIGURED, "needs a disposable cluster and an explicit PostgreSQL major")
class StaleLegacyPrompts(_Differential, unittest.TestCase):
    SCENARIO = "stale"

    def check_scenario_prompts(self, prompts):
        self.assertEqual(prompts[P.PITCH_PROMPT_NAME], P.PITCH_DEFAULT_PROMPT)
        self.assertEqual(prompts[P.BRAND_CHECK_PROMPT_NAME], P.BRAND_CHECK_DEFAULT_PROMPT)
        self.assertEqual(prompts[P.LINKEDIN_COMPANY_MATCH_PROMPT_NAME], P.LINKEDIN_COMPANY_MATCH_DEFAULT_PROMPT)
        self.assertEqual(prompts[P.CONTRACT_ADVICE_PROMPT_NAME], P.CONTRACT_ADVICE_DEFAULT_PROMPT)
        self.assertEqual(prompts[P.RATE_INTEL_PROMPT_NAME], P.RATE_INTEL_DEFAULT_PROMPT)
        self.assertEqual(prompts[P.CREATOR_NICHE_DESCRIPTION_PROMPT_NAME], "old creator description prompt (renamed by the migration)")


@unittest.skipUnless(CONFIGURED, "needs a disposable cluster and an explicit PostgreSQL major")
class EditedCurrentPrompts(_Differential, unittest.TestCase):
    SCENARIO = "edited"

    def check_scenario_prompts(self, prompts):
        self.assertEqual(prompts[P.PITCH_PROMPT_NAME], "edited pitch prompt that already uses {brand_target_audience}")
        self.assertEqual(prompts[P.BRAND_CHECK_PROMPT_NAME], "edited brand check with has_referral_code")
        self.assertEqual(prompts[P.LINKEDIN_COMPANY_MATCH_PROMPT_NAME], "edited linkedin prompt with {experience}")
        self.assertEqual(prompts[P.CONTRACT_ADVICE_PROMPT_NAME], "edited contract prompt with {exclusivity}")
        self.assertEqual(prompts[P.RATE_INTEL_PROMPT_NAME], "edited rate {creator_highest_likes} {views_adjustment} speaking directly to the creator")
        self.assertEqual(prompts[P.CREATOR_NICHE_DESCRIPTION_PROMPT_NAME], "the current creator description prompt")


@unittest.skipUnless(CONFIGURED, "needs a disposable cluster and an explicit PostgreSQL major")
class UnchangedEarlierDefaultPitchPrompt(_Differential, unittest.TestCase):
    SCENARIO = "previous_default"

    def check_scenario_prompts(self, prompts):
        self.assertEqual(prompts[P.PITCH_PROMPT_NAME], P.PITCH_DEFAULT_PROMPT, "an unedited earlier default is replaced even though it has the new placeholder")
        self.assertEqual(prompts[P.CREATOR_NICHE_DESCRIPTION_PROMPT_NAME], "older name of the creator description prompt")
        self.assertNotIn("creator_profile", prompts)


@unittest.skipUnless(CONFIGURED, "needs a disposable cluster")
class UnparseableLegacyTimestamp(unittest.TestCase):
    """The extracted migration refuses a text timestamp it cannot convert instead of clearing it, and leaves the data stage untouched."""

    @classmethod
    def setUpClass(cls):
        cls.created = []
        cls.maintenance = _engine(TEST_URL, isolation_level="AUTOCOMMIT")
        cls.addClassCleanup(cls.maintenance.dispose)
        assert_isolated(cls.maintenance, TEST_URL)
        cls.run_id = uuid.uuid4().hex[:10]
        cls.reader = f"mavlit_difftest_{cls.run_id}_reader"
        with cls.maintenance.connect() as conn:
            conn.execute(text(f"CREATE ROLE {cls.reader} NOLOGIN"))
        cls.addClassCleanup(cls._cleanup)
        try:
            _, baseline = support.load_baseline_migration(_engine(TEST_URL), BASELINE_REV)
        except support.BaselineUnavailable as exc:
            raise unittest.SkipTest(str(exc))
        name = f"mavlit_difftest_{cls.run_id}_unparseable"
        with cls.maintenance.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        cls.created.append(name)
        cls.engine = _engine(TEST_URL, name)
        cls.addClassCleanup(cls.engine.dispose)
        with cls.engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(cls.engine)
        support.build_legacy_database(cls.engine, baseline, "stale", cls.reader)
        with cls.engine.begin() as conn:
            conn.execute(text("UPDATE brand_match_profile SET youtube_last_sponsorship = 'not a timestamp' WHERE brand_raw_id = 2"))

    @classmethod
    def _cleanup(cls):
        with cls.maintenance.connect() as conn:
            for name in cls.created:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f"DROP ROLE IF EXISTS {cls.reader}"))

    def test_the_migration_stops_with_a_clear_error_and_the_data_stage_is_rolled_back(self):
        with self.assertRaises(migrations.MigrationSchemaError) as ctx:
            migrations.run_migrations(self.engine)
        self.assertIn("not a valid timestamp", str(ctx.exception))
        with self.engine.connect() as conn:
            snap = support.snapshot(conn)
        types = {(t, c): ty for t, c, ty, *_ in snap["columns"]}
        self.assertEqual(types[("brand_match_profile", "youtube_last_sponsorship")], "text", "not converted")
        self.assertIn("login_credentials", snap["tables"], "the big transaction (copy and drop of the legacy table) was rolled back as a whole")
        self.assertIn("tiktok_posts", snap["tables"])
        statuses = {json_row for json_row in snap["data"]["pitches"] if "proposal_sent" in json_row}
        self.assertTrue(statuses, "proposal_sent rows were not rewritten")
        values = [r for r in snap["data"]["brand_match_profile"] if "not a timestamp" in r]
        self.assertTrue(values, "the unparseable value is still there, not replaced by NULL")
        self.assertIn("creator_niches", snap["tables"], "the earlier committed stages (rename, create_all, duplicate removal) stay applied; the run is repeatable")


if __name__ == "__main__":
    unittest.main()
