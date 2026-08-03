import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqladmin import Admin, ModelView
from sqlalchemy import text
from config import FRONTEND_ORIGINS, IS_PRODUCTION, POSTHOG_PROJECT_TOKEN, POSTHOG_HOST
from pipeline.db import Base, BrandContact, BrandInstagramUser, BrandNiche, BrandProfile, BrandRaw, ContentCreatorRE, CreatorProfile, InitialBrandScore, InstagramCreatorCommenter, InstagramPost, InstagramUser, MetaAd, Prompt, YoutubeSponsorship, SessionLocal, engine
from api.auth import get_current_user, router as auth_router
from pipeline.matching.matcher import get_matches
from pipeline.enrichment.creator_signals import compute_creator_signals
from pipeline.helpers.prompts import (
    FULL_PROMPT_NAME, FULL_DEFAULT_PROMPT,
    COAUTHOR_PROMPT_NAME, COAUTHOR_DEFAULT_PROMPT,
    DEMOGRAPHICS_PROMPT_NAME, DEMOGRAPHICS_DEFAULT_PROMPT,
    CREATOR_NICHE_PROMPT_NAME, CREATOR_NICHE_DEFAULT_PROMPT,
    GENDER_PROMPT_NAME, GENDER_DEFAULT_PROMPT,
    SPONSOR_CHECK_PROMPT_NAME, SPONSOR_CHECK_DEFAULT_PROMPT,
    APOLLO_RANK_PROMPT_NAME, APOLLO_RANK_DEFAULT_PROMPT,
    TAGS_PROMPT_NAME, TAGS_DEFAULT_PROMPT,
    BRAND_NICHE_TAGS_PROMPT_NAME, BRAND_NICHE_TAGS_DEFAULT_PROMPT,
    BRAND_CHECK_PROMPT_NAME, BRAND_CHECK_DEFAULT_PROMPT,
)
from pipeline.helpers.creator_tier import bucket_creator_tier
from pipeline.enrichment.orchestrator import run_signal_enrichment
from pipeline.enrichment.initial_brand_scoring import run_brand_scoring
from pipeline.seed import run_seed

logger = logging.getLogger(__name__)

