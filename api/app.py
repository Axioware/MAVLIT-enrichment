import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqladmin import Admin, ModelView
from sqlalchemy import text

from config import IS_PRODUCTION
from pipeline.db import Base, BrandContact, BrandInstagramUser, BrandNiche, BrandProfile, BrandRaw, CreatorProfile, InitialBrandScore, InstagramCreatorCommenter, InstagramPost, InstagramUser, MetaAd, Prompt, TiktokPost, TwitterPost, YoutubeSponsorship, SessionLocal, engine
from api.auth import get_current_user, router as auth_router
from pipeline.matching.matcher import get_matches
from pipeline.enrichment.creator_signals import compute_creator_signals
from pipeline.helpers.prompts import (
    FULL_PROMPT_NAME, FULL_DEFAULT_PROMPT,
    COAUTHOR_PROMPT_NAME, COAUTHOR_DEFAULT_PROMPT,
    DEMOGRAPHICS_PROMPT_NAME, DEMOGRAPHICS_DEFAULT_PROMPT,
    GENDER_PROMPT_NAME, GENDER_DEFAULT_PROMPT,
    SPONSOR_CHECK_PROMPT_NAME, SPONSOR_CHECK_DEFAULT_PROMPT,
    APOLLO_RANK_PROMPT_NAME, APOLLO_RANK_DEFAULT_PROMPT,
    TAGS_PROMPT_NAME, TAGS_DEFAULT_PROMPT,
    BRAND_NICHE_TAGS_PROMPT_NAME, BRAND_NICHE_TAGS_DEFAULT_PROMPT,
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
        # Signal enrichment — social handles
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS instagram_handle TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS youtube_channel_id TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS twitter_handle TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS facebook_page TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS facebook_page_id TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS tiktok_handle TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS linkedin_id TEXT",
        # Signal enrichment — website
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS has_official_website BOOLEAN",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS website_source TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS google_discovered_website TEXT",
        # Signal enrichment — detection
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS is_shopify BOOLEAN",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS is_woocommerce BOOLEAN",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS in_tranco_list BOOLEAN",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS tranco_rank INTEGER",
        # Signal enrichment — per-step tracking flags
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS wikidata_enriched BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS shopify_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS google_social_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS tranco_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS meta_ads_fetched BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS youtube_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS instagram_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS tiktok_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS twitter_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS initial_brand_scored BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS top_commenters",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS is_comment_profile_scraped",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS confirmed_creators",
        "ALTER TABLE instagram_posts ADD COLUMN IF NOT EXISTS llm_checked BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE instagram_posts ADD COLUMN IF NOT EXISTS is_users_scraped BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS user_type TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS tier_fit TEXT",
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS captions JSONB",
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
        "ALTER TABLE brand_match_profile ADD COLUMN IF NOT EXISTS has_tiktok BOOLEAN",
        "ALTER TABLE brand_match_profile ADD COLUMN IF NOT EXISTS has_twitter BOOLEAN",
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
        ON CONFLICT (brand_raw_id, niche) DO NOTHING
        """,
        # brands_niches: description mirrored from shopify_detect.py's scraped
        # brands_raw.description, plus Mistral-extracted sub-niche tags.
        "ALTER TABLE brands_niches ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE brands_niches ADD COLUMN IF NOT EXISTS tags JSONB",
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
            (TAGS_PROMPT_NAME,          TAGS_DEFAULT_PROMPT),
            (GENDER_PROMPT_NAME,        GENDER_DEFAULT_PROMPT),
            (SPONSOR_CHECK_PROMPT_NAME, SPONSOR_CHECK_DEFAULT_PROMPT),
            (APOLLO_RANK_PROMPT_NAME,   APOLLO_RANK_DEFAULT_PROMPT),
            (BRAND_NICHE_TAGS_PROMPT_NAME, BRAND_NICHE_TAGS_DEFAULT_PROMPT),
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
    column_list  = [
        BrandRaw.id,
        BrandRaw.wikidata_id,
        BrandRaw.linkedin_id,
        BrandRaw.youtube_channel_id,
        BrandRaw.name,
        BrandRaw.entity_type,
        BrandRaw.description,
        BrandRaw.niche,
        BrandRaw.source,
        BrandRaw.source_confidence,
        BrandRaw.website,
        BrandRaw.domain,
        BrandRaw.wikipedia_url,
        BrandRaw.country,
        BrandRaw.has_official_website,
        BrandRaw.website_source,
        BrandRaw.is_shopify,
        BrandRaw.is_woocommerce,
        BrandRaw.in_tranco_list,
        BrandRaw.tranco_rank,
        BrandRaw.instagram_handle,
        BrandRaw.twitter_handle,
        BrandRaw.tiktok_handle,
        BrandRaw.facebook_page,
        BrandRaw.facebook_page_id,
        BrandRaw.wikidata_enriched,
        BrandRaw.shopify_checked,
        BrandRaw.tranco_checked,
        BrandRaw.meta_ads_fetched,
        BrandRaw.youtube_checked,
        BrandRaw.instagram_checked,
        BrandRaw.tiktok_checked,
        BrandRaw.twitter_checked,
        BrandRaw.initial_brand_scored,
        BrandRaw.created_at,
    ]
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
        BrandRaw.twitter_handle,
        BrandRaw.tiktok_handle,
    ]
    column_sortable_list = [
        BrandRaw.id,
        BrandRaw.niche,
        BrandRaw.source,
        BrandRaw.source_confidence,
        BrandRaw.domain,
        BrandRaw.country,
        BrandRaw.has_official_website,
        BrandRaw.website_source,
        BrandRaw.is_shopify,
        BrandRaw.is_woocommerce,
        BrandRaw.facebook_page_id,
        BrandRaw.in_tranco_list,
        BrandRaw.tranco_rank,
        BrandRaw.wikidata_enriched,
        BrandRaw.shopify_checked,
        BrandRaw.tranco_checked,
        BrandRaw.meta_ads_fetched,
        BrandRaw.youtube_checked,
        BrandRaw.instagram_checked,
        BrandRaw.tiktok_checked,
        BrandRaw.twitter_checked,
        BrandRaw.initial_brand_scored,
        BrandRaw.created_at,
        BrandRaw.description,
    ]
    column_default_sort = [(BrandRaw.id, True)]
    page_size = 15


class BrandNicheAdmin(ModelView, model=BrandNiche):
    name         = "Brand Niche"
    name_plural  = "Brand Niches"
    icon         = "fa-solid fa-tags"
    column_list  = [BrandNiche.id, BrandNiche.brand_raw, BrandNiche.niche, BrandNiche.description, BrandNiche.tags]
    column_labels = {BrandNiche.brand_raw: "Brand"}
    column_searchable_list = [BrandNiche.niche, BrandNiche.description]
    column_sortable_list   = [BrandNiche.id, BrandNiche.niche, BrandNiche.description, BrandNiche.tags]
    page_size = 15


class MetaAdAdmin(ModelView, model=MetaAd):
    name         = "Meta Ad"
    name_plural  = "Meta Ads"
    icon         = "fa-solid fa-rectangle-ad"
    column_list  = [
        MetaAd.id,
        MetaAd.brand_raw,
        MetaAd.ad_archive_id,
        MetaAd.page_name,
        MetaAd.page_id,
        MetaAd.publisher_platforms,
        MetaAd.start_date,
        MetaAd.end_date,
        MetaAd.impressions,
        MetaAd.spend,
        MetaAd.currency,
        MetaAd.fetched_at,
    ]
    column_labels      = {MetaAd.brand_raw: "Brand"}
    column_searchable_list = [MetaAd.page_name, MetaAd.page_id, MetaAd.ad_archive_id]
    column_sortable_list   = [MetaAd.id, MetaAd.brand_raw_id, MetaAd.start_date, MetaAd.fetched_at, MetaAd.impressions, MetaAd.spend]
    column_default_sort    = [(MetaAd.id, True)]
    page_size = 15


class YoutubeSponsorshipAdmin(ModelView, model=YoutubeSponsorship):
    name         = "YouTube Sponsorship"
    name_plural  = "YouTube Sponsorships"
    icon         = "fa-brands fa-youtube"
    column_list  = [
        YoutubeSponsorship.id,
        YoutubeSponsorship.brand_raw,
        YoutubeSponsorship.video_title,
        YoutubeSponsorship.channel_name,
        YoutubeSponsorship.subscriber_count,
        YoutubeSponsorship.tier_fit,
        YoutubeSponsorship.sponsorship_type,
        YoutubeSponsorship.confidence,
        YoutubeSponsorship.matched_keywords,
        YoutubeSponsorship.view_count,
        YoutubeSponsorship.published_at,
        YoutubeSponsorship.video_url,
        YoutubeSponsorship.description_snippet,
        YoutubeSponsorship.comments,
        YoutubeSponsorship.male_pct,
        YoutubeSponsorship.female_pct,
        YoutubeSponsorship.fetched_at,
    ]
    column_labels      = {YoutubeSponsorship.brand_raw: "Brand"}
    column_searchable_list = [YoutubeSponsorship.video_title, YoutubeSponsorship.channel_name]
    column_sortable_list   = [
        YoutubeSponsorship.id,
        YoutubeSponsorship.brand_raw_id,
        YoutubeSponsorship.video_title,
        YoutubeSponsorship.channel_name,
        YoutubeSponsorship.subscriber_count,
        YoutubeSponsorship.tier_fit,
        YoutubeSponsorship.sponsorship_type,
        YoutubeSponsorship.confidence,
        YoutubeSponsorship.matched_keywords,
        YoutubeSponsorship.comments,
        YoutubeSponsorship.male_pct,
        YoutubeSponsorship.female_pct,
        YoutubeSponsorship.view_count,
        YoutubeSponsorship.like_count,
        YoutubeSponsorship.published_at,
        YoutubeSponsorship.fetched_at,
    ]
    column_default_sort    = [(YoutubeSponsorship.confidence, True)]
    page_size = 15


class InstagramPostAdmin(ModelView, model=InstagramPost):
    name         = "Instagram Post"
    name_plural  = "Instagram Posts"
    icon         = "fa-brands fa-instagram"
    column_list  = [
        InstagramPost.id,
        InstagramPost.brand_raw,
        InstagramPost.instagram_handle,
        InstagramPost.post_type,
        InstagramPost.timestamp,
        InstagramPost.likes_count,
        InstagramPost.comments_count,
        InstagramPost.video_view_count,
        InstagramPost.paid_partnership,
        InstagramPost.sponsors,
        InstagramPost.llm_checked,
        InstagramPost.is_users_scraped,
        InstagramPost.mentions,
        InstagramPost.tagged_users,
        InstagramPost.coauthor_producers,
        InstagramPost.followers_count,
        InstagramPost.caption,
        InstagramPost.biography,
        InstagramPost.post_url,
        InstagramPost.fetched_at,
        InstagramPost.business_category_name,
    ]
    column_labels          = {InstagramPost.brand_raw: "Brand"}
    column_searchable_list = [InstagramPost.instagram_handle, InstagramPost.caption, InstagramPost.post_id]
    column_sortable_list   = [
        InstagramPost.id,
        InstagramPost.brand_raw_id,
        InstagramPost.instagram_handle,
        InstagramPost.timestamp,
        InstagramPost.likes_count,
        InstagramPost.comments_count,
        InstagramPost.video_view_count,
        InstagramPost.followers_count,
        InstagramPost.paid_partnership,
        InstagramPost.sponsors,
        InstagramPost.llm_checked,
        InstagramPost.is_users_scraped,
        InstagramPost.coauthor_producers,
        InstagramPost.mentions,
        InstagramPost.tagged_users,
        InstagramPost.biography,
        InstagramPost.fetched_at,
        InstagramPost.business_category_name,
    ]
    column_default_sort    = [(InstagramPost.id, True)]
    page_size = 15


class InstagramUserAdmin(ModelView, model=InstagramUser):
    name         = "Instagram User"
    name_plural  = "Instagram Users"
    icon         = "fa-solid fa-user-circle"
    column_list  = [
        InstagramUser.id,
        InstagramUser.username,
        InstagramUser.user_type,
        InstagramUser.full_name,
        InstagramUser.followers_count,
        InstagramUser.tier_fit,
        InstagramUser.follows_count,
        InstagramUser.posts_count,
        InstagramUser.is_verified,
        InstagramUser.is_business_account,
        InstagramUser.gender,
        InstagramUser.country,
        InstagramUser.language,
        InstagramUser.location,
        InstagramUser.age_group,
        InstagramUser.bio,
        InstagramUser.external_url,
        InstagramUser.profile_url,
        InstagramUser.top_posts,
        InstagramUser.captions,
        InstagramUser.raw_profile,
        InstagramUser.fetched_at,
    ]
    column_searchable_list = [
        InstagramUser.username,
        InstagramUser.full_name,
        InstagramUser.bio,
        InstagramUser.country,
        InstagramUser.location,
    ]
    column_sortable_list   = [
        InstagramUser.id,
        InstagramUser.username,
        InstagramUser.user_type,
        InstagramUser.followers_count,
        InstagramUser.tier_fit,
        InstagramUser.follows_count,
        InstagramUser.posts_count,
        InstagramUser.is_verified,
        InstagramUser.gender,
        InstagramUser.country,
        InstagramUser.language,
        InstagramUser.age_group,
        InstagramUser.top_posts,
        InstagramUser.captions,
        InstagramUser.raw_profile,
        InstagramUser.bio,
        InstagramUser.fetched_at,
    ]
    column_default_sort    = [(InstagramUser.followers_count, True)]
    page_size = 15


class BrandInstagramUserAdmin(ModelView, model=BrandInstagramUser):
    name         = "Brand ↔ Instagram User"
    name_plural  = "Brand Instagram Users"
    icon         = "fa-solid fa-link"
    column_list  = [
        BrandInstagramUser.brand_raw,
        BrandInstagramUser.instagram_user,
        BrandInstagramUser.created_at,
    ]
    column_labels = {
        BrandInstagramUser.brand_raw:      "Brand",
        BrandInstagramUser.instagram_user: "Instagram User",
    }
    column_sortable_list = [BrandInstagramUser.created_at]
    column_default_sort  = [(BrandInstagramUser.created_at, True)]
    page_size = 15


class InstagramCreatorCommenterAdmin(ModelView, model=InstagramCreatorCommenter):
    name         = "Creator Commenter"
    name_plural  = "Creator Commenters"
    icon         = "fa-solid fa-comments"
    column_list  = [
        InstagramCreatorCommenter.brand_raw,
        InstagramCreatorCommenter.creator_user,
        InstagramCreatorCommenter.commenter_user,
        InstagramCreatorCommenter.source_post_url,
        InstagramCreatorCommenter.comment_likes,
        InstagramCreatorCommenter.comment_text,
        InstagramCreatorCommenter.created_at,
    ]
    column_labels = {
        InstagramCreatorCommenter.brand_raw:      "Brand",
        InstagramCreatorCommenter.creator_user:   "Content Creator",
        InstagramCreatorCommenter.commenter_user: "Commenter",
        InstagramCreatorCommenter.source_post_url: "Source Post",
        InstagramCreatorCommenter.comment_likes:  "Comment Likes",
        InstagramCreatorCommenter.comment_text:   "Comment",
    }
    column_sortable_list = [
        InstagramCreatorCommenter.created_at,
        InstagramCreatorCommenter.comment_likes,
    ]
    column_default_sort  = [(InstagramCreatorCommenter.created_at, True)]
    page_size = 15


class TiktokPostAdmin(ModelView, model=TiktokPost):
    name         = "TikTok Post"
    name_plural  = "TikTok Posts"
    icon         = "fa-brands fa-tiktok"
    column_list  = [
        TiktokPost.id,
        TiktokPost.brand_raw,
        TiktokPost.tiktok_handle,
        TiktokPost.create_time,
        TiktokPost.play_count,
        TiktokPost.like_count,
        TiktokPost.comment_count,
        TiktokPost.share_count,
        TiktokPost.collect_count,
        TiktokPost.is_sponsored,
        TiktokPost.is_ad,
        TiktokPost.mentions,
        TiktokPost.hashtags,
        TiktokPost.video_url,
        TiktokPost.fetched_at,
    ]
    column_labels          = {TiktokPost.brand_raw: "Brand"}
    column_searchable_list = [TiktokPost.tiktok_handle, TiktokPost.video_id]
    column_sortable_list   = [
        TiktokPost.id,
        TiktokPost.brand_raw_id,
        TiktokPost.tiktok_handle,
        TiktokPost.create_time,
        TiktokPost.play_count,
        TiktokPost.like_count,
        TiktokPost.comment_count,
        TiktokPost.share_count,
        TiktokPost.is_sponsored,
        TiktokPost.is_ad,
        TiktokPost.fetched_at,
    ]
    column_default_sort    = [(TiktokPost.play_count, True)]
    page_size = 15


class TwitterPostAdmin(ModelView, model=TwitterPost):
    name         = "Twitter Post"
    name_plural  = "Twitter Posts"
    icon         = "fa-brands fa-x-twitter"
    column_list  = [
        TwitterPost.id,
        TwitterPost.brand_raw,
        TwitterPost.twitter_handle,
        TwitterPost.created_at,
        TwitterPost.likes,
        TwitterPost.retweets,
        TwitterPost.comments,
        TwitterPost.quotes,
        TwitterPost.is_sponsored,
        TwitterPost.sponsor_signals,
        TwitterPost.hashtags,
        TwitterPost.mentions,
        TwitterPost.has_media,
        TwitterPost.username,
        TwitterPost.verified,
        TwitterPost.text,
        TwitterPost.permalink,
        TwitterPost.fetched_at,
    ]
    column_labels          = {TwitterPost.brand_raw: "Brand"}
    column_searchable_list = [TwitterPost.twitter_handle, TwitterPost.username, TwitterPost.tweet_id, TwitterPost.text]
    column_sortable_list   = [
        TwitterPost.id,
        TwitterPost.brand_raw_id,
        TwitterPost.twitter_handle,
        TwitterPost.created_at,
        TwitterPost.likes,
        TwitterPost.retweets,
        TwitterPost.comments,
        TwitterPost.is_sponsored,
        TwitterPost.fetched_at,
    ]
    column_default_sort    = [(TwitterPost.likes, True)]
    page_size = 15


class PromptAdmin(ModelView, model=Prompt):
    name         = "Prompt"
    name_plural  = "Prompts"
    icon         = "fa-solid fa-wand-magic-sparkles"
    column_list  = [Prompt.id, Prompt.name, Prompt.content, Prompt.updated_at]
    column_searchable_list = [Prompt.name]
    column_default_sort    = [(Prompt.id, True)]
    page_size = 20


class InitialBrandScoreAdmin(ModelView, model=InitialBrandScore):
    name         = "Initial Brand Score"
    name_plural  = "Initial Brand Scores"
    icon         = "fa-solid fa-star"
    column_list  = [
        InitialBrandScore.id,
        InitialBrandScore.brand_raw,
        InitialBrandScore.total_score,
        InitialBrandScore.score_band,
        InitialBrandScore.influencer_score,
        InitialBrandScore.ad_spend_score,
        InitialBrandScore.legitimacy_score,
        InitialBrandScore.reachability_score,
        InitialBrandScore.enrichment_completeness,
        InitialBrandScore.scored_at,
    ]
    column_labels = {InitialBrandScore.brand_raw: "Brand"}
    column_sortable_list = [
        InitialBrandScore.id,
        InitialBrandScore.total_score,
        InitialBrandScore.score_band,
        InitialBrandScore.influencer_score,
        InitialBrandScore.ad_spend_score,
        InitialBrandScore.legitimacy_score,
        InitialBrandScore.reachability_score,
        InitialBrandScore.enrichment_completeness,
        InitialBrandScore.scored_at,
    ]
    column_default_sort = [(InitialBrandScore.total_score, True)]
    page_size = 15


class BrandProfileAdmin(ModelView, model=BrandProfile):
    name         = "Brand Match Profile"
    name_plural  = "Brand Match Profiles"
    icon         = "fa-solid fa-chart-line"
    column_list  = [
        BrandProfile.brand_raw,
        BrandProfile.sponsorship_activity_score,
        BrandProfile.meta_ads_active,
        BrandProfile.meta_ads_recency_days,
        BrandProfile.meta_ads_no_end_date,
        BrandProfile.meta_ads_count,
        BrandProfile.youtube_last_sponsorship,
        BrandProfile.tiktok_sponsored_count,
        BrandProfile.twitter_sponsored_count,
        BrandProfile.avg_yt_creator_subscribers,
        BrandProfile.avg_ig_collaborator_followers,
        BrandProfile.typical_creator_tier,
        BrandProfile.youtube_highest,
        BrandProfile.youtube_lowest,
        BrandProfile.insta_highest,
        BrandProfile.insta_lowest,
        BrandProfile.audience_gender_male_pct,
        BrandProfile.audience_gender_female_pct,
        BrandProfile.audience_top_countries,
        BrandProfile.audience_age_groups,
        BrandProfile.has_instagram,
        BrandProfile.has_youtube,
        BrandProfile.has_facebook,
        BrandProfile.has_tiktok,
        BrandProfile.has_twitter,
        BrandProfile.has_marketing_contact,
        BrandProfile.contact_mode,
        BrandProfile.best_contact_title_score,
        BrandProfile.computed_at,
    ]
    column_labels = {BrandProfile.brand_raw: "Brand"}
    column_sortable_list = [
        BrandProfile.sponsorship_activity_score,
        BrandProfile.meta_ads_active,
        BrandProfile.meta_ads_recency_days,
        BrandProfile.meta_ads_count,
        BrandProfile.typical_creator_tier,
        BrandProfile.has_instagram,
        BrandProfile.has_youtube,
        BrandProfile.has_facebook,
        BrandProfile.has_marketing_contact,
        BrandProfile.contact_mode,
        BrandProfile.computed_at,
    ]
    column_default_sort = [(BrandProfile.sponsorship_activity_score, True)]
    page_size = 15


class BrandContactAdmin(ModelView, model=BrandContact):
    name         = "Brand Contact"
    name_plural  = "Brand Contacts"
    icon         = "fa-solid fa-address-card"
    column_list  = [
        BrandContact.id,
        BrandContact.brand_raw,
        BrandContact.rank,
        BrandContact.is_enriched,
        BrandContact.full_name,
        BrandContact.title,
        BrandContact.departments,
        BrandContact.subdepartments,
        BrandContact.functions,
        BrandContact.seniority,
        BrandContact.email,
        BrandContact.email_status,
        BrandContact.phone,
        BrandContact.linkedin_url,
        BrandContact.city,
        BrandContact.state,
        BrandContact.country,
        BrandContact.llm_reason,
        BrandContact.fetched_at,
    ]
    column_labels = {BrandContact.brand_raw: "Brand"}
    column_searchable_list = [BrandContact.full_name, BrandContact.title, BrandContact.email]
    column_sortable_list = [
        BrandContact.id, BrandContact.rank, BrandContact.is_enriched, BrandContact.full_name,
        BrandContact.seniority, BrandContact.country, BrandContact.fetched_at,
    ]
    column_default_sort = [(BrandContact.rank, False)]
    page_size = 15


class CreatorProfileAdmin(ModelView, model=CreatorProfile):
    name         = "Creator Profile"
    name_plural  = "Creator Profiles"
    icon         = "fa-solid fa-id-badge"
    column_list  = [
        CreatorProfile.id,
        CreatorProfile.email,
        CreatorProfile.google_id,
        CreatorProfile.is_active,
        CreatorProfile.full_name,
        CreatorProfile.creator_handle,
        CreatorProfile.content_niche,
        CreatorProfile.primary_platform,
        CreatorProfile.location_city,
        CreatorProfile.location_country,
        CreatorProfile.instagram_handle,
        CreatorProfile.tiktok_handle,
        CreatorProfile.youtube_channel,
        CreatorProfile.facebook_page,
        CreatorProfile.substack_url,
        CreatorProfile.sub_niches,
        CreatorProfile.content_description,
        CreatorProfile.excluded_categories,
        CreatorProfile.follower_count,
        CreatorProfile.audience_age_min,
        CreatorProfile.audience_age_max,
        CreatorProfile.content_tags,
        CreatorProfile.creator_tier,
        CreatorProfile.created_at,
        CreatorProfile.updated_at,
    ]
    column_searchable_list = [CreatorProfile.email, CreatorProfile.full_name, CreatorProfile.creator_handle, CreatorProfile.content_niche]
    column_sortable_list    = [
        CreatorProfile.id, CreatorProfile.email, CreatorProfile.is_active,
        CreatorProfile.full_name, CreatorProfile.content_niche,
        CreatorProfile.primary_platform, CreatorProfile.creator_tier, CreatorProfile.created_at,
    ]
    column_default_sort    = [(CreatorProfile.created_at, True)]
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
admin.add_view(BrandInstagramUserAdmin)
admin.add_view(InstagramCreatorCommenterAdmin)
admin.add_view(PromptAdmin)
admin.add_view(TiktokPostAdmin)
admin.add_view(TwitterPostAdmin)

# 
# API models
# 

class SeedRequest(BaseModel):
    niche:          str
    use_google:     bool        = False
    limit:          int | None  = None
    country:        str | None  = None
    headquarters:   str | None  = None
    location:       str | None  = None
    operating_area: str | None  = None


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
            use_google=body.use_google,
            limit=body.limit,
            country=body.country,
            headquarters=body.headquarters,
            location=body.location,
            operating_area=body.operating_area,
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
    Steps: wikidata_socials, shopify, google_social, tranco, meta_ads, youtube.
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
    tiktok_handle:     str | None = None
    youtube_channel:   str | None = None
    facebook_page:     str | None = None
    substack_url:      str | None = None
    substack_subscribers: int | None = None

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
    tiktok_handle:     str | None = None
    youtube_channel:   str | None = None
    facebook_page:     str | None = None
    substack_url:      str | None = None
    substack_subscribers: int | None = None

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
        tiktok_handle=row.tiktok_handle,
        youtube_channel=row.youtube_channel,
        facebook_page=row.facebook_page,
        substack_url=row.substack_url,
        substack_subscribers=row.substack_subscribers,
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
    background task (an LLM call + a Mistral embed call — a few seconds,
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
