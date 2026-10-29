"""
pipeline/migrations.py

The database migrations, in one place so they can run on their own instead of at every API start:

    python -m pipeline.migrate            # explicit, e.g. from a deploy step (see pipeline/migrate.py)
    RUN_MIGRATIONS=1 (the default)        # the API still runs them at startup, exactly as before

What they do: create the pgvector extension, rename old tables, Base.metadata.create_all, add / drop / alter
columns and indexes (many IF NOT EXISTS, so safe to repeat), a few one-off data fixes, rebuild the
creator_llm_cost view, and seed or refresh the default prompts in the prompts table.

Which database: whichever engine the caller passes. The standalone command (pipeline/migrate.py) builds it from the
migration-only settings in pipeline/migration_settings.py; the API's startup passes nothing and so uses its own
runtime engine, exactly as before. This module reads no environment variable and opens no connection when imported.

Lock timeout: the standalone command passes lock_timeout_ms, and every migration transaction then starts with
SET LOCAL lock_timeout, so a migration that cannot get a table lock fails fast (MigrationLockTimeout) and rolls back
instead of queueing behind running queries and blocking everyone behind it. None (the API's startup) sets nothing.
"""

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from pipeline import db_grants
from pipeline.db import Base, Prompt, engine as runtime_engine
from pipeline.helpers.prompts import (
    APOLLO_RANK_DEFAULT_PROMPT,
    APOLLO_RANK_PROMPT_NAME,
    BRAND_AUDIENCE_ANALYSIS_DEFAULT_PROMPT,
    BRAND_AUDIENCE_ANALYSIS_PROMPT_NAME,
    BRAND_BEACH_RELATED_DEFAULT_PROMPT,
    BRAND_BEACH_RELATED_PROMPT_NAME,
    BRAND_CHECK_DEFAULT_PROMPT,
    BRAND_CHECK_PROMPT_NAME,
    BRAND_FROM_INSTAGRAM_DEFAULT_PROMPT,
    BRAND_FROM_INSTAGRAM_PROMPT_NAME,
    BRAND_NICHE_TAGS_DEFAULT_PROMPT,
    BRAND_NICHE_TAGS_PROMPT_NAME,
    COAUTHOR_DEFAULT_PROMPT,
    COAUTHOR_PROMPT_NAME,
    CONTRACT_ADVICE_DEFAULT_PROMPT,
    CONTRACT_ADVICE_PROMPT_NAME,
    CREATOR_NICHE_DEFAULT_PROMPT,
    CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT,
    CREATOR_NICHE_DESCRIPTION_PROMPT_NAME,
    CREATOR_NICHE_PROMPT_NAME,
    DEMOGRAPHICS_DEFAULT_PROMPT,
    DEMOGRAPHICS_PROMPT_NAME,
    FULL_DEFAULT_PROMPT,
    FULL_PROMPT_NAME,
    GENDER_DEFAULT_PROMPT,
    GENDER_PROMPT_NAME,
    INSTAGRAM_POST_SPONSORSHIP_DEFAULT_PROMPT,
    INSTAGRAM_POST_SPONSORSHIP_PROMPT_NAME,
    LINKEDIN_COMPANY_MATCH_DEFAULT_PROMPT,
    LINKEDIN_COMPANY_MATCH_PROMPT_NAME,
    LINK_CLASSIFY_DEFAULT_PROMPT,
    LINK_CLASSIFY_PROMPT_NAME,
    LLM_BRAND_RANKING_DEFAULT_PROMPT,
    LLM_BRAND_RANKING_PROMPT_NAME,
    LLM_BRAND_RANKING_V3_DEFAULT_PROMPT,
    LLM_BRAND_RANKING_V3_PROMPT_NAME,
    PITCH_DEFAULT_PROMPT,
    PITCH_PREVIOUS_DEFAULT_PROMPTS,
    PITCH_PROMPT_NAME,
    RATE_INTEL_DEFAULT_PROMPT,
    RATE_INTEL_PROMPT_NAME,
    SPONSOR_CHECK_DEFAULT_PROMPT,
    SPONSOR_CHECK_PROMPT_NAME,
    TAGS_DEFAULT_PROMPT,
    TAGS_PROMPT_NAME,
    WEBSITE_PICK_DEFAULT_PROMPT,
    WEBSITE_PICK_PROMPT_NAME,
)

logger = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """A migration could not be applied. The message never contains a URL or a password."""


class MigrationLockTimeout(MigrationError):
    """A table lock could not be acquired within the lock timeout; the transaction in progress was rolled back."""


class MigrationSchemaError(MigrationError):
    """The existing schema is not what a migration step can safely convert; nothing was changed by that step."""


_LOCK_NOT_AVAILABLE = "55P03"


def run_migrations(engine: Engine | None = None, lock_timeout_ms: int | None = None, grant_role: str | None = None) -> None:
    """
    Create tables and apply column migrations (and the data fixes and prompt seeding that go with them).

    Runs against `engine`, or against the runtime engine (DATABASE_URL) when none is given, which is what the API has
    always done. The caller owns the engine, including disposing a dedicated one. With lock_timeout_ms set, a lock
    that cannot be acquired in time raises MigrationLockTimeout. The migrations are several transactions, not one:
    every step is idempotent, so after a failure the same command can simply be run again.

    With grant_role (the API's runtime database role) the runtime grants of pipeline/db_grants.py are re-applied: right
    after the creator_llm_cost view is recreated (a dropped view loses its grants) and again at the end for every table and
    sequence. Without it nothing is granted, which is what the API's own startup does.
    """
    if grant_role is not None:
        db_grants.validate_identifier(grant_role)
    if lock_timeout_ms is not None and lock_timeout_ms < 0:
        raise MigrationError("lock_timeout_ms must not be negative")
    try:
        _apply_migrations(engine if engine is not None else runtime_engine, lock_timeout_ms or None, grant_role)
    except DBAPIError as exc:
        if getattr(exc.orig, "pgcode", None) == _LOCK_NOT_AVAILABLE:
            raise MigrationLockTimeout(
                f"Could not get a database lock within {lock_timeout_ms} ms; the migration step in progress was rolled back. "
                "Run it again when the API is quieter, or raise the limit (--lock-timeout-ms)."
            ) from exc
        raise