def _run_migrations() -> None:
    """Create tables and apply column migrations before accepting requests."""
    # pgvector extension must exist before create_all() defines the
    # embedding Vector(1024) column. Only a superuser can create it for the
    # first time on a fresh DB (run once: `CREATE EXTENSION vector;` as
    # postgres) — after that this is a harmless no-op for the app's own user.
    with engine.connect() as conn:
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        except Exception:
            conn.rollback()
            logger.warning(
                "Could not create pgvector extension (needs superuser once) — "
                "continuing; embedding columns will fail if it's still missing."
            )

    Base.metadata.create_all(bind=engine)
    stmts = [
        # Original columns (idempotent)
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS country TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS headquarters TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS location TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS operating_area TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS website TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS domain TEXT",
        # Entity identity and metadata columns
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS wikidata_id TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS entity_type TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS wikipedia_url TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS source_confidence INTEGER",
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
        "DROP TABLE IF EXISTS tiktok_posts",
        "DROP TABLE IF EXISTS twitter_posts",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS top_commenters",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS is_comment_profile_scraped",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS confirmed_creators",
        "ALTER TABLE instagram_posts ADD COLUMN IF NOT EXISTS llm_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE instagram_posts ADD COLUMN IF NOT EXISTS is_users_scraped BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS user_type TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS tier_fit TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS captions JSONB",
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
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS google_id TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS avatar_url TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true",
        "ALTER TABLE creator_profiles ALTER COLUMN full_name DROP NOT NULL",
        "ALTER TABLE creator_profiles ALTER COLUMN creator_handle DROP NOT NULL",
        "ALTER TABLE creator_profiles ALTER COLUMN content_niche DROP NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_creator_profiles_email ON creator_profiles(email)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_creator_profiles_google_id ON creator_profiles(google_id)",
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
        # youtube_last_sponsorship was TEXT but never actually written by any
        # code yet (compute_sponsorship_activity doesn't exist) — safe to
        # retype with no data-loss risk. USING NULL sidesteps needing a real
        # text->timestamp cast in case a stray non-null value ever existed.
        "ALTER TABLE brand_match_profile ALTER COLUMN youtube_last_sponsorship TYPE TIMESTAMPTZ USING NULL::timestamptz",
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
        # only the top 5 of which are enriched with real contact info
        "ALTER TABLE brand_contacts ADD COLUMN IF NOT EXISTS is_enriched BOOLEAN NOT NULL DEFAULT FALSE",
        # creator_profiles: Stage 2 creator-profile-setup fields (matching
        # algorithm design doc) — sub-niches, free-text description for LLM
        # tag extraction, a hard-filter exclusion list, an explicit follower
        # count to drive tier bucketing, an age-range pair (in addition to
        # the older single audience_age_bracket), and embedding_text to
        # mirror brand_match_profile's embedding/embedding_text pairing.
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS sub_niches JSONB",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS content_description TEXT",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS excluded_categories JSONB",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS follower_count INTEGER",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS audience_age_min INTEGER",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS audience_age_max INTEGER",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS embedding_text TEXT",
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
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS instagram_following INTEGER",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS tiktok_handle",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS tiktok_followers",
        "ALTER TABLE creator_profiles DROP COLUMN IF EXISTS tiktok_following",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS youtube_followers INTEGER",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS facebook_followers INTEGER",
        "ALTER TABLE creator_profiles ADD COLUMN IF NOT EXISTS facebook_following INTEGER",
    ]
    with engine.connect() as conn:
        for sql in stmts:
            conn.execute(text(sql))
        conn.commit()

        # One-time data migration: fold the old `users` table into
        # creator_profiles, then drop it. Only runs if `users` still exists
        # (no-op on fresh installs or after the first successful run).
        users_table_exists = conn.execute(
            text("SELECT to_regclass('public.users') IS NOT NULL")
        ).scalar()
        if users_table_exists:
            conn.execute(text("""
                UPDATE creator_profiles cp
                SET email = u.email, google_id = u.google_id,
                    avatar_url = u.avatar_url, is_active = u.is_active
                FROM users u
                WHERE cp.user_id = u.id AND cp.email IS NULL
            """))
            conn.execute(text("""
                INSERT INTO creator_profiles (user_id, full_name, email, google_id, avatar_url, is_active, created_at)
                SELECT u.id, u.name, u.email, u.google_id, u.avatar_url, u.is_active, u.created_at
                FROM users u
                WHERE NOT EXISTS (SELECT 1 FROM creator_profiles cp WHERE cp.user_id = u.id)
            """))
            conn.execute(text("ALTER TABLE creator_profiles DROP COLUMN IF EXISTS user_id"))
            conn.execute(text("DROP TABLE IF EXISTS users"))
            conn.commit()

    # Seed default prompts (on conflict = already exists, keep existing content)
    with SessionLocal() as db:
        for name, content in [
            (FULL_PROMPT_NAME,          FULL_DEFAULT_PROMPT),
            (COAUTHOR_PROMPT_NAME,      COAUTHOR_DEFAULT_PROMPT),
            (DEMOGRAPHICS_PROMPT_NAME,  DEMOGRAPHICS_DEFAULT_PROMPT),
            (CREATOR_NICHE_PROMPT_NAME, CREATOR_NICHE_DEFAULT_PROMPT),
            (TAGS_PROMPT_NAME,          TAGS_DEFAULT_PROMPT),
            (GENDER_PROMPT_NAME,        GENDER_DEFAULT_PROMPT),
            (SPONSOR_CHECK_PROMPT_NAME, SPONSOR_CHECK_DEFAULT_PROMPT),
            (APOLLO_RANK_PROMPT_NAME,   APOLLO_RANK_DEFAULT_PROMPT),
            (BRAND_NICHE_TAGS_PROMPT_NAME, BRAND_NICHE_TAGS_DEFAULT_PROMPT),
            (BRAND_CHECK_PROMPT_NAME,   BRAND_CHECK_DEFAULT_PROMPT),
        ]:
            if not db.query(Prompt).filter(Prompt.name == name).first():
                db.add(Prompt(name=name, content=content))
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _run_migrations()
    yield


app = FastAPI(
    title="Sponsorship Pipeline",
    description="Brand database enrichment pipeline API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(auth_router)

# Lets a separate frontend app (different domain, see FRONTEND_ORIGINS in
# config.py) call this API with credentials (cookies) from the browser.
# allow_credentials=True requires explicit origins — CORS forbids pairing
# it with a wildcard "*". Empty FRONTEND_ORIGINS (the default) means no
# cross-origin frontend is configured, so this is a no-op.
if FRONTEND_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=FRONTEND_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def security_headers(request, call_next):
    """Defense-in-depth headers for the auth cookies and the frontend pages."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response

# 
# SQLAdmin — /admin
# 

class BrandRawAdmin(ModelView, model=BrandRaw):
    name         = "Brand Raw"
    name_plural  = "Brands Raw"
    icon         = "fa-solid fa-seedling"
    column_list  = "__all__"
    column_searchable_list = [
        BrandRaw.name,
        BrandRaw.wikidata_id,
        BrandRaw.entity_type,
        BrandRaw.description,
        BrandRaw.niche,
        BrandRaw.source,
        BrandRaw.website,
        BrandRaw.domain,
        BrandRaw.wikipedia_url,
        BrandRaw.country,
        BrandRaw.instagram_handle,
    ]
    column_sortable_list = [c.name for c in BrandRaw.__table__.columns]
    column_default_sort = [(BrandRaw.id, True)]
    page_size = 15


class BrandNicheAdmin(ModelView, model=BrandNiche):
    name         = "Brand Niche"
    name_plural  = "Brand Niches"
    icon         = "fa-solid fa-tags"
    column_list  = "__all__"
    column_labels = {BrandNiche.brand_raw: "Brand"}
    column_searchable_list = [BrandNiche.niche, BrandNiche.description]
    column_sortable_list   = [c.name for c in BrandNiche.__table__.columns]
    column_default_sort    = [(BrandNiche.id, True)]
    page_size = 15


class MetaAdAdmin(ModelView, model=MetaAd):
    name         = "Meta Ad"
    name_plural  = "Meta Ads"
    icon         = "fa-solid fa-rectangle-ad"
    column_list  = "__all__"
    column_labels      = {MetaAd.brand_raw: "Brand"}
    column_searchable_list = [MetaAd.page_name, MetaAd.page_id, MetaAd.ad_archive_id]
    column_sortable_list   = [c.name for c in MetaAd.__table__.columns]
    column_default_sort    = [(MetaAd.id, True)]
    page_size = 15


class YoutubeSponsorshipAdmin(ModelView, model=YoutubeSponsorship):
    name         = "YouTube Sponsorship"
    name_plural  = "YouTube Sponsorships"
    icon         = "fa-brands fa-youtube"
    column_list  = "__all__"
    column_labels      = {YoutubeSponsorship.brand_raw: "Brand"}
    column_searchable_list = [YoutubeSponsorship.video_title, YoutubeSponsorship.channel_name]
    column_sortable_list   = [c.name for c in YoutubeSponsorship.__table__.columns]
    column_default_sort    = [(YoutubeSponsorship.id, True)]
    page_size = 15


class InstagramPostAdmin(ModelView, model=InstagramPost):
    name         = "Instagram Post"
    name_plural  = "Instagram Posts"
    icon         = "fa-brands fa-instagram"
    column_list  = "__all__"
    column_labels          = {InstagramPost.brand_raw: "Brand"}
    column_searchable_list = [InstagramPost.instagram_handle, InstagramPost.caption, InstagramPost.post_id]
    column_sortable_list   = [c.name for c in InstagramPost.__table__.columns]
    column_default_sort    = [(InstagramPost.id, True)]
    page_size = 15


class InstagramUserAdmin(ModelView, model=InstagramUser):
    name         = "Instagram User"
    name_plural  = "Instagram Users"
    icon         = "fa-solid fa-user-circle"
    column_list  = "__all__"
    column_searchable_list = [
        InstagramUser.username,
        InstagramUser.full_name,
        InstagramUser.bio,
        InstagramUser.country,
        InstagramUser.location,
    ]
    column_sortable_list   = [c.name for c in InstagramUser.__table__.columns]
    column_default_sort    = [(InstagramUser.id, True)]
    page_size = 15


class ContentCreatorREAdmin(ModelView, model=ContentCreatorRE):
    name         = "Content Creator RE"
    name_plural  = "Content Creator RE"
    icon         = "fa-solid fa-address-card"
    column_list  = "__all__"
    column_searchable_list = [ContentCreatorRE.username, ContentCreatorRE.niche]
    column_sortable_list   = [c.name for c in ContentCreatorRE.__table__.columns]
    column_default_sort    = [(ContentCreatorRE.id, True)]
    page_size = 15


class BrandInstagramUserAdmin(ModelView, model=BrandInstagramUser):
    name         = "Brand ↔ Instagram User"
    name_plural  = "Brand Instagram Users"
    icon         = "fa-solid fa-link"
    column_list  = "__all__"
    column_labels = {
        BrandInstagramUser.brand_raw:      "Brand",
        BrandInstagramUser.instagram_user: "Instagram User",
    }
    column_sortable_list = [c.name for c in BrandInstagramUser.__table__.columns]
    column_default_sort  = [(BrandInstagramUser.created_at, True)]
    page_size = 15


class InstagramCreatorCommenterAdmin(ModelView, model=InstagramCreatorCommenter):
    name         = "Creator Commenter"
    name_plural  = "Creator Commenters"
    icon         = "fa-solid fa-comments"
    column_list  = "__all__"
    column_labels = {
        InstagramCreatorCommenter.brand_raw:      "Brand",
        InstagramCreatorCommenter.creator_user:   "Content Creator",
        InstagramCreatorCommenter.commenter_user: "Commenter",
        InstagramCreatorCommenter.source_post_url: "Source Post",
        InstagramCreatorCommenter.comment_likes:  "Comment Likes",
        InstagramCreatorCommenter.comment_text:   "Comment",
    }
    column_sortable_list = [c.name for c in InstagramCreatorCommenter.__table__.columns]
    column_default_sort  = [(InstagramCreatorCommenter.created_at, True)]
    page_size = 15


class PromptAdmin(ModelView, model=Prompt):
    name         = "Prompt"
    name_plural  = "Prompts"
    icon         = "fa-solid fa-wand-magic-sparkles"
    column_list  = "__all__"
    column_searchable_list = [Prompt.name]
    column_sortable_list    = [c.name for c in Prompt.__table__.columns]
    column_default_sort    = [(Prompt.id, True)]
    page_size = 20


class InitialBrandScoreAdmin(ModelView, model=InitialBrandScore):
    name         = "Initial Brand Score"
    name_plural  = "Initial Brand Scores"
    icon         = "fa-solid fa-star"
    column_list  = "__all__"
    column_labels = {InitialBrandScore.brand_raw: "Brand"}
    column_sortable_list = [c.name for c in InitialBrandScore.__table__.columns]
    column_default_sort = [(InitialBrandScore.id, True)]
    page_size = 15


class BrandProfileAdmin(ModelView, model=BrandProfile):
    name         = "Brand Match Profile"
    name_plural  = "Brand Match Profiles"
    icon         = "fa-solid fa-chart-line"
    # embedding is a pgvector column (numpy array) — sqladmin's list/detail
    # rendering does `if value and isinstance(value, Enum)`, which raises
    # "truth value of an array... is ambiguous" for any multi-element
    # array. Must be excluded from column_list (can't combine column_list
    # with column_exclude_list) and from column_sortable_list.
    column_exclude_list = [BrandProfile.embedding]
    column_labels = {BrandProfile.brand_raw: "Brand"}
    column_sortable_list = [c.name for c in BrandProfile.__table__.columns if c.name != "embedding"]
    column_default_sort = [(BrandProfile.brand_raw_id, True)]
    page_size = 15


class BrandContactAdmin(ModelView, model=BrandContact):
    name         = "Brand Contact"
    name_plural  = "Brand Contacts"
    icon         = "fa-solid fa-address-card"
    column_list  = "__all__"
    column_labels = {BrandContact.brand_raw: "Brand"}
    column_searchable_list = [BrandContact.full_name, BrandContact.title, BrandContact.email]
    column_sortable_list = [c.name for c in BrandContact.__table__.columns]
    column_default_sort = [(BrandContact.id, True)]
    page_size = 15


class CreatorProfileAdmin(ModelView, model=CreatorProfile):
    name         = "Creator Profile"
    name_plural  = "Creator Profiles"
    icon         = "fa-solid fa-id-badge"
    # embedding is a pgvector column (numpy array) — see BrandProfileAdmin's
    # comment above for why it must be excluded from list/sort.
    column_exclude_list = [CreatorProfile.embedding]
    column_searchable_list = [CreatorProfile.email, CreatorProfile.full_name, CreatorProfile.creator_handle, CreatorProfile.content_niche]
    column_sortable_list    = [c.name for c in CreatorProfile.__table__.columns if c.name != "embedding"]
    column_default_sort    = [(CreatorProfile.id, True)]
    page_size = 15

# tables

admin = Admin(app, engine)
admin.add_view(CreatorProfileAdmin)
admin.add_view(BrandProfileAdmin)
admin.add_view(BrandContactAdmin)
admin.add_view(InitialBrandScoreAdmin)
admin.add_view(BrandRawAdmin)
admin.add_view(BrandNicheAdmin)
admin.add_view(MetaAdAdmin)
admin.add_view(YoutubeSponsorshipAdmin)
admin.add_view(InstagramPostAdmin)
admin.add_view(InstagramUserAdmin)
admin.add_view(ContentCreatorREAdmin)
admin.add_view(BrandInstagramUserAdmin)
admin.add_view(InstagramCreatorCommenterAdmin)
admin.add_view(PromptAdmin)

# 
# API models
# 

class SeedRequest(BaseModel):
    niche:          str
    limit:          int | None  = None
    country:        str | None  = None
    headquarters:   str | None  = None
    location:       str | None  = None
    operating_area: str | None  = None
    niche_label:    str | None  = None


class EnrichRequest(BaseModel):
    niche:           str | None  = None
    limit_per_step:  int         = 300
    steps:           list[str] | None = None  # None = all steps


# 
# Background job store (in-memory; single-process)
# 

_jobs: dict[str, dict] = {}


def _run_seed_job(job_id: str, body: SeedRequest) -> None:
    db = SessionLocal()
    try:
        inserted = run_seed(
            niche=body.niche,
            db=db,
            limit=body.limit,
            country=body.country,
            headquarters=body.headquarters,
            location=body.location,
            operating_area=body.operating_area,
            niche_label=body.niche_label,
        )
        _jobs[job_id].update({"status": "done", "inserted": inserted})
    except Exception as exc:
        _jobs[job_id].update({"status": "error", "error": str(exc)})
    finally:
        db.close()


def _run_enrich_job(job_id: str, body: EnrichRequest) -> None:
    db = SessionLocal()
    try:
        results = run_signal_enrichment(
            db,
            niche=body.niche,
            limit_per_step=body.limit_per_step,
            steps=body.steps,
        )
        _jobs[job_id].update({"status": "done", "results": results})
    except Exception as exc:
        _jobs[job_id].update({"status": "error", "error": str(exc)})
    finally:
        db.close()

#
# Frontend — /
#

# PostHog's standard array.js loader snippet, with the project token/host
# filled in server-side (from config.py) rather than hardcoded into the
# static HTML — keeps it configurable per environment (dev/staging/prod
# can point at different PostHog projects without editing frontend files).
_POSTHOG_JS_TEMPLATE = """
!function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_for_session unregister unregister_for_session getFeatureFlag getFeatureFlagResult isFeatureEnabled reloadFeatureFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures on onFeatureFlags onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey getNextSurveyStep identify setPersonProperties group resetGroups setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags reset get_distinct_id getGroups get_session_id get_session_replay_url alias set_config startSessionRecording stopSessionRecording sessionRecordingStarted captureException loadToolbar get_property getSessionProperty createPersonProfile opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing clear_opt_in_out_capturing debug".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
posthog.init("__PH_TOKEN__", { api_host: "__PH_HOST__", defaults: "2026-05-30", capture_pageview: true, autocapture: true });

// Called by each page after GET /auth/me resolves, so logged-in users are
// tied to their PostHog events instead of being tracked as anonymous.
window.mavlitIdentifyUser = function(user) {
  if (user && user.email && window.posthog) {
    posthog.identify(user.email, { email: user.email, name: user.name || undefined });
  }
};

// Called on sign-out so the next visitor on this device isn't merged into
// the previous user's PostHog identity.
window.mavlitResetIdentity = function() {
  if (window.posthog) posthog.reset();
};
"""


@app.get("/posthog-init.js", include_in_schema=False)
def posthog_init_js():
    if not POSTHOG_PROJECT_TOKEN:
        js = "console.warn('PostHog: POSTHOG_PROJECT_TOKEN not set — analytics disabled');"
    else:
        js = _POSTHOG_JS_TEMPLATE.replace("__PH_TOKEN__", POSTHOG_PROJECT_TOKEN).replace("__PH_HOST__", POSTHOG_HOST)
    return Response(content=js, media_type="application/javascript")


app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/", include_in_schema=False)
def frontend():
    return FileResponse("frontend/index.html")


@app.get("/signin", include_in_schema=False)
def signin_page():
    return FileResponse("frontend/login.html")


@app.get("/login", include_in_schema=False)
def login_redirect():
    return RedirectResponse(url="/signin", status_code=301)


@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    return FileResponse("frontend/index.html")


@app.get("/creator-profile", include_in_schema=False)
def creator_profile_page():
    return FileResponse("frontend/creator-profile.html")


@app.get("/matches", include_in_schema=False)
def matches_page():
    return FileResponse("frontend/matches.html")


class SeedJobResponse(BaseModel):
    job_id:  str
    status:  str
    niche:   str
    message: str


class SeedStatusResponse(BaseModel):
    status:   str
    niche:    str
    inserted: int
    error:    str | None = None


class EnrichJobResponse(BaseModel):
    job_id:  str
    status:  str
    message: str


class EnrichStatusResponse(BaseModel):
    status:  str
    results: dict | None = None
    error:   str | None  = None

# 
# Endpoints
# 

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/seed", response_model=SeedJobResponse)
def seed_niche(body: SeedRequest, background_tasks: BackgroundTasks):
    """
    Kick off a seed job in the background and return a job_id immediately.
    Poll GET /seed/status/{job_id} to track progress.
    """
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "niche": body.niche, "inserted": 0}
    background_tasks.add_task(_run_seed_job, job_id, body)
    return SeedJobResponse(
        job_id=job_id,
        status="running",
        niche=body.niche,
        message=f"Seeding '{body.niche}' started",
    )


@app.get("/seed/status/{job_id}", response_model=SeedStatusResponse)
def seed_status(job_id: str):
    """Poll every 2 s until status is 'done' or 'error'."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return SeedStatusResponse(
        status=job["status"],
        niche=job["niche"],
        inserted=job.get("inserted", 0),
        error=job.get("error"),
    )