def _set_lock_timeout(conn, lock_timeout_ms: int | None) -> None:
    """First statement of a migration transaction (conn can be a Connection or a Session): SET LOCAL lasts until it ends."""
    if lock_timeout_ms:
        conn.execute(text("SELECT set_config('lock_timeout', :ms, true)"), {"ms": str(int(lock_timeout_ms))})   # true = this transaction only, like SET LOCAL


_TIMESTAMPTZ_CASTS = {   # column type -> expression that converts its values to timestamptz without losing them
    "text": "NULLIF(BTRIM(youtube_last_sponsorship), '')::timestamptz",
    "character varying": "NULLIF(BTRIM(youtube_last_sponsorship), '')::timestamptz",
    "timestamp without time zone": "youtube_last_sponsorship::timestamptz",
    "date": "youtube_last_sponsorship::timestamptz",
}
_INVALID_DATETIME_CODES = ("22007", "22008", "22P02")   # invalid_datetime_format, datetime_field_overflow, invalid_text_representation


def _convert_youtube_last_sponsorship(conn) -> None:
    """
    brand_match_profile.youtube_last_sponsorship is a TIMESTAMPTZ (the publish date of the brand's latest sponsored
    video, written by pipeline/enrichment/brand_signals.py). It was TEXT until 2026-07-13, when nothing wrote it yet, and
    the migration of that day cleared it with `USING NULL::timestamptz`. That statement kept running on every start and
    cleared the column each time, once brand_signals began writing it.

    Now the type is read first: a TIMESTAMPTZ column is left completely alone (no rewrite, no lock beyond the read), an
    older TEXT / VARCHAR / TIMESTAMP / DATE column is converted with its values kept, and a value that cannot be parsed or
    an unexpected type stops the migration with a clear error instead of being replaced by NULL.
    """
    row = conn.execute(text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'brand_match_profile' AND column_name = 'youtube_last_sponsorship'"
    )).first()
    if row is None:   # table or column not there yet: create_all / the ADD COLUMN steps take care of it
        return
    data_type = row[0]
    if data_type == "timestamp with time zone":
        return
    cast = _TIMESTAMPTZ_CASTS.get(data_type)
    if cast is None:
        raise MigrationSchemaError(
            f"brand_match_profile.youtube_last_sponsorship has the unexpected type {data_type!r}; it was not converted to "
            "TIMESTAMPTZ because that could lose values. Convert it manually, then run the migrations again."
        )
    try:
        conn.execute(text(f"ALTER TABLE brand_match_profile ALTER COLUMN youtube_last_sponsorship TYPE TIMESTAMPTZ USING {cast}"))
    except DBAPIError as exc:
        if getattr(exc.orig, "pgcode", None) in _INVALID_DATETIME_CODES:
            raise MigrationSchemaError(
                "brand_match_profile.youtube_last_sponsorship contains a value that is not a valid timestamp, so the column "
                "was not converted to TIMESTAMPTZ. Fix or clear that value manually, then run the migrations again."
            ) from None
        raise


_VIEW_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN"})


class _ViewAcl:
    """
    Carries a view's grants across DROP VIEW / CREATE VIEW (a dropped view loses them). Whatever roles could use the view before
    can use it again, whether or not the runtime-role grant step runs; the grant step then makes sure the runtime role is exact.
    """
    def __init__(self, view: str):
        self.view = view
        self.entries: list[tuple[str, str]] = []

    def save(self, conn) -> None:
        rows = conn.execute(text(
            "SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, a.privilege_type "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a "
            "WHERE c.relname = :view AND n.nspname = current_schema() AND a.grantee <> c.relowner"), {"view": self.view}).all()
        self.entries = [(row[0], row[1]) for row in rows]

    def restore(self, conn) -> None:
        for grantee, privilege in self.entries:
            if privilege not in _VIEW_PRIVILEGES:   # catalog values, but never interpolate an unexpected word into a GRANT
                raise MigrationError(f"unexpected privilege {privilege!r} on view {self.view}")
            target = grantee if grantee == "PUBLIC" else db_grants.quote(grantee)
            conn.execute(text(f"GRANT {privilege} ON TABLE {db_grants.quote(self.view)} TO {target}"))


def _grant_creator_llm_cost(grant_role: str | None):
    """A migration step: give the runtime role SELECT on the freshly recreated view (no-op without a role)."""
    def step(conn) -> None:
        if grant_role:
            for statement in db_grants.view_statements(grant_role):
                conn.execute(text(statement))
    return step