@app.post("/enrich/signals", response_model=EnrichJobResponse)
def enrich_signals(body: EnrichRequest, background_tasks: BackgroundTasks):
    """
    Kick off signal enrichment in the background.
    Steps: wikidata_socials, shopify, tranco, meta_ads, youtube.
    Pass `steps` to run only a subset, e.g. ["shopify", "tranco"].
    Poll GET /enrich/signals/status/{job_id} to track progress.
    """
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "results": {}}
    background_tasks.add_task(_run_enrich_job, job_id, body)
    return EnrichJobResponse(
        job_id=job_id,
        status="running",
        message="Signal enrichment started",
    )


@app.get("/enrich/signals/status/{job_id}", response_model=EnrichStatusResponse)
def enrich_signals_status(job_id: str):
    """Poll until status is 'done' or 'error'."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return EnrichStatusResponse(
        status=job["status"],
        results=job.get("results"),
        error=job.get("error"),
    )


# 
# Scoring endpoints
# 

class ScoreRequest(BaseModel):
    limit: int = 500


class ScoreJobResponse(BaseModel):
    job_id:  str
    status:  str
    message: str


class ScoreStatusResponse(BaseModel):
    status:  str
    scored:  int | None = None
    error:   str | None = None



def _run_scoring_job(job_id: str, limit: int) -> None:
    db = SessionLocal()
    try:
        scored = run_brand_scoring(db, limit=limit)
        _jobs[job_id].update({"status": "done", "scored": scored})
    except Exception as exc:
        _jobs[job_id].update({"status": "error", "error": str(exc)})
    finally:
        db.close()


@app.post("/score/brands", response_model=ScoreJobResponse)
def score_brands(body: ScoreRequest, background_tasks: BackgroundTasks):
    """
    Score all brands that have enrichment data. Results are written to initial_brand_score.
    Poll GET /score/brands/status/{job_id} to track progress.
    """
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "scored": 0}
    background_tasks.add_task(_run_scoring_job, job_id, body.limit)
    return ScoreJobResponse(job_id=job_id, status="running", message="Brand scoring started")


@app.get("/score/brands/status/{job_id}", response_model=ScoreStatusResponse)
def score_brands_status(job_id: str):
    """Poll until status is 'done' or 'error'."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return ScoreStatusResponse(
        status=job["status"],
        scored=job.get("scored"),
        error=job.get("error"),
    )



# ---------------------------------------------------------------------------
# Creator profile endpoints — self-service, one profile per logged-in user
# ---------------------------------------------------------------------------

class CreatorProfileRequest(BaseModel):
    full_name:        str
    creator_handle:   str
    location_city:    str | None = None
    location_country: str | None = None
    bio_tagline:      str | None = None

    content_niche:       str
    sub_niches:          list[str] | None = None
    content_description: str | None = None
    excluded_categories:  list[str] | None = None

    instagram_handle: str | None = None
    youtube_channel:   str | None = None
    facebook_page:     str | None = None
    substack_url:      str | None = None
    substack_subscribers: int | None = None

    instagram_followers: int | None = None
    instagram_following: int | None = None
    youtube_followers:   int | None = None
    facebook_followers:  int | None = None
    facebook_following:  int | None = None

    primary_platform: str | None = None
    follower_count:   int | None = None

    audience_gender_male_pct:   float | None = None
    audience_gender_female_pct: float | None = None
    audience_age_bracket:       str | None = None
    audience_age_min:           int | None = None
    audience_age_max:           int | None = None
    audience_top_countries:     list[dict] | None = None


class CreatorProfileResponse(BaseModel):
    id:         int
    email:      str
    is_active:  bool

    full_name:        str | None = None
    creator_handle:   str | None = None
    location_city:    str | None = None
    location_country: str | None = None
    bio_tagline:      str | None = None

    content_niche:       str | None = None
    sub_niches:          list[str] | None = None
    content_description: str | None = None
    excluded_categories:  list[str] | None = None

    instagram_handle: str | None = None
    youtube_channel:   str | None = None
    facebook_page:     str | None = None
    substack_url:      str | None = None
    substack_subscribers: int | None = None

    instagram_followers: int | None = None
    instagram_following: int | None = None
    youtube_followers:   int | None = None
    facebook_followers:  int | None = None
    facebook_following:  int | None = None

    primary_platform: str | None = None
    follower_count:   int | None = None

    audience_gender_male_pct:   float | None = None
    audience_gender_female_pct: float | None = None
    audience_age_bracket:       str | None = None
    audience_age_min:           int | None = None
    audience_age_max:           int | None = None
    audience_top_countries:     list[dict] | None = None

    creator_tier: str | None = None
    content_tags: list[str] | None = None
    created_at: str
    updated_at: str | None = None