def _apply_migrations(engine: Engine, lock_timeout_ms: int | None = None, grant_role: str | None = None) -> None:
    # pgvector extension must exist before create_all() defines the
    # embedding Vector(1024) column. Only a superuser can create it for the
    # first time on a fresh DB (run once: `CREATE EXTENSION vector;` as
    # postgres) — after that this is a harmless no-op for the app's own user.
    with engine.connect() as conn:
        try:
            _set_lock_timeout(conn, lock_timeout_ms)
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if isinstance(exc, DBAPIError) and getattr(exc.orig, "pgcode", None) == _LOCK_NOT_AVAILABLE:
                raise   # a lock timeout is a real failure, not "no superuser"
            logger.warning(
                "Could not create pgvector extension (needs superuser once) — "
                "continuing; embedding columns will fail if it's still missing."
            )

    with engine.begin() as conn:
        _set_lock_timeout(conn, lock_timeout_ms)
        conn.execute(text("""
            DO $$
            BEGIN
                IF to_regclass('public.creator_profiles_test') IS NOT NULL
                   AND to_regclass('public.creator_niches') IS NULL THEN
                    ALTER TABLE creator_profiles_test RENAME TO creator_niches;
                END IF;

                IF to_regclass('public.ix_creator_profiles_test_username') IS NOT NULL
                   AND to_regclass('public.ix_creator_niches_username') IS NULL THEN
                    ALTER INDEX ix_creator_profiles_test_username RENAME TO ix_creator_niches_username;
                END IF;

                IF to_regclass('public.ix_creator_profiles_test_niche') IS NOT NULL
                   AND to_regclass('public.ix_creator_niches_niche') IS NULL THEN
                    ALTER INDEX ix_creator_profiles_test_niche RENAME TO ix_creator_niches_niche;
                END IF;

                IF to_regclass('public.ix_creator_profiles_test_instagram_user_id') IS NOT NULL
                   AND to_regclass('public.ix_creator_niches_instagram_user_id') IS NULL THEN
                    ALTER INDEX ix_creator_profiles_test_instagram_user_id RENAME TO ix_creator_niches_instagram_user_id;
                END IF;
            END $$;
        """))

    if lock_timeout_ms:
        with engine.begin() as conn:
            _set_lock_timeout(conn, lock_timeout_ms)
            Base.metadata.create_all(bind=conn)
    else:
        Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _set_lock_timeout(conn, lock_timeout_ms)
        duplicate_result = conn.execute(text("""
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY LOWER(TRIM(username))
                        ORDER BY created_at DESC NULLS LAST, id DESC
                    ) AS row_number
                FROM creator_niches
            )
            DELETE FROM creator_niches AS niches
            USING ranked
            WHERE niches.id = ranked.id
              AND ranked.row_number > 1
        """))
        if duplicate_result.rowcount:
            logger.warning(
                "Creator niche migration: removed %d duplicate row(s), keeping the newest result per username",
                duplicate_result.rowcount,
            )

    view_acl = _ViewAcl("creator_llm_cost")
    stmts = [
        # Original columns (idempotent)
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS country TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS headquarters TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS location TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS operating_area TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS website TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS domain TEXT",
        "ALTER TABLE pitches ADD COLUMN IF NOT EXISTS sent_at DATE",
        "ALTER TABLE pitches ADD COLUMN IF NOT EXISTS agreed_rate NUMERIC(12, 2)",
        "ALTER TABLE pitches ADD COLUMN IF NOT EXISTS is_manual BOOLEAN NOT NULL DEFAULT false",
        "UPDATE pitches SET status = 'generated' WHERE status = 'proposal_sent'",
        "ALTER TABLE pitches ALTER COLUMN status SET DEFAULT 'generated'",
        # Entity identity and metadata columns
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS wikidata_id TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS entity_type TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS wikipedia_url TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS source_confidence INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS refferls BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS beach_brands BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS male_pct",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS male_confidence",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS female_pct",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS female_confidence",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS gender_explanation",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS target_audience_gender TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS target_audience_gender_confidence INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS target_audience_min_age INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS target_audience_max_age INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS target_audience_age_confidence INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS product_audience_gender TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS product_audience_gender_confidence INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS product_audience_min_age INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS product_audience_max_age INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS product_audience_age_confidence INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS latest_product TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS latest_product_confidence INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS audience_analysis_explanation TEXT",
        # Full unique index on wikidata_id — PostgreSQL treats NULLs as distinct,
        # so multiple NULL rows are permitted even with a UNIQUE index.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_brands_raw_wikidata_id ON brands_raw(wikidata_id)",
        # name/name_normalized/niche/source relaxed to nullable — the
        # content_creator_re brand_check flow creates bare rows with only
        # instagram_handle set, no name/niche/source available.
        "ALTER TABLE brands_raw ALTER COLUMN name DROP NOT NULL",
        "ALTER TABLE brands_raw ALTER COLUMN name_normalized DROP NOT NULL",
        "ALTER TABLE brands_raw ALTER COLUMN niche DROP NOT NULL",
        "ALTER TABLE brands_raw ALTER COLUMN source DROP NOT NULL",
        # Signal enrichment — social handles
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS instagram_handle TEXT",
        # Partial unique index on instagram_handle, scoped to bare rows only
        # (name IS NULL — only content_creator_re's brand_check flow creates
        # those). A table-wide unique index isn't possible: legacy seeded
        # data already has ~32 groups of brands sharing a duplicate
        # instagram_handle from historical scraping issues.
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_brands_raw_instagram_handle_bare
        ON brands_raw(instagram_handle)
        WHERE name IS NULL
        """,
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS youtube_channel_id TEXT",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS twitter_handle",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS facebook_page TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS facebook_page_id TEXT",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS tiktok_handle",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS linkedin_id TEXT",
        # Signal enrichment — website
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS has_official_website BOOLEAN",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS website_source TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS brand_tier TEXT",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS google_discovered_website",
        # Signal enrichment — detection
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS is_shopify BOOLEAN",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS is_woocommerce BOOLEAN",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS in_tranco_list BOOLEAN",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS tranco_rank INTEGER",
        # Signal enrichment — per-step tracking flags
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS wikidata_enriched BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS shopify_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS google_social_checked",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS tranco_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS meta_ads_fetched BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS youtube_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS instagram_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS tiktok_checked",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS twitter_checked",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS initial_brand_scored BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS instagram_wikidata_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS instagram_profile_checked BOOLEAN NOT NULL DEFAULT false",
        "DROP TABLE IF EXISTS tiktok_posts",
        "DROP TABLE IF EXISTS twitter_posts",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS top_commenters",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS is_comment_profile_scraped",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS confirmed_creators",
        "ALTER TABLE instagram_posts ADD COLUMN IF NOT EXISTS llm_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE instagram_posts ADD COLUMN IF NOT EXISTS is_users_scraped BOOLEAN NOT NULL DEFAULT false",
        # instagram_posts: LLM-estimated 0-100 sponsorship confidence with the
        # creator(s) referenced on the post, scored by
        # pipeline/enrichment/instagram_posts.py.
        "ALTER TABLE instagram_posts ADD COLUMN IF NOT EXISTS sponsorship_confidence INTEGER",
        # instagram_posts: allow a "profile-only" row (brand_raw_id + profile
        # snapshot, no post fields) for brands whose scrape returns zero
        # posts worth keeping — see enrich_instagram_posts's
        # _build_profile_only_row. Partial unique index caps it at one such
        # row per brand (real posts still have their own post_id and aren't
        # affected, since this index only covers post_id IS NULL rows).
        "ALTER TABLE instagram_posts ALTER COLUMN post_id DROP NOT NULL",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_instagram_posts_profile_only
        ON instagram_posts(brand_raw_id) WHERE post_id IS NULL
        """,
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS user_type TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS tier_fit TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS niche TEXT",
        # Per-post row storage for creators (coauthor_producer/tagged_user/
        # mention/contentcreatorRE) — one row per post instead of one row
        # per profile, so username can no longer be globally unique.
        # commenters still get exactly 1 row each (unique-by-username
        # preserved for them via this partial index).
        "ALTER TABLE instagram_users DROP CONSTRAINT IF EXISTS instagram_users_username_key",
        "DROP INDEX IF EXISTS uq_instagram_users_username_default",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_instagram_users_username_commenter
        ON instagram_users(username)
        WHERE user_type = 'commenter'
        """,
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS post_id TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS post_url TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS caption TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS likes_count INTEGER",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS comments_count INTEGER",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS post_timestamp TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS is_content_creator_re BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS post_collaborators TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS collaborators_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS top_comments TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_instagram_users_post_id ON instagram_users(post_id)",
        "ALTER TABLE content_creator_re ADD COLUMN IF NOT EXISTS is_scraped BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE youtube_sponsorships ADD COLUMN IF NOT EXISTS tier_fit TEXT",
        "ALTER TABLE youtube_sponsorships ADD COLUMN IF NOT EXISTS comments JSONB",
        "ALTER TABLE youtube_sponsorships ADD COLUMN IF NOT EXISTS male_pct FLOAT",
        "ALTER TABLE youtube_sponsorships ADD COLUMN IF NOT EXISTS female_pct FLOAT",
        "DROP TABLE IF EXISTS instagram_commenters",
        # creator_profiles absorbed the separate `users` table — auth fields
        # now live directly on creator_profiles (see backfill block below).
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS email TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true",
        "ALTER TABLE creator_profiles ALTER COLUMN full_name DROP NOT NULL",
        "ALTER TABLE creator_profiles ALTER COLUMN creator_handle DROP NOT NULL",
        "ALTER TABLE creator_profiles ALTER COLUMN content_niche DROP NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_creator_profiles_email ON creator_profiles(email)",
        # Google OAuth login retired in favor of login_credentials
        # (email/password, admin-managed via /admin) — drop its columns.
        "DROP INDEX IF EXISTS uq_creator_profiles_google_id",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS google_id",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS avatar_url",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS embedding vector(1024)",
        # (Previously stepped through an OpenAI-sized guess of 1536, then
        # sentence-transformers' 384 — both superseded, see below.)
        # Switched again: sentence-transformers (local, 384 dims) -> Mistral's
        # mistral-embed API (1024 dims, fixed). Existing 384-dim vectors are
        # incompatible with the new dimension and must be cleared before the
        # type change (guarded by vector_dims() so this is a no-op on repeat
        # runs, once already 1024/NULL) — they get recomputed by
        # run_brand_signals() on the next pass.
        "UPDATE brand_match_profile SET embedding = NULL WHERE embedding IS NOT NULL AND vector_dims(embedding) != 1024",
        "ALTER TABLE brand_match_profile ALTER COLUMN embedding TYPE vector(1024)",
        "UPDATE creator_profiles SET embedding = NULL WHERE embedding IS NOT NULL AND vector_dims(embedding) != 1024",
        "ALTER TABLE creator_profiles ALTER COLUMN embedding TYPE vector(1024)",
        # enrich.py / contacts.py / verify.py pipeline retired — each enrichment
        # step now runs independently and is scored directly from brands_raw.
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS enriched",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS enrichment_failed",
        # contacts must drop before brands (contacts.brand_id -> brands.id)
        "DROP TABLE IF EXISTS contacts",
        "DROP TABLE IF EXISTS brands",
        # Creator tier fit — min/max follower/subscriber range per brand
        "ALTER TABLE brand_match_profile ADD COLUMN IF NOT EXISTS youtube_highest INTEGER",
        "ALTER TABLE brand_match_profile ADD COLUMN IF NOT EXISTS youtube_lowest INTEGER",
        "ALTER TABLE brand_match_profile ADD COLUMN IF NOT EXISTS insta_highest INTEGER",
        "ALTER TABLE brand_match_profile ADD COLUMN IF NOT EXISTS insta_lowest INTEGER",
        # youtube_last_sponsorship was TEXT; it is converted to TIMESTAMPTZ only if it still is not one, and the
        # values are kept (see _convert_youtube_last_sponsorship) - the old `USING NULL::timestamptz` cleared it on every run.
        _convert_youtube_last_sponsorship,
        # youtube_sponsorship_count / instagram_paid_posts_count removed —
        # derived on demand from youtube_sponsorships / instagram_posts instead
        # of duplicating a count into brand_match_profile.
        "ALTER TABLE brand_match_profile DROP COLUMN IF EXISTS youtube_sponsorship_count",
        "ALTER TABLE brand_match_profile DROP COLUMN IF EXISTS instagram_paid_posts_count",
        "ALTER TABLE brand_match_profile DROP COLUMN IF EXISTS audience_sample_size",
        # Platform presence
        "ALTER TABLE brand_match_profile ADD COLUMN IF NOT EXISTS has_instagram BOOLEAN",
        "ALTER TABLE brand_match_profile ADD COLUMN IF NOT EXISTS has_youtube BOOLEAN",
        "ALTER TABLE brand_match_profile ADD COLUMN IF NOT EXISTS has_facebook BOOLEAN",
        "ALTER TABLE brand_match_profile DROP COLUMN IF EXISTS has_tiktok",
        "ALTER TABLE brand_match_profile DROP COLUMN IF EXISTS has_twitter",
        "ALTER TABLE brand_match_profile DROP COLUMN IF EXISTS tiktok_sponsored_count",
        "ALTER TABLE brand_match_profile DROP COLUMN IF EXISTS twitter_sponsored_count",
        # brand_contacts: switched from keyword title_score to Mistral-ranked llm_reason,
        # and from a single `department` string to Apollo's real departments/subdepartments/functions
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS departments TEXT",
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS subdepartments TEXT",
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS functions TEXT",
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS llm_reason TEXT",
        "ALTER TABLE brand_contacts DROP COLUMN IF EXISTS department",
        "ALTER TABLE brand_contacts DROP COLUMN IF EXISTS title_score",
        # brand_contacts: now stores up to 5 ranked contacts per brand instead
        # of just 1, so brand_raw_id can no longer be unique on its own.
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS rank INTEGER",
        "DROP INDEX IF EXISTS ix_brand_contacts_brand_raw_id",
        "CREATE INDEX IF NOT EXISTS ix_brand_contacts_brand_raw_id ON brand_contacts(brand_raw_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_brand_contact_person ON brand_contacts(brand_raw_id, apollo_person_id)",
        # brand_contacts: now stores up to 50 ranked candidates per brand,
        # with confidence-qualified candidates enriched with real contact info
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS is_enriched BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS sponsorship_contact_confidence INTEGER",
        # creator_profiles: Stage 2 creator-profile-setup fields (matching
        # algorithm design doc) — sub-niches, free-text description for LLM
        # tag extraction, a hard-filter exclusion list, an explicit follower
        # count to drive tier bucketing, an age-range pair (in addition to
        # the older single audience_age_bracket), and embedding_text to
        # mirror brand_match_profile's embedding/embedding_text pairing.
        # content_description replaced by instagram_description / youtube_description
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS content_description",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS excluded_categories JSONB",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS brand_preferences TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS pitch_style_preferences TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS follower_count INTEGER",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS age INTEGER",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS gender TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_primary_niche TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_sub_niches JSONB",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_excluded_categories JSONB",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_description TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_description_mode TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_description_status TEXT NOT NULL DEFAULT 'idle'",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_description_job_id TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_description_error TEXT",
        # replaced by the single instagram_audience_gender / youtube_audience_gender dropdown
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS instagram_audience_gender_male_pct",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS instagram_audience_gender_female_pct",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_audience_gender TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_audience_gender TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_audience_description TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_audience_description TEXT",
        # Rate intelligence now rests on the rate data (pipeline/rate_intelligence.py): a typical rate, a confidence, the
        # details it was built from, and brands that are not in brands_raw.
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS rate_typical INTEGER",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS confidence TEXT",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS details JSONB",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS brand_name TEXT",
        # Contract Advice compares the contract with the Deal Tools terms.
        "ALTER TABLE contract_reviews ADD COLUMN IF NOT EXISTS brand_name TEXT",
        "ALTER TABLE contract_reviews ADD COLUMN IF NOT EXISTS deal_context JSONB",
        "ALTER TABLE contract_reviews ADD COLUMN IF NOT EXISTS deal_comparison JSONB",
        # The request log lives in rate_estimates itself (no separate table).
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS creator_handle TEXT",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS model TEXT",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS prompt TEXT",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS output TEXT",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS input_tokens INTEGER",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS cached_tokens INTEGER",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS output_tokens INTEGER",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12, 6)",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS duration_ms INTEGER",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ok'",
        "ALTER TABLE rate_estimates ADD COLUMN IF NOT EXISTS error TEXT",
        "DROP TABLE IF EXISTS rate_intelligence_logs",   # an earlier version kept the log in its own table
        "ALTER TABLE rate_estimates ALTER COLUMN brand_raw_id DROP NOT NULL",
        # The audience age range is no longer its own field: the age is written in the audience description
        # (existing profiles had their age copied into it before these columns were dropped).
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS instagram_audience_age_min",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS instagram_audience_age_max",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS youtube_audience_age_min",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS youtube_audience_age_max",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_channel_name TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_subscribers INTEGER",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_primary_niche TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_sub_niches JSONB",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_excluded_categories JSONB",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_description TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_description_mode TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_description_status TEXT NOT NULL DEFAULT 'idle'",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_description_job_id TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_description_error TEXT",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS youtube_audience_gender_male_pct",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS youtube_audience_gender_female_pct",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS location_city",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS location_country",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS bio_tagline",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS sub_niches",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS youtube_channel",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS youtube_followers",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS instagram_following",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS facebook_page",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS facebook_followers",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS facebook_following",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS substack_url",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS substack_subscribers",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS audience_gender_male_pct",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS audience_gender_female_pct",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS audience_age_bracket",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS audience_age_min",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS audience_age_max",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS audience_top_countries",
        # Legacy columns that were dropped above (no longer in the model): never ADD them again here. Re-adding a column that
        # is dropped on every startup uses up one of PostgreSQL's 1600 column slots per restart - a dropped column's slot is
        # never given back - and the API would stop starting once creator_profiles reaches the limit.
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS embedding_text TEXT",
        # Matches v2 — LLM ranking status (pipeline/matching/llm_ranking_v2.py)
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS llm_ranking_status TEXT NOT NULL DEFAULT 'idle'",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS llm_ranking_error TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS llm_ranked_at TIMESTAMPTZ",
        "ALTER TABLE creator_brand_llm_rankings ADD COLUMN IF NOT EXISTS reasons JSONB",
        # LLM cost per MAVLIT user (llm_usage.creator_profile_id / feature) + the per-user totals view
        "ALTER TABLE IF EXISTS llm_usage ADD COLUMN IF NOT EXISTS creator_profile_id INTEGER",
        "ALTER TABLE IF EXISTS llm_usage ADD COLUMN IF NOT EXISTS feature TEXT",
        "CREATE INDEX IF NOT EXISTS ix_llm_usage_creator_profile_id ON llm_usage (creator_profile_id)",
        view_acl.save,
        "DROP VIEW IF EXISTS creator_llm_cost",
        """
        CREATE VIEW creator_llm_cost AS
        WITH llm AS (
            SELECT creator_profile_id,
                   COUNT(*)::int                                                          AS calls,
                   COALESCE(SUM(cost_usd), 0)                                             AS total_cost_usd,
                   COALESCE(SUM(cost_usd) FILTER (WHERE feature = 'description'), 0)      AS description_usd,
                   COALESCE(SUM(cost_usd) FILTER (WHERE feature = 'profile_signals'), 0)  AS profile_signals_usd,
                   COALESCE(SUM(cost_usd) FILTER (WHERE feature = 'match_v2'), 0)         AS match_v2_usd,
                   COALESCE(SUM(cost_usd) FILTER (WHERE feature = 'match_v3'), 0)         AS match_v3_usd,
                   COALESCE(SUM(cost_usd) FILTER (
                       WHERE feature IS NULL OR feature NOT IN
                             ('description', 'profile_signals', 'match_v2', 'match_v3')), 0) AS other_usd,
                   MAX(created_at)                                                        AS last_call_at
            FROM llm_usage
            WHERE creator_profile_id IS NOT NULL
            GROUP BY creator_profile_id
        ),
        scrapes AS (
            SELECT creator_profile_id,
                   COUNT(*)::int                        AS instagram_scrapes,
                   COALESCE(SUM(posts_scraped), 0)::int AS instagram_posts_scraped,
                   MAX(created_at)                      AS last_scrape_at
            FROM apify_usage
            WHERE platform = 'instagram' AND creator_profile_id IS NOT NULL
            GROUP BY creator_profile_id
        )
        SELECT COALESCE(l.creator_profile_id, s.creator_profile_id)  AS creator_profile_id,
               cp.email,
               cp.full_name,
               COALESCE(l.calls, 0)                 AS calls,
               COALESCE(l.total_cost_usd, 0)        AS total_cost_usd,
               COALESCE(l.description_usd, 0)       AS description_usd,
               COALESCE(l.profile_signals_usd, 0)   AS profile_signals_usd,
               COALESCE(l.match_v2_usd, 0)          AS match_v2_usd,
               COALESCE(l.match_v3_usd, 0)          AS match_v3_usd,
               COALESCE(l.other_usd, 0)             AS other_usd,
               GREATEST(l.last_call_at, s.last_scrape_at) AS last_call_at,
               COALESCE(s.instagram_scrapes, 0)       AS instagram_scrapes,
               COALESCE(s.instagram_posts_scraped, 0) AS instagram_posts_scraped
        FROM llm l
        FULL OUTER JOIN scrapes s ON s.creator_profile_id = l.creator_profile_id
        LEFT JOIN creator_profiles cp ON cp.id = COALESCE(l.creator_profile_id, s.creator_profile_id)
        """,
        view_acl.restore,                      # the grants the view had before the DROP, in the same transaction (no gap)
        _grant_creator_llm_cost(grant_role),   # and, when asked, exactly the runtime role's planned access
        # Matches v3 — LLM ranking with taglines (pipeline/matching/llm_ranking_v3.py)
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS llm_v3_ranking_status TEXT NOT NULL DEFAULT 'idle'",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS llm_v3_ranking_error TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS llm_v3_ranked_at TIMESTAMPTZ",
        # brands_niches: many-to-many mirror of brands_raw.niche (see BrandNiche
        # docstring). New brands get a row automatically via insert_brand()/
        # insert_brands_batch(); this backfills every brand that already
        # existed before this table did. Idempotent — ON CONFLICT DO NOTHING
        # means this is a no-op on every startup after the first.
        """
        INSERT INTO brands_niches (brand_raw_id, niche)
        SELECT id, niche FROM brands_raw
        WHERE niche IS NOT NULL
        ON CONFLICT (brand_raw_id, niche) DO NOTHING
        """,
        # brands_niches: description mirrored from shopify_detect.py's scraped
        # brands_raw.description, plus Mistral-extracted sub-niche tags.
        "ALTER TABLE brands_niches ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE brands_niches ADD COLUMN IF NOT EXISTS tags JSONB",
        # creator_profiles: per-platform follower/following counts as plain
        # integers, replacing the old JSONB *_stats columns (never had
        # real data — nothing to migrate).
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS instagram_stats",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS tiktok_stats",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS youtube_stats",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS facebook_stats",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_followers INTEGER",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_highest_views INTEGER",
        "ALTER TABLE IF EXISTS creator_instagram_posts ADD COLUMN IF NOT EXISTS partnership_post BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS tiktok_handle",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS tiktok_followers",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS tiktok_following",
        # login_credentials (a separate login table) retired in favor of a
        # password_hash column straight on creator_profiles — see the
        # backfill block below for migrating any existing rows across
        # before the old table is dropped. Nullable at the DB level so
        # sqladmin's auto-generated form doesn't force a value on every
        # edit (see CreatorProfileAdmin's on_model_change in this file).
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS password_hash TEXT",
                """
                UPDATE prompts
                SET name = 'creator_niche_description'
                WHERE name IN ('creator_profile', 'instagram_creator_profile')
                    AND NOT EXISTS (
                            SELECT 1 FROM prompts WHERE name = 'creator_niche_description'
                    )
                """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_test_creator_brand_post
        ON test_creator_brand_partnership_posts(creator_username, brand_raw_id, post_url)
        """,
        # test_creator_brand_partnership_posts: post content/collaboration
        # signals, backfilled for pre-existing rows by
        # pipeline/enrichment_re/backfill_partnership_post_content.py.
        "ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS caption TEXT",
        "ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS paid_partnership BOOLEAN",
        "ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS mentions JSONB",
        "ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS tagged_users JSONB",
        "ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS coauthor_producers JSONB",
        # test_creator_brand_partnership_posts: LLM-estimated 0-100 sponsorship
        # confidence, backfilled by pipeline/enrichment_re/score_post_sponsorship.py.
        "ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS sponsorship_confidence INTEGER",
        # Geographic reach scoring — pipeline/enrichment/geo_reach/geo_reach.py
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS geo_reach_score INTEGER",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS geo_reach_label TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS geo_reach_locations JSONB",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS geo_reach_country_codes JSONB",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS geo_reach_pages_scraped JSONB",
        "ALTER TABLE brands_raw DROP COLUMN IF EXISTS geo_reach_checked",
        # LinkedIn employment verification — pipeline/enrichment/linkedin_verify.py
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS linkedin_verified_at TIMESTAMPTZ",
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS still_at_brand BOOLEAN",
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS still_at_brand_confidence INTEGER",
        # linkedin_current_company (raw employer name from LinkedIn) dropped —
        # still_at_brand's LLM verdict is kept, the raw string isn't.
        "ALTER TABLE brand_contacts DROP COLUMN IF EXISTS linkedin_current_company",
        # Email deliverability verification — pipeline/enrichment/hunter_verify.py
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS hunter_verified_at TIMESTAMPTZ",
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS hunter_status TEXT",
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS hunter_score INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_creator_niches_username ON creator_niches(username)",
        "CREATE INDEX IF NOT EXISTS ix_creator_niches_niche ON creator_niches(niche)",
        "DROP INDEX IF EXISTS ix_creator_niches_instagram_user_id",
        "DROP INDEX IF EXISTS uq_creator_profiles_test_instagram_user_id",
        "CREATE INDEX IF NOT EXISTS ix_creator_niches_instagram_user_id ON creator_niches(instagram_user_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_creator_niches_username ON creator_niches(username)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_creator_niches_username_normalized ON creator_niches(LOWER(TRIM(username)))",
    ]
    with engine.connect() as conn:
        _set_lock_timeout(conn, lock_timeout_ms)
        for sql in stmts:
            if callable(sql):   # a step that needs Python logic (see _convert_youtube_last_sponsorship)
                sql(conn)
            else:
                conn.execute(text(sql))
        conn.commit()
        _set_lock_timeout(conn, lock_timeout_ms)   # the commit ended the transaction that carried the first SET LOCAL

        # One-time data migration: carry any existing login_credentials
        # rows' password_hash across to the matching creator_profiles row
        # (by email) before dropping the old table. Only runs if
        # login_credentials still exists (no-op on fresh installs or after
        # the first successful run).
        login_credentials_exists = conn.execute(
            text("SELECT to_regclass('public.login_credentials') IS NOT NULL")
        ).scalar()
        if login_credentials_exists:
            conn.execute(text("""
                UPDATE creator_profiles cp
                SET password_hash = lc.password_hash
                FROM login_credentials lc
                WHERE cp.email = lc.email AND cp.password_hash IS NULL
            """))
            conn.execute(text("DROP TABLE IF EXISTS login_credentials"))
            conn.commit()

    # Seed default prompts (on conflict = already exists, keep existing content)
    with sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)() as db:
        _set_lock_timeout(db, lock_timeout_ms)
        db.query(Prompt).filter(Prompt.name == "instagram_creator_profile").delete(synchronize_session=False)
        for name, content in [
            (FULL_PROMPT_NAME,          FULL_DEFAULT_PROMPT),
            (COAUTHOR_PROMPT_NAME,      COAUTHOR_DEFAULT_PROMPT),
            (INSTAGRAM_POST_SPONSORSHIP_PROMPT_NAME, INSTAGRAM_POST_SPONSORSHIP_DEFAULT_PROMPT),
            (DEMOGRAPHICS_PROMPT_NAME,  DEMOGRAPHICS_DEFAULT_PROMPT),
            (CREATOR_NICHE_PROMPT_NAME, CREATOR_NICHE_DEFAULT_PROMPT),
            (TAGS_PROMPT_NAME,          TAGS_DEFAULT_PROMPT),
            (CREATOR_NICHE_DESCRIPTION_PROMPT_NAME, CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT),
            (GENDER_PROMPT_NAME,        GENDER_DEFAULT_PROMPT),
            (BRAND_AUDIENCE_ANALYSIS_PROMPT_NAME, BRAND_AUDIENCE_ANALYSIS_DEFAULT_PROMPT),
            (SPONSOR_CHECK_PROMPT_NAME, SPONSOR_CHECK_DEFAULT_PROMPT),
            (APOLLO_RANK_PROMPT_NAME,   APOLLO_RANK_DEFAULT_PROMPT),
            (BRAND_NICHE_TAGS_PROMPT_NAME, BRAND_NICHE_TAGS_DEFAULT_PROMPT),
            (BRAND_BEACH_RELATED_PROMPT_NAME, BRAND_BEACH_RELATED_DEFAULT_PROMPT),
            (BRAND_FROM_INSTAGRAM_PROMPT_NAME, BRAND_FROM_INSTAGRAM_DEFAULT_PROMPT),
            (BRAND_CHECK_PROMPT_NAME,   BRAND_CHECK_DEFAULT_PROMPT),
            (PITCH_PROMPT_NAME,         PITCH_DEFAULT_PROMPT),
            (RATE_INTEL_PROMPT_NAME,    RATE_INTEL_DEFAULT_PROMPT),
            (CONTRACT_ADVICE_PROMPT_NAME, CONTRACT_ADVICE_DEFAULT_PROMPT),
            (LINK_CLASSIFY_PROMPT_NAME, LINK_CLASSIFY_DEFAULT_PROMPT),
            (WEBSITE_PICK_PROMPT_NAME,  WEBSITE_PICK_DEFAULT_PROMPT),
            (LLM_BRAND_RANKING_PROMPT_NAME, LLM_BRAND_RANKING_DEFAULT_PROMPT),
            (LLM_BRAND_RANKING_V3_PROMPT_NAME, LLM_BRAND_RANKING_V3_DEFAULT_PROMPT),
        ]:
            if not db.query(Prompt).filter(Prompt.name == name).first():
                db.add(Prompt(name=name, content=content))
        # The pitch prompt was rewritten (brand audience, partner creators, creator profile
        # inputs): replace a stored copy that still has the old inputs. An edited copy that
        # already uses the new inputs is left alone.
        pitch_prompt = db.query(Prompt).filter(Prompt.name == PITCH_PROMPT_NAME).first()
        if pitch_prompt and (
            "{brand_target_audience}" not in (pitch_prompt.content or "")
            or pitch_prompt.content.strip() in {old.strip() for old in PITCH_PREVIOUS_DEFAULT_PROMPTS}   # unedited earlier version
        ):
            pitch_prompt.content = PITCH_DEFAULT_PROMPT
        brand_check_prompt = db.query(Prompt).filter(Prompt.name == BRAND_CHECK_PROMPT_NAME).first()
        if brand_check_prompt and "has_referral_code" not in (brand_check_prompt.content or ""):
            brand_check_prompt.content = BRAND_CHECK_DEFAULT_PROMPT
        linkedin_prompt = db.query(Prompt).filter(Prompt.name == LINKEDIN_COMPANY_MATCH_PROMPT_NAME).first()
        if linkedin_prompt and "{experience}" not in (linkedin_prompt.content or ""):   # an older prompt without the work-experience input
            linkedin_prompt.content = LINKEDIN_COMPANY_MATCH_DEFAULT_PROMPT
        contract_prompt = db.query(Prompt).filter(Prompt.name == CONTRACT_ADVICE_PROMPT_NAME).first()
        if contract_prompt and "{exclusivity}" not in (contract_prompt.content or ""):   # the earlier prompt had no deal-terms comparison
            contract_prompt.content = CONTRACT_ADVICE_DEFAULT_PROMPT
        rate_prompt = db.query(Prompt).filter(Prompt.name == RATE_INTEL_PROMPT_NAME).first()
        if rate_prompt and not all(k in (rate_prompt.content or "") for k in ("{creator_highest_likes}", "{views_adjustment}", "speaking directly to the creator")):   # an earlier prompt without the rate-data / best-post inputs
            rate_prompt.content = RATE_INTEL_DEFAULT_PROMPT
        db.commit()

    if grant_role:   # opt-in (pipeline.migrate --grant-role): the API's runtime role gets exactly the planned privileges
        with engine.begin() as conn:
            _set_lock_timeout(conn, lock_timeout_ms)
            unclassified = db_grants.apply_grants(conn, grant_role)
        if unclassified:
            logger.warning("Tables without a runtime-grant classification in pipeline/db_grants.py (the API cannot use them): %s", ", ".join(unclassified))