def _profile_to_response(row: CreatorProfile) -> CreatorProfileResponse:
    return CreatorProfileResponse(
        id=row.id,
        email=row.email,
        is_active=row.is_active,
        full_name=row.full_name,
        creator_handle=row.creator_handle,
        location_city=row.location_city,
        location_country=row.location_country,
        bio_tagline=row.bio_tagline,
        content_niche=row.content_niche,
        sub_niches=row.sub_niches,
        content_description=row.content_description,
        excluded_categories=row.excluded_categories,
        instagram_handle=row.instagram_handle,
        youtube_channel=row.youtube_channel,
        facebook_page=row.facebook_page,
        substack_url=row.substack_url,
        substack_subscribers=row.substack_subscribers,
        instagram_followers=row.instagram_followers,
        instagram_following=row.instagram_following,
        youtube_followers=row.youtube_followers,
        facebook_followers=row.facebook_followers,
        facebook_following=row.facebook_following,
        primary_platform=row.primary_platform,
        follower_count=row.follower_count,
        audience_gender_male_pct=row.audience_gender_male_pct,
        audience_gender_female_pct=row.audience_gender_female_pct,
        audience_age_bracket=row.audience_age_bracket,
        audience_age_min=row.audience_age_min,
        audience_age_max=row.audience_age_max,
        audience_top_countries=row.audience_top_countries,
        creator_tier=row.creator_tier,
        content_tags=row.content_tags,
        created_at=str(row.created_at) if row.created_at else "",
        updated_at=str(row.updated_at) if row.updated_at else None,
    )


@app.get("/brand-niches")
def list_brand_niches():
    """
    Distinct niche values from brands_niches AND instagram_users.niche
    (LLM-classified creator niches — see instagram_users.py), for the
    creator-profile form's Primary niche multi-select. Both vocabularies
    feed the same Stage 3 hard filter in matcher.py: a brand can match
    either via its own niche or via a confirmed Instagram collaborator's
    classified niche, so creators need to be able to pick from either set.
    """
    db = SessionLocal()
    try:
        brand_rows = db.query(BrandNiche.niche).distinct().all()
        creator_rows = db.query(InstagramUser.niche).distinct().all()
        all_values = {r[0] for r in brand_rows if r[0]} | {r[0] for r in creator_rows if r[0] and r[0] != "unknown"}
        niches = sorted(all_values, key=str.lower)
        return {"niches": niches}
    finally:
        db.close()


@app.get("/brand-niche-tags")
def list_brand_niche_tags():
    """
    Distinct sub-niche/category tags from brands_niches.tags — LLM-extracted
    in shopify_detect.py from each brand's scraped about-page description
    (e.g. ["sustainable clothing", "streetwear"]), for the creator-profile
    form's Sub-niches multi-select. Used the same way content_niche is in
    the Stage 3 hard filter (matcher.py): a brand matches if the creator's
    sub_niches overlaps with that brand's brands_niches.tags.
    """
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT DISTINCT tag
            FROM brands_niches, jsonb_array_elements_text(tags) AS tag
            WHERE jsonb_typeof(tags) = 'array'
        """)).fetchall()
        tags = sorted({r[0] for r in rows if r[0]}, key=str.lower)
        return {"tags": tags}
    finally:
        db.close()


@app.get("/creator-profile/me", response_model=CreatorProfileResponse)
def get_my_creator_profile(current_user: CreatorProfile = Depends(get_current_user)):
    """Return the logged-in user's creator profile."""
    return _profile_to_response(current_user)


def _run_creator_signals_job(creator_id: int) -> None:
    db = SessionLocal()
    try:
        compute_creator_signals(db, creator_id)
    except Exception:
        logger.exception("Creator signals background job failed for creator_id=%d", creator_id)
    finally:
        db.close()


@app.put("/creator-profile/me", response_model=CreatorProfileResponse)
def upsert_my_creator_profile(
    body: CreatorProfileRequest,
    background_tasks: BackgroundTasks,
    current_user: CreatorProfile = Depends(get_current_user),
):
    """
    Update the logged-in user's creator profile fields. creator_tier is
    bucketed synchronously (instant, no LLM call) so the response reflects
    it immediately; content_tags and the embedding are refreshed in a
    background task (an LLM call + an OpenAI embed call — a few seconds,
    not worth blocking the response for).
    """
    db = SessionLocal()
    try:
        row = db.query(CreatorProfile).filter(CreatorProfile.id == current_user.id).first()
        for field, value in body.model_dump().items():
            setattr(row, field, value)
        row.creator_tier = bucket_creator_tier(row.follower_count)

        db.commit()
        db.refresh(row)
        background_tasks.add_task(_run_creator_signals_job, row.id)
        return _profile_to_response(row)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Matches — Stage 3 real-time matching, self-service (same auth pattern as
# /creator-profile/me — scoped to the logged-in creator, not an arbitrary
# {creator_profile_id} path param, so one creator can't read/refresh
# another's matches without an ownership check).
# ---------------------------------------------------------------------------

class MatchDimension(BaseModel):
    score:  float | None = None
    weight: float


class MatchResult(BaseModel):
    brand_raw_id: int
    brand_name:   str
    total_score:  float
    dimensions:   dict[str, MatchDimension]
    reasons:      list[str]


class MatchesResponse(BaseModel):
    matches:     list[MatchResult]
    cached:      bool
    computed_at: str


@app.get("/matches/me", response_model=MatchesResponse)
def get_my_matches(
    limit: int = 20,
    offset: int = 0,
    current_user: CreatorProfile = Depends(get_current_user),
):
    """
    Ranked brand matches for the logged-in creator (Stage 3). Always
    computes live — no match_cache layer exists yet (deferred by design,
    per the matching doc), so `cached` is always false; the field is kept
    in the response so the contract doesn't change once caching is added.
    """
    db = SessionLocal()
    try:
        matches = get_matches(db, current_user.id, limit=limit, offset=offset)
        return MatchesResponse(matches=matches, cached=False, computed_at=datetime.now(timezone.utc).isoformat())
    finally:
        db.close()


@app.post("/matches/me/refresh", response_model=MatchesResponse)
def refresh_my_matches(
    limit: int = 20,
    current_user: CreatorProfile = Depends(get_current_user),
):
    """
    Force-recompute matches, bypassing any cache. There's no cache to
    bypass yet, so this currently behaves identically to GET /matches/me —
    the separate endpoint is kept for when caching is added.
    """
    db = SessionLocal()
    try:
        matches = get_matches(db, current_user.id, limit=limit, offset=0)
        return MatchesResponse(matches=matches, cached=False, computed_at=datetime.now(timezone.utc).isoformat())
    finally:
        db.close()
