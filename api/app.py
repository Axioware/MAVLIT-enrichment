import logging
import uuid
from typing import Annotated, Literal
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import hashlib
import hmac

import wtforms
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqladmin.filters import AllUniqueStringValuesFilter, OperationColumnFilter
from sqlalchemy import String, cast, or_, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import Select

from api.advisory import router as advisory_router
from api.early_access import router as early_access_router
from api.auth import (
    get_completed_user,
    get_current_user,
    get_current_user_optional,
    is_profile_complete,
)
from api.auth import router as auth_router
from api.brands import router as brands_router
from api.dashboard import router as dashboard_api_router
from api.creator_posts import router as creator_posts_router
from pipeline.creator_posts import clear_posts_if_username_changed
from api.own_brands import router as own_brands_router
from api.pitches import router as pitches_router
from api.saved_brands import router as saved_brands_router
from api.schemas import CreatorProfileResponse
from api import attempt_limits, usage_limits
from api.body_limits import BodySizeLimitMiddleware
import config
from api.operator import MIN_RECOMMENDED_LENGTH, allow_unauthenticated_admin, operator_configured, passkey, passkey_matches, require_operator
from api.settings import RUN_MIGRATIONS, drop_migration_credentials
from api.schemas import profile_to_response as _profile_to_response
from config import (
    FRONTEND_ORIGINS,
    ADMIN_SESSION_SECRET,
    IS_PRODUCTION,
    POSTHOG_HOST,
    POSTHOG_PROJECT_TOKEN,
)
from pipeline.creator_content.instagram_description import (
    generate_instagram_description,
)
from pipeline.creator_content.youtube_description import generate_youtube_description
from pipeline.db import (
    BrandContact,
    BrandInstagramUser,
    BrandNiche,
    BrandProfile,
    BrandRaw,
    ContentCreatorRE,
    ContractReview,
    EarlyAccessSignup,
    CreatorBrandLlmRanking,
    CreatorBrandLlmV3Ranking,
    CreatorNiche,
    CreatorProfile,
    FacebookBrandedContentCheck,
    InitialBrandScore,
    InstagramCreatorCommenter,
    InstagramPost,
    InstagramUser,
    CreatorInstagramPost,
    CreatorLlmCost,
    LlmUsage,
    MetaAd,
    OwnBrandJob,
    Pitch,
    Prompt,
    RateEstimate,
    SavedBrand,
    BatchSessionLocal,
    SessionLocal,
    get_db,
    TestBrandsWithInstagramPosts,
    TestCreatorBrandPartnershipPost,
    TestNiche,
    YoutubeSponsorship,
    engine,
)
from pipeline.enrichment.creator_signals import compute_creator_signals
from pipeline.enrichment.initial_brand_scoring import run_brand_scoring
from pipeline.enrichment.orchestrator import run_signal_enrichment
from pipeline.enrichment_re.content_creator_re import add_content_creator_re
from pipeline.helpers.creator_tier import bucket_creator_tier
from pipeline.helpers.gpt_llm import llm_usage_scope
from pipeline.helpers.passwords import hash_password
from pipeline.helpers.db import escape_like
from pipeline.matching.llm_ranking_v2 import rank_brands_with_llm
from pipeline.matching.llm_ranking_v3 import MIN_MATCH_CONFIDENCE, rank_brands_with_llm_v3
from pipeline.matching.match_text import generate_match_reasons
from pipeline.matching.matcher import apply_brand_hard_filters, get_matches
from pipeline.migrations import run_migrations
from pipeline.seed import run_seed

logger = logging.getLogger(__name__)

def _run_migrations() -> None:
    """Run the migrations in pipeline/migrations.py (the lifespan calls this unless RUN_MIGRATIONS is off)."""
    run_migrations()


@asynccontextmanager
async def lifespan(app: FastAPI):
    drop_migration_credentials()
    if RUN_MIGRATIONS:
        _run_migrations()
    else:
        logger.info("RUN_MIGRATIONS is off: skipping the startup migrations (run `python -m pipeline.migrate` when the schema changes)")
    yield


# The interactive docs and the OpenAPI schema list every route (the operator ones too): public in development only.
_DOCS = {"docs_url": None, "redoc_url": None, "openapi_url": None} if IS_PRODUCTION else {}

app = FastAPI(
    title="Sponsorship Pipeline",
    description="Brand database enrichment pipeline API",
    version="0.1.0",
    lifespan=lifespan,
    **_DOCS,
)
app.add_middleware(BodySizeLimitMiddleware)   # 413 JSON for an oversized body, before any route parses it

_QUERY_CANCELED = "57014"   # statement_timeout (or a cancel)
_LOCK_NOT_AVAILABLE = "55P03"   # lock_timeout


@app.exception_handler(DBAPIError)
async def _database_error(request: Request, exc: DBAPIError):
    """A cancelled or lock-blocked query is a retryable 503; any other database error a generic 500. Never the SQL text."""
    logger.error("Database error on %s %s: %s", request.method, request.url.path, type(exc).__name__, exc_info=exc)
    if getattr(exc.orig, "pgcode", None) in (_QUERY_CANCELED, _LOCK_NOT_AVAILABLE):
        return JSONResponse(status_code=503, content={"detail": "Database busy, retry"}, headers={"Retry-After": "5"})
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.exception_handler(PoolTimeoutError)
async def _pool_exhausted(request: Request, exc: PoolTimeoutError):
    """No pooled connection became free within pool_timeout: retryable, not a server fault."""
    logger.error("Connection pool exhausted on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=503, content={"detail": "Server busy, retry shortly"}, headers={"Retry-After": "5"})


app.include_router(auth_router)
app.include_router(brands_router)
app.include_router(saved_brands_router)
app.include_router(pitches_router)
app.include_router(own_brands_router)
app.include_router(creator_posts_router)
app.include_router(dashboard_api_router)
app.include_router(advisory_router)
app.include_router(early_access_router)

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


# --- CSRF -----------------------------------------------------------------------------------------------------------------------------
# A browser sends the login cookie with a request that another website triggers. For cookie-authenticated unsafe methods the request must
# therefore come from this site or a configured frontend: with an Origin header it has to be our own origin or one of FRONTEND_ORIGINS; without
# one, a browser that says Sec-Fetch-Site: cross-site is refused. Requests with neither header (curl, servers) carry no ambient cookie to abuse.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# No cookie involved: /health*, the public sign-up (called from the landing page on another site) and the operator endpoints (Bearer passkey).
_CSRF_EXEMPT_PREFIXES = ("/health", "/early-access", "/seed", "/enrich/signals", "/score/brands", "/content-creator-re/bulk-add")


def _origin_allowed(request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return request.headers.get("sec-fetch-site", "").lower() != "cross-site"
    origin = origin.strip().rstrip("/")
    if origin in FRONTEND_ORIGINS:
        return True
    host = (request.headers.get("host") or "").lower()
    from urllib.parse import urlparse
    return bool(host) and urlparse(origin).netloc.lower() == host   # our own origin; "null" and anything else is refused


@app.middleware("http")
async def csrf_origin_check(request, call_next):
    if request.method in _UNSAFE_METHODS and not request.url.path.startswith(_CSRF_EXEMPT_PREFIXES) and not _origin_allowed(request):
        return JSONResponse(status_code=403, content={"detail": "Cross-site request refused"})
    return await call_next(request)


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
    column_searchable_list = [InstagramPost.instagram_handle, InstagramPost.post_id]
    column_sortable_list   = [c.name for c in InstagramPost.__table__.columns]
    column_default_sort    = [(InstagramPost.id, True)]
    can_export = False   # unbounded export of a large table
    page_size = 15


class TestBrandsWithInstagramPostsAdmin(ModelView, model=TestBrandsWithInstagramPosts):
    name         = "Brand w/ IG Posts (test)"
    name_plural  = "Brands w/ IG Posts (test)"
    category     = "Test"
    icon         = "fa-solid fa-flask"
    column_list  = "__all__"
    column_searchable_list = [TestBrandsWithInstagramPosts.brand_name]
    column_sortable_list   = [c.name for c in TestBrandsWithInstagramPosts.__table__.columns]
    column_default_sort    = [(TestBrandsWithInstagramPosts.instagram_post_count, True)]
    page_size = 15


class TestNicheAdmin(ModelView, model=TestNiche):
    name         = "Niche (test)"
    name_plural  = "Niches (test)"
    category     = "Test"
    icon         = "fa-solid fa-list"
    column_list  = [TestNiche.niche, TestNiche.description]
    column_searchable_list = [TestNiche.niche, TestNiche.description]
    column_sortable_list   = [TestNiche.niche, TestNiche.description]
    column_default_sort    = [(TestNiche.niche, False)]
    form_excluded_columns  = [TestNiche.embedding]
    page_size = 15


class TestCreatorBrandPartnershipPostAdmin(ModelView, model=TestCreatorBrandPartnershipPost):
    name         = "Creator Brand Post (test)"
    name_plural  = "Creator Brand Posts (test)"
    category     = "Test"
    icon         = "fa-solid fa-flask-vial"
    column_list  = "__all__"
    column_labels = {
        TestCreatorBrandPartnershipPost.brand_raw: "Brand",
        TestCreatorBrandPartnershipPost.content_creator_re: "Content Creator RE",
    }
    column_searchable_list = [
        TestCreatorBrandPartnershipPost.brand_name,
        TestCreatorBrandPartnershipPost.brand_instagram_handle,
        TestCreatorBrandPartnershipPost.creator_username,
        TestCreatorBrandPartnershipPost.creator_name,
        TestCreatorBrandPartnershipPost.post_url,
    ]
    column_sortable_list = [c.name for c in TestCreatorBrandPartnershipPost.__table__.columns]
    column_default_sort  = [(TestCreatorBrandPartnershipPost.detected_at, True)]
    page_size = 15


class FacebookBrandedContentCheckAdmin(ModelView, model=FacebookBrandedContentCheck):
    name         = "Facebook Branded Content Check"
    name_plural  = "Facebook Branded Content Checks"
    category     = "Test"
    icon         = "fa-brands fa-facebook"
    column_list  = "__all__"
    column_searchable_list = [
        FacebookBrandedContentCheck.username,
        FacebookBrandedContentCheck.instagram_id,
        FacebookBrandedContentCheck.status,
    ]
    column_sortable_list = [c.name for c in FacebookBrandedContentCheck.__table__.columns]
    column_default_sort  = [(FacebookBrandedContentCheck.checked_at, True)]
    page_size = 15


class LlmUsageAdmin(ModelView, model=LlmUsage):
    """
    LLM cost log: one read-only row per successful OpenAI call (written by
    pipeline/helpers/gpt_llm.py). Sort by cost_usd to find the expensive
    calls, filter by model / endpoint, search the context label (e.g.
    "llm brand ranking creator_id=4"). Totals by day / model / feature:
    python scripts/llm_cost_report.py --days 30
    """
    name         = "LLM Usage"
    name_plural  = "LLM Usage (cost)"
    category     = "Test"
    icon         = "fa-solid fa-coins"
    column_list  = "__all__"
    column_labels = {LlmUsage.cost_usd: "Cost (USD)", LlmUsage.duration_ms: "Duration (ms)",
                     LlmUsage.creator_profile_id: "MAVLIT user id"}
    column_searchable_list = [LlmUsage.context, LlmUsage.model, LlmUsage.endpoint, LlmUsage.feature]
    column_sortable_list   = [c.name for c in LlmUsage.__table__.columns]
    column_default_sort    = [(LlmUsage.created_at, True)]
    column_filters = [
        AllUniqueStringValuesFilter(LlmUsage.model),
        AllUniqueStringValuesFilter(LlmUsage.endpoint),
        AllUniqueStringValuesFilter(LlmUsage.feature),
        OperationColumnFilter(LlmUsage.creator_profile_id),
        OperationColumnFilter(LlmUsage.cost_usd),
        OperationColumnFilter(LlmUsage.created_at),
    ]
    can_create = False
    can_edit   = False
    can_delete = False
    page_size  = 25


class CreatorLlmCostAdmin(ModelView, model=CreatorLlmCost):
    """
    LLM cost per MAVLIT user: one row per creator with calls, total cost and the
    cost of each user-triggered feature (generate my description, profile
    signals, matches v2, matches v3; other = pitch / rate estimate / contract
    advice), and how many Instagram posts were scraped (Apify, e.g. 20 for one
    "generate my description" click; count only, no Apify cost). Built from
    llm_usage / apify_usage rows that carry a creator_profile_id - open
    "LLM Usage (cost)" and filter by MAVLIT user id for the individual calls.
    """
    name         = "LLM Cost per User"
    name_plural  = "LLM Cost + Posts per User"
    category     = "Test"
    icon         = "fa-solid fa-user-tag"
    column_list  = "__all__"
    column_labels = {
        CreatorLlmCost.creator_profile_id: "MAVLIT user id",
        CreatorLlmCost.total_cost_usd: "Total (USD)",
        CreatorLlmCost.description_usd: "Description (USD)",
        CreatorLlmCost.profile_signals_usd: "Profile signals (USD)",
        CreatorLlmCost.match_v2_usd: "Matches v2 (USD)",
        CreatorLlmCost.match_v3_usd: "Matches v3 (USD)",
        CreatorLlmCost.other_usd: "Other (USD)",
        CreatorLlmCost.instagram_scrapes: "Instagram scrapes",
        CreatorLlmCost.instagram_posts_scraped: "Instagram posts scraped",
    }
    column_searchable_list = [CreatorLlmCost.email, CreatorLlmCost.full_name]
    column_sortable_list   = [c.name for c in CreatorLlmCost.__table__.columns]
    column_default_sort    = [(CreatorLlmCost.total_cost_usd, True)]
    can_create = False
    can_edit   = False
    can_delete = False
    page_size  = 25


class CreatorInstagramPostAdmin(ModelView, model=CreatorInstagramPost):
    """Posts scraped for a MAVLIT user when they chose "Let MAVLIT generate it from my content"."""
    name         = "Creator Instagram Post"
    name_plural  = "Creator Instagram Posts"
    category     = "Test"
    icon         = "fa-brands fa-instagram"
    column_list  = "__all__"
    column_labels = {CreatorInstagramPost.creator_profile_id: "MAVLIT user id", CreatorInstagramPost.creator: "Creator"}
    column_searchable_list = [CreatorInstagramPost.instagram_handle, CreatorInstagramPost.post_url]
    column_sortable_list   = [c.name for c in CreatorInstagramPost.__table__.columns]
    column_default_sort    = [(CreatorInstagramPost.fetched_at, True)]
    column_filters = [OperationColumnFilter(CreatorInstagramPost.creator_profile_id)]
    can_create = False
    can_edit   = False
    page_size  = 25


class CreatorNicheAdmin(ModelView, model=CreatorNiche):
    name         = "Creator Niche"
    name_plural  = "Creator Niches"
    category     = "Test"
    icon         = "fa-solid fa-id-card"
    column_list  = "__all__"
    column_labels = {
        CreatorNiche.instagram_user: "Instagram User",
    }
    column_searchable_list = [
        CreatorNiche.username,
        CreatorNiche.niche,
        CreatorNiche.description,
    ]
    column_sortable_list = [c.name for c in CreatorNiche.__table__.columns]
    column_default_sort = [(CreatorNiche.id, True)]
    page_size = 15


class InstagramUserAdmin(ModelView, model=InstagramUser):
    can_export = False   # unbounded export of a large table
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

    async def on_model_change(self, data: dict, model, is_created: bool, request) -> None:
        """
        Reject a save (create or edit) whose "url" OR "username" already
        exists on a DIFFERENT row — content_creator_re has no DB-level
        unique constraint on either, so without this, adding the same
        Instagram account twice (by URL or by username) would silently
        create a duplicate scrape target. Raising here surfaces a clean
        error on the form itself (sqladmin catches it and re-renders with
        context["error"] = str(e), see application.py's create/edit
        routes) rather than crashing — same pattern already used by
        CreatorProfileAdmin.on_model_change for its own validation.
        """
        url      = (data.get("url") or "").strip()
        username = (data.get("username") or "").strip()
        if not url and not username:
            return

        db = SessionLocal()
        try:
            existing_url = None
            if url:
                q = db.query(ContentCreatorRE.id).filter(ContentCreatorRE.url == url)
                if not is_created:
                    q = q.filter(ContentCreatorRE.id != model.id)
                existing_url = q.first()

            existing_username = None
            if username:
                q = db.query(ContentCreatorRE.id).filter(ContentCreatorRE.username == username)
                if not is_created:
                    q = q.filter(ContentCreatorRE.id != model.id)
                existing_username = q.first()
        finally:
            db.close()

        if existing_url and existing_username:
            raise ValueError(
                f"This URL (row id={existing_url.id}) and username (row id={existing_username.id}) "
                "are already in Content Creator RE — not saved."
            )
        if existing_url:
            raise ValueError(f"This URL is already in Content Creator RE (row id={existing_url.id}) — not saved.")
        if existing_username:
            raise ValueError(f"This username is already in Content Creator RE (row id={existing_username.id}) — not saved.")


class BrandInstagramUserAdmin(ModelView, model=BrandInstagramUser):
    name         = "Brand ↔ Instagram User"
    name_plural  = "Brand Instagram Users"
    icon         = "fa-solid fa-link"
    column_list  = "__all__"
    column_labels = {
        BrandInstagramUser.brand_raw:      "Brand",
        BrandInstagramUser.instagram_user: "Instagram User",
    }
    # BrandInstagramUser has no text columns of its own (just two FK ints) —
    # dotted paths search the related BrandRaw/InstagramUser columns instead
    # (sqladmin auto-joins them; see ModelView.search_query in sqladmin).
    column_searchable_list = ["brand_raw.name", "brand_raw.instagram_handle", "instagram_user.username"]
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
    column_searchable_list = [
        "brand_raw.name", "creator_user.username", "commenter_user.username",
        InstagramCreatorCommenter.comment_text, InstagramCreatorCommenter.source_post_url,
    ]
    column_sortable_list = [c.name for c in InstagramCreatorCommenter.__table__.columns]
    column_default_sort  = [(InstagramCreatorCommenter.created_at, True)]
    page_size = 15

    def search_query(self, stmt: Select, term: str) -> Select:
        """
        Override sqladmin's default join-per-dotted-path search: creator_user
        and commenter_user both point at instagram_users, and joining that
        table twice unaliased makes Postgres raise DuplicateAlias ("table
        name 'instagram_users' specified more than once"). Alias each side
        explicitly instead.
        """
        creator_alias = aliased(InstagramUser)
        commenter_alias = aliased(InstagramUser)
        pattern = f"%{escape_like(term)}%"
        return (
            stmt
            .join(InstagramCreatorCommenter.brand_raw)
            .join(creator_alias, InstagramCreatorCommenter.creator_user_id == creator_alias.id)
            .join(commenter_alias, InstagramCreatorCommenter.commenter_user_id == commenter_alias.id)
            .filter(or_(
                cast(BrandRaw.name, String).ilike(pattern, escape="\\"),
                cast(creator_alias.username, String).ilike(pattern, escape="\\"),
                cast(commenter_alias.username, String).ilike(pattern, escape="\\"),
                cast(InstagramCreatorCommenter.comment_text, String).ilike(pattern, escape="\\"),
                cast(InstagramCreatorCommenter.source_post_url, String).ilike(pattern, escape="\\"),
            ))
        )


class PromptAdmin(ModelView, model=Prompt):
    name         = "Prompt"
    name_plural  = "Prompts"
    icon         = "fa-solid fa-wand-magic-sparkles"
    column_list  = "__all__"
    column_searchable_list = [Prompt.name]
    column_sortable_list    = [c.name for c in Prompt.__table__.columns]
    column_default_sort    = [(Prompt.id, True)]
    page_size = 20


class SavedBrandAdmin(ModelView, model=SavedBrand):
    name         = "Saved Brand"
    name_plural  = "Saved Brands"
    icon         = "fa-solid fa-bookmark"
    column_list  = "__all__"
    column_labels = {SavedBrand.creator: "Creator", SavedBrand.brand_raw: "Brand"}
    column_searchable_list = ["brand_raw.name", "creator.email", "creator.full_name"]
    column_sortable_list = [c.name for c in SavedBrand.__table__.columns]
    column_default_sort  = [(SavedBrand.created_at, True)]
    page_size = 15


class OwnBrandJobAdmin(ModelView, model=OwnBrandJob):
    """Brands MAVLIT users added through "my own brand" on the pitch page, and how the follow-up pipeline went."""
    name         = "Own Brand Job"
    name_plural  = "Own Brand Jobs"
    category     = "Test"
    icon         = "fa-solid fa-store"
    column_list  = [
        OwnBrandJob.created_at, OwnBrandJob.creator_profile_id, OwnBrandJob.instagram_handle, OwnBrandJob.brand_raw_id,
        OwnBrandJob.created_brand, OwnBrandJob.status, OwnBrandJob.stage, OwnBrandJob.error,
        OwnBrandJob.followup_status, OwnBrandJob.followup_log,
    ]
    column_labels = {OwnBrandJob.creator_profile_id: "MAVLIT user id", OwnBrandJob.brand_raw_id: "Brand id"}
    column_searchable_list = [OwnBrandJob.instagram_handle, OwnBrandJob.status, OwnBrandJob.followup_status]
    column_sortable_list   = [c.name for c in OwnBrandJob.__table__.columns]
    column_default_sort    = [(OwnBrandJob.created_at, True)]
    can_create = False
    can_edit   = False
    page_size  = 25


class PitchAdmin(ModelView, model=Pitch):
    name         = "Pitch"
    name_plural  = "Pitches"
    icon         = "fa-solid fa-paper-plane"
    column_list  = "__all__"
    column_labels = {Pitch.creator: "Creator", Pitch.brand_raw: "Brand"}
    column_searchable_list = [Pitch.brand_name, Pitch.status]
    column_sortable_list    = [c.name for c in Pitch.__table__.columns]
    column_default_sort    = [(Pitch.id, True)]
    page_size = 15


class EarlyAccessSignupAdmin(ModelView, model=EarlyAccessSignup):
    """Sign-ups from the landing page's "Join the founding creators" form."""
    name         = "Early Access Sign-up"
    name_plural  = "Early Access Sign-ups"
    icon         = "fa-solid fa-user-plus"
    column_list  = [EarlyAccessSignup.id, EarlyAccessSignup.created_at, EarlyAccessSignup.email, EarlyAccessSignup.handle,
                    EarlyAccessSignup.platform, EarlyAccessSignup.niche, EarlyAccessSignup.followers]
    column_labels = {EarlyAccessSignup.created_at: "Signed up"}
    column_searchable_list = [EarlyAccessSignup.email, EarlyAccessSignup.handle]
    column_sortable_list = [c.name for c in EarlyAccessSignup.__table__.columns]
    column_default_sort  = [(EarlyAccessSignup.id, True)]
    column_filters = [AllUniqueStringValuesFilter(EarlyAccessSignup.platform), AllUniqueStringValuesFilter(EarlyAccessSignup.niche),
                      AllUniqueStringValuesFilter(EarlyAccessSignup.followers), OperationColumnFilter(EarlyAccessSignup.created_at)]
    can_create = False
    page_size = 25


class RateEstimateAdmin(ModelView, model=RateEstimate):
    """
    Rate Intelligence estimates AND their log: one row per request from Deal Tools - who (MAVLIT user id + the Instagram
    handle at that moment), which brand and deal terms, when, the estimate, and the LLM request behind it (model, tokens,
    cost, status). Open a row to read the full prompt (input) and the LLM's raw answer (output). Failed requests are
    rows with status "error" and no rates.
    """
    name         = "Rate Estimate"
    name_plural  = "Rate Estimates"
    icon         = "fa-solid fa-money-bill-trend-up"
    column_list  = [RateEstimate.id, RateEstimate.created_at, RateEstimate.creator, RateEstimate.creator_handle, RateEstimate.brand_name,
                    RateEstimate.deliverable_type, RateEstimate.exclusivity, RateEstimate.usage, RateEstimate.duration_months,
                    RateEstimate.rate_min, RateEstimate.rate_typical, RateEstimate.rate_max, RateEstimate.confidence,
                    RateEstimate.input_tokens, RateEstimate.output_tokens, RateEstimate.cost_usd, RateEstimate.status]
    column_labels = {RateEstimate.creator: "Creator", RateEstimate.brand_raw: "Brand", RateEstimate.creator_handle: "Instagram handle",
                     RateEstimate.cost_usd: "Cost (USD)", RateEstimate.input_tokens: "Input tokens",
                     RateEstimate.output_tokens: "Output tokens", RateEstimate.duration_ms: "Duration (ms)"}
    column_searchable_list = [
        "brand_raw.name", "creator.email", "creator.full_name", RateEstimate.creator_handle, RateEstimate.brand_name,
        RateEstimate.platform, RateEstimate.deliverable_type, RateEstimate.status,
    ]
    column_sortable_list = [c.name for c in RateEstimate.__table__.columns if c.name not in ("prompt", "output", "details")]
    column_default_sort  = [(RateEstimate.id, True)]
    column_filters = [
        OperationColumnFilter(RateEstimate.creator_profile_id),
        AllUniqueStringValuesFilter(RateEstimate.status),
        AllUniqueStringValuesFilter(RateEstimate.deliverable_type),
        OperationColumnFilter(RateEstimate.cost_usd),
        OperationColumnFilter(RateEstimate.created_at),
    ]
    page_size = 15


class ContractReviewAdmin(ModelView, model=ContractReview):
    name         = "Contract Review"
    name_plural  = "Contract Reviews"
    icon         = "fa-solid fa-file-contract"
    column_list  = "__all__"
    column_labels = {ContractReview.creator: "Creator"}
    column_searchable_list = ["creator.email", "creator.full_name", ContractReview.summary]
    can_export = False   # contract texts
    column_sortable_list = [c.name for c in ContractReview.__table__.columns]
    column_default_sort  = [(ContractReview.id, True)]
    page_size = 15


class InitialBrandScoreAdmin(ModelView, model=InitialBrandScore):
    name         = "Initial Brand Score"
    name_plural  = "Initial Brand Scores"
    icon         = "fa-solid fa-star"
    column_list  = "__all__"
    column_labels = {InitialBrandScore.brand_raw: "Brand"}
    column_searchable_list = ["brand_raw.name", InitialBrandScore.score_band]
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
    column_searchable_list = ["brand_raw.name", BrandProfile.typical_creator_tier, BrandProfile.contact_mode]
    column_sortable_list = [c.name for c in BrandProfile.__table__.columns if c.name != "embedding"]
    column_default_sort = [(BrandProfile.brand_raw_id, True)]
    page_size = 15


class BrandContactAdmin(ModelView, model=BrandContact):
    name         = "Brand Contact"
    name_plural  = "Brand Contacts"
    icon         = "fa-solid fa-address-card"
    column_list  = "__all__"
    column_labels = {BrandContact.brand_raw: "Brand"}
    can_export = False    # would export every contact's email
    column_searchable_list = [BrandContact.full_name, BrandContact.title, BrandContact.email]
    column_sortable_list = [c.name for c in BrandContact.__table__.columns]
    column_default_sort = [(BrandContact.id, True)]
    page_size = 15


class CreatorProfileAdmin(ModelView, model=CreatorProfile):
    """
    Accounts are admin-provisioned: set email + password here and hand
    those credentials to the creator — POST /auth/login checks straight
    against this row's password_hash, there's no separate login table.
    The password field renders as a real password input (browsers never
    echo its current value back, unlike a normal text field) — type a new
    plain-text password to set/change it, or leave it blank when editing
    to keep the existing password unchanged. on_model_change hashes
    whatever's typed before it ever reaches the DB; plain text is never
    stored.
    """
    name         = "Creator Profile"
    name_plural  = "Creator Profiles"
    icon         = "fa-solid fa-id-badge"
    # embedding is a pgvector column (numpy array) — see BrandProfileAdmin's
    # comment above for why it must be excluded from list/sort. It must
    # ALSO be excluded from the form (sqladmin has no form converter for
    # pgvector's type and hard-crashes create/edit with NoConverterFound
    # otherwise — column_exclude_list alone only hides it from list/detail).
    #
    # Platform sub-niches, excluded_categories, and content_tags are also
    # excluded from the form: they're list-shaped JSONB, populated by the
    # creator's own profile flow or LLM extraction elsewhere — never meant to
    # hand here. sqladmin's generic JSON widget defaults an untouched JSONB
    # field to "{}" (an empty OBJECT) on create, not "[]" — CreatorProfile
    # Response's schema requires a list for these, so any admin-created row
    # that left them at that default 500'd on every /auth/login for that
    # user (confirmed live: pydantic_core.ValidationError, "Input should be
    # a valid list", input_value={}). Excluding them from the form removes
    # the only way admin-create was producing that bad value.
    column_exclude_list = [CreatorProfile.embedding, CreatorProfile.password_hash]
    column_details_exclude_list = [CreatorProfile.embedding, CreatorProfile.password_hash]   # the detail page lists every column unless told otherwise
    can_export = False    # would export every creator's email
    can_delete = False
    column_searchable_list = [CreatorProfile.email, CreatorProfile.full_name, CreatorProfile.creator_handle, CreatorProfile.content_niche]
    column_sortable_list    = [c.name for c in CreatorProfile.__table__.columns if c.name not in ("embedding", "password_hash")]
    column_default_sort    = [(CreatorProfile.id, True)]
    form_excluded_columns = [
        CreatorProfile.embedding,
        CreatorProfile.excluded_categories,
        CreatorProfile.content_tags,
        CreatorProfile.instagram_sub_niches,
        CreatorProfile.instagram_excluded_categories,
        CreatorProfile.youtube_sub_niches,
        CreatorProfile.youtube_excluded_categories,
        CreatorProfile.token_version,   # managed by the code below, not typed in
    ]
    form_overrides   = {"password_hash": wtforms.PasswordField}
    form_labels      = {"password_hash": "Password"}
    form_widget_args = {"password_hash": {"placeholder": "Leave blank to keep the current password"}}
    page_size = 15

    async def on_model_change(self, data: dict, model, is_created: bool, request) -> None:
        plaintext = (data.get("password_hash") or "").strip()
        revoke_sessions = False
        if plaintext:
            data["password_hash"] = hash_password(plaintext)   # a ValueError (too long for bcrypt) is shown by SQLAdmin as the form error
            revoke_sessions = True
        elif is_created:
            raise ValueError("Password is required when creating a creator profile")
        else:
            # Blank on edit — leave the existing hash untouched.
            data.pop("password_hash", None)
        if "is_active" in data and not is_created and bool(data["is_active"]) != bool(model.is_active):
            revoke_sessions = True   # deactivated or reactivated: tokens issued before must not come back to life
        if revoke_sessions and not is_created:
            data["token_version"] = int(model.token_version or 0) + 1   # every login token issued so far stops working

def _admin_fingerprint() -> str:
    """HMAC(ADMIN_SESSION_SECRET, ADMIN_PASSKEY): stored in the admin session, compared on every request; the passkey itself is never stored."""
    return hmac.new(config.ADMIN_SESSION_SECRET.encode("utf-8"), (passkey() or "").encode("utf-8"), hashlib.sha256).hexdigest()


ADMIN_SESSION_SECONDS = 8 * 60 * 60


class AdminAuth(AuthenticationBackend):
    """
    Gates /admin behind a single shared passkey (config.ADMIN_PASSKEY) —
    not per-person accounts, just one secret set in .env. The built-in
    sqladmin login form has "Username" + "Password" fields; there's no
    custom template here, so Username is unused (leave it blank) and the
    passkey goes in the Password field.

    Fails closed (see api/operator.py): without a configured passkey no login
    succeeds, and a session only counts while a passkey is still configured.
    """
    async def login(self, request: Request) -> bool:
        attempt_limits.check("admin", request)   # HTTP 429 while this client is locked out (5 failures in 15 minutes)
        form = await request.form()
        if not passkey_matches(str(form.get("password") or "")):   # constant time; false when no passkey is configured
            attempt_limits.failed("admin", request)
            return False
        attempt_limits.succeeded("admin", request)
        request.session["admin_authenticated"] = True
        request.session["admin_fp"] = _admin_fingerprint()
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        # The session only counts while it was made with the CURRENT passkey and session secret: changing either ends every admin session.
        return (bool(request.session.get("admin_authenticated")) and operator_configured()
                and hmac.compare_digest(str(request.session.get("admin_fp") or ""), _admin_fingerprint()))

# tables

# /admin fails closed. With ADMIN_PASSKEY set (and not blank) the login form takes it. Without it the backend is still attached, but
# nothing can log in, so /admin is unreachable. The only way to leave it open is ADMIN_ALLOW_UNAUTHENTICATED=1 for local development,
# which is ignored when ENVIRONMENT=production (api/operator.py).
def _admin_authentication_backend():
    backend = AdminAuth(secret_key=ADMIN_SESSION_SECRET, https_only=IS_PRODUCTION, max_age=ADMIN_SESSION_SECONDS)
    if operator_configured():
        if len(passkey()) < MIN_RECOMMENDED_LENGTH:
            logger.warning("ADMIN_PASSKEY is shorter than %d characters; use a long random value", MIN_RECOMMENDED_LENGTH)
        return backend
    if allow_unauthenticated_admin():
        logger.warning("ADMIN_PASSKEY is not set and ADMIN_ALLOW_UNAUTHENTICATED is on: /admin is OPEN (development only)")
        return None
    logger.error("ADMIN_PASSKEY is not set (or is blank): /admin and the operator endpoints are closed until it is configured")
    return backend


admin = Admin(
    app, engine,
    authentication_backend=_admin_authentication_backend(),
)
admin.add_view(CreatorProfileAdmin)
admin.add_view(BrandProfileAdmin)
admin.add_view(BrandContactAdmin)
admin.add_view(InitialBrandScoreAdmin)
admin.add_view(BrandRawAdmin)
admin.add_view(BrandNicheAdmin)
admin.add_view(MetaAdAdmin)
admin.add_view(YoutubeSponsorshipAdmin)
admin.add_view(InstagramPostAdmin)
admin.add_view(TestBrandsWithInstagramPostsAdmin)
admin.add_view(TestNicheAdmin)
admin.add_view(TestCreatorBrandPartnershipPostAdmin)
admin.add_view(FacebookBrandedContentCheckAdmin)
admin.add_view(CreatorNicheAdmin)
admin.add_view(CreatorInstagramPostAdmin)
admin.add_view(LlmUsageAdmin)
admin.add_view(CreatorLlmCostAdmin)
admin.add_view(InstagramUserAdmin)
admin.add_view(ContentCreatorREAdmin)
admin.add_view(BrandInstagramUserAdmin)
admin.add_view(InstagramCreatorCommenterAdmin)
admin.add_view(PromptAdmin)
admin.add_view(SavedBrandAdmin)
admin.add_view(PitchAdmin)
admin.add_view(OwnBrandJobAdmin)
admin.add_view(RateEstimateAdmin)
admin.add_view(EarlyAccessSignupAdmin)
admin.add_view(ContractReviewAdmin)

# 
# API models
# 

Text200 = Annotated[str, Field(max_length=200)]


class SeedRequest(BaseModel):
    niche:          Text200
    limit:          int | None  = Field(default=None, ge=1, le=5000)
    country:        Text200 | None  = None
    headquarters:   Text200 | None  = None
    location:       Text200 | None  = None
    operating_area: Text200 | None  = None
    niche_label:    Text200 | None  = None


class EnrichRequest(BaseModel):
    niche:           Text200 | None  = None
    limit_per_step:  int         = Field(default=300, ge=1, le=5000)
    steps:           list[Literal["wikidata_socials", "shopify", "tranco", "meta_ads", "youtube"]] | None = Field(default=None, max_length=5)  # None = all steps


# 
# Background job store (in-memory; single-process)
# 

_jobs: dict[str, dict] = {}


BATCH_BUSY_MESSAGE = "Another job is running, retry shortly."   # the batch pool (2 connections) had none free within pool_timeout


def _fail_row_via_api_pool(creator_id: int, status_field: str, error_field: str, message: str, job_field: str | None = None, job_id: str | None = None) -> None:
    """Record a failure when the batch pool had no connection (so the job's own session cannot be used): a short session on the API pool."""
    with SessionLocal() as db:
        row = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
        if row and (job_field is None or getattr(row, job_field) == job_id):
            setattr(row, status_field, "failed")
            setattr(row, error_field, message)
            db.commit()


def _safe_error(exc: Exception) -> str:
    """Text that may be stored or returned to clients: never raw database error text (it carries SQL and parameters)."""
    if isinstance(exc, PoolTimeoutError):
        return BATCH_BUSY_MESSAGE
    if isinstance(exc, DBAPIError) and getattr(exc.orig, "pgcode", None) == _QUERY_CANCELED:
        return "database timeout"
    return type(exc).__name__


def _run_seed_job(job_id: str, body: SeedRequest) -> None:
    db = BatchSessionLocal()
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
        _jobs[job_id].update({"status": "error", "error": _safe_error(exc)})
    finally:
        db.close()


def _run_enrich_job(job_id: str, body: EnrichRequest) -> None:
    db = BatchSessionLocal()
    try:
        results = run_signal_enrichment(
            db,
            niche=body.niche,
            limit_per_step=body.limit_per_step,
            steps=body.steps,
        )
        _jobs[job_id].update({"status": "done", "results": results})
    except Exception as exc:
        _jobs[job_id].update({"status": "error", "error": _safe_error(exc)})
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

_PROTECTED_PAGE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate"}


@app.get("/", include_in_schema=False)
def frontend(current_user: CreatorProfile | None = Depends(get_current_user_optional)):
    if not current_user:
        return RedirectResponse(url="/signin?return_to=/", status_code=307)
    if not is_profile_complete(current_user):
        return RedirectResponse(url="/creator-profile?onboarding=required", status_code=307)
    return FileResponse("frontend/index.html", headers=_PROTECTED_PAGE_HEADERS)


@app.get("/signin", include_in_schema=False)
def signin_page():
    # no-store: this page's markup/JS changed shape (Google button -> email/
    # password form) during development — a stale cached copy would silently
    # keep pointing at the removed /auth/google route. Auth pages shouldn't
    # ever serve from cache regardless.
    return FileResponse("frontend/login.html", headers={"Cache-Control": "no-store"})


@app.get("/login", include_in_schema=False)
def login_redirect():
    return RedirectResponse(url="/signin", status_code=301)


@app.get("/dashboard", include_in_schema=False)
def dashboard_page(current_user: CreatorProfile | None = Depends(get_current_user_optional)):
    if not current_user:
        return RedirectResponse(url="/signin?return_to=/dashboard", status_code=307)
    if not is_profile_complete(current_user):
        return RedirectResponse(url="/creator-profile?onboarding=required", status_code=307)
    return FileResponse("frontend/index.html", headers=_PROTECTED_PAGE_HEADERS)


@app.get("/creator-profile", include_in_schema=False)
def creator_profile_page(current_user: CreatorProfile | None = Depends(get_current_user_optional)):
    if not current_user:
        return RedirectResponse(url="/signin?return_to=/creator-profile", status_code=307)
    return FileResponse("frontend/creator-profile.html", headers=_PROTECTED_PAGE_HEADERS)


@app.get("/matches", include_in_schema=False)
def matches_page(current_user: CreatorProfile | None = Depends(get_current_user_optional)):
    if not current_user:
        return RedirectResponse(url="/signin?return_to=/matches", status_code=307)
    if not is_profile_complete(current_user):
        return RedirectResponse(url="/creator-profile?onboarding=required", status_code=307)
    return FileResponse("frontend/matches.html", headers=_PROTECTED_PAGE_HEADERS)


@app.get("/pitch", include_in_schema=False)
def pitch_page(current_user: CreatorProfile | None = Depends(get_current_user_optional)):
    if not current_user:
        return RedirectResponse(url="/signin?return_to=/pitch", status_code=307)
    if not is_profile_complete(current_user):
        return RedirectResponse(url="/creator-profile?onboarding=required", status_code=307)
    return FileResponse("frontend/pitch.html", headers=_PROTECTED_PAGE_HEADERS)


@app.get("/add-creators", include_in_schema=False)
def add_creators_page(current_user: CreatorProfile | None = Depends(get_current_user_optional)):
    if not current_user:
        return RedirectResponse(url="/signin?return_to=/add-creators", status_code=307)
    if not is_profile_complete(current_user):
        return RedirectResponse(url="/creator-profile?onboarding=required", status_code=307)
    return FileResponse("frontend/add-creators.html", headers=_PROTECTED_PAGE_HEADERS)


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


class BulkAddCreatorsRequest(BaseModel):
    urls:  list[Annotated[str, Field(max_length=300)]] = Field(max_length=200)
    niche: Text200


class BulkAddCreatorsResponse(BaseModel):
    added:   int
    skipped: int
    total:   int

#
# Endpoints
#

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/db")
def health_db(db: Session = Depends(get_db)):
    """
    Readiness: a cheap read of two tables through the API's own database role, so a missing grant or a wrong DATABASE_URL shows up
    right after a deploy (deploy.sh probes it). Returns no data; a failure is the generic 503 / 500 of the database error handler.
    """
    db.execute(text("SELECT 1 FROM creator_profiles LIMIT 1"))
    db.execute(text("SELECT 1 FROM brands_raw LIMIT 1"))
    return {"status": "ok"}


@app.post("/content-creator-re/bulk-add", response_model=BulkAddCreatorsResponse, dependencies=[Depends(require_operator)])
def bulk_add_content_creators(body: BulkAddCreatorsRequest):
    """
    Bulk-insert content_creator_re rows from pasted Instagram URLs, one
    niche for the whole batch. Synchronous (no Apify/LLM calls — just URL
    parsing + DB inserts) so no background job/polling needed, unlike
    /seed. Skips a URL if its extracted username already exists.
    """
    urls = [u.strip() for u in body.urls if u.strip()]
    added, skipped = add_content_creator_re(urls, body.niche)
    return BulkAddCreatorsResponse(added=added, skipped=skipped, total=len(urls))


@app.post("/seed", response_model=SeedJobResponse, dependencies=[Depends(require_operator)])
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


@app.get("/seed/status/{job_id}", response_model=SeedStatusResponse, dependencies=[Depends(require_operator)])
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


@app.post("/enrich/signals", response_model=EnrichJobResponse, dependencies=[Depends(require_operator)])
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


@app.get("/enrich/signals/status/{job_id}", response_model=EnrichStatusResponse, dependencies=[Depends(require_operator)])
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
    limit: int = Field(default=500, ge=1, le=5000)


class ScoreJobResponse(BaseModel):
    job_id:  str
    status:  str
    message: str


class ScoreStatusResponse(BaseModel):
    status:  str
    scored:  int | None = None
    error:   str | None = None



def _run_scoring_job(job_id: str, limit: int) -> None:
    db = BatchSessionLocal()
    try:
        scored = run_brand_scoring(db, limit=limit)
        _jobs[job_id].update({"status": "done", "scored": scored})
    except Exception as exc:
        _jobs[job_id].update({"status": "error", "error": _safe_error(exc)})
    finally:
        db.close()


@app.post("/score/brands", response_model=ScoreJobResponse, dependencies=[Depends(require_operator)])
def score_brands(body: ScoreRequest, background_tasks: BackgroundTasks):
    """
    Score all brands that have enrichment data. Results are written to initial_brand_score.
    Poll GET /score/brands/status/{job_id} to track progress.
    """
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "scored": 0}
    background_tasks.add_task(_run_scoring_job, job_id, body.limit)
    return ScoreJobResponse(job_id=job_id, status="running", message="Brand scoring started")


@app.get("/score/brands/status/{job_id}", response_model=ScoreStatusResponse, dependencies=[Depends(require_operator)])
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

Text100 = Annotated[str, Field(max_length=100)]
Count = Annotated[int, Field(ge=0, le=10**10)]
Description = Annotated[str, Field(max_length=4000)]
DescriptionMode = Literal["generate", "manual", "write"]   # "write" is the older static page's name for "manual"
TextList = Annotated[list[Text100], Field(max_length=30)]


class CreatorProfileRequest(BaseModel):
    full_name:        str = Field(max_length=150)
    creator_handle:   str = Field(max_length=100)
    age:              int | None = Field(default=None, ge=13, le=100)
    gender:           str | None = Field(default=None, max_length=50)

    content_niche:       Text200 | None = None
    excluded_categories:  TextList | None = None
    # Free-text brand preferences (wanted / unwanted brand types) for the
    # Matches v3 LLM ranking. Stored trimmed; empty becomes NULL.
    brand_preferences:    str | None = Field(default=None, max_length=1000)
    pitch_style_preferences: str | None = Field(default=None, max_length=1000)

    instagram_handle: Text100 | None = None
    instagram_followers: Count | None = None
    instagram_highest_views: int | None = Field(default=None, ge=0, le=10**10)

    primary_platform: str | None = Field(default=None, max_length=100)
    follower_count:   Count | None = None

    instagram_primary_niche: Text200 | None = None
    instagram_sub_niches: TextList | None = None
    instagram_excluded_categories: TextList | None = None
    instagram_description: Description | None = None
    instagram_description_mode: DescriptionMode | None = None
    instagram_audience_gender: Literal["male", "female", "both"] | None = None
    instagram_audience_description: str | None = Field(default=None, max_length=1500)
    youtube_channel_name: Text200 | None = None
    youtube_subscribers: Count | None = None
    youtube_primary_niche: Text200 | None = None
    youtube_sub_niches: TextList | None = None
    youtube_excluded_categories: TextList | None = None
    youtube_description: Description | None = None
    youtube_description_mode: DescriptionMode | None = None
    youtube_audience_gender: Literal["male", "female", "both"] | None = None
    youtube_audience_description: str | None = Field(default=None, max_length=1500)


class CreatorDescriptionRequest(BaseModel):
    platform: Literal["instagram", "youtube"]
    username: str = Field(max_length=100)
    niche: Text200


class CreatorDescriptionResponse(BaseModel):
    platform: str
    status: str
    job_id: str
    description: str | None = None
    posts_analyzed: int | None = None
    videos_analyzed: int | None = None
    bio_found: bool = False
    error: str | None = None



@app.get("/brand-niches")
def list_brand_niches():
    """
    Distinct niche values supplied through content_creator_re, for the
    creator-profile form's Primary niche multi-select.
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(ContentCreatorRE.niche)
            .filter(ContentCreatorRE.niche.isnot(None), ContentCreatorRE.niche != "")
            .distinct()
            .all()
        )
        niches = sorted({row[0].strip() for row in rows if row[0] and row[0].strip()}, key=str.casefold)
        return {"niches": niches}
    finally:
        db.close()


@app.get("/brand-category-niches")
def list_brand_category_niches():
    """Distinct brand category values used by the creator avoid fields."""
    db = SessionLocal()
    try:
        rows = (
            db.query(BrandNiche.niche)
            .filter(BrandNiche.niche.isnot(None), BrandNiche.niche != "")
            .distinct()
            .all()
        )
        niches = sorted({row[0].strip() for row in rows if row[0] and row[0].strip()}, key=str.casefold)
        return {"niches": niches}
    finally:
        db.close()


@app.get("/brand-category-tags")
def list_brand_category_tags(niche: Annotated[list[Annotated[str, Field(max_length=100)]], Query(max_length=20)] = []):
    """
    Options for the creator-profile "Brand categories to avoid" fields:
    distinct brands_niches.tags of brands in the requested primary niche(s),
    e.g. /brand-category-tags?niche=Music&niche=Beauty.

    A niche matches any brands_niches.niche that contains it
    (case-insensitive), so "Health" also covers "Health & Wellness".
    Tags are deduped case-insensitively. Returns {"tags_by_niche": {niche:
    [tags...]}} keyed by each requested niche exactly as passed. Selecting
    a tag excludes every brand carrying it (matcher.py hard filter).
    """
    requested = [n.strip() for n in niche if n and n.strip()]
    if not requested:
        return {"tags_by_niche": {}}

    db = SessionLocal()
    try:
        tags_by_niche: dict[str, list[str]] = {}
        for name in requested:
            rows = db.execute(text("""
                SELECT DISTINCT trim(tag)
                FROM brands_niches,
                     jsonb_array_elements_text(tags) AS tag
                WHERE jsonb_typeof(tags) = 'array'
                  AND niche ILIKE :pattern ESCAPE '\\'
                  AND trim(tag) <> ''
            """), {"pattern": f"%{escape_like(name)}%"}).fetchall()
            seen: dict[str, str] = {}
            for (tag,) in rows:
                seen.setdefault(tag.casefold(), tag)
            tags_by_niche[name] = sorted(seen.values(), key=str.casefold)
        return {"tags_by_niche": tags_by_niche}
    finally:
        db.close()


@app.get("/creator-sub-niches")
def list_creator_sub_niches():
    """Distinct brand tag values used as searchable creator sub-niches."""
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT DISTINCT tag
            FROM brands_niches,
                 jsonb_array_elements_text(tags) AS tag
            WHERE jsonb_typeof(tags) = 'array'
              AND tag <> ''
        """)).fetchall()
        niches = sorted({row[0].strip() for row in rows if row[0] and row[0].strip()}, key=str.casefold)
        return {"niches": niches}
    finally:
        db.close()


@app.get("/brand-niche-tags")
def list_brand_niche_tags():
    """
    Sub-niche/category tags from brands_niches.tags — LLM-extracted in
    shopify_detect.py from each brand's scraped about-page description
    (e.g. ["sustainable clothing", "streetwear"]), grouped by the brand's
    primary niche. Returned both as a flat distinct list and as a
    tags-by-niche map so the creator-profile form can filter the Sub-niches
    multi-select based on the primary niche(s) the creator has chosen.

    A brand matches in the Stage 3 hard filter (matcher.py) when the
    creator's sub_niches overlap with that brand's brands_niches.tags.
    """
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT niche, jsonb_array_elements_text(tags) AS tag
            FROM brands_niches
            WHERE jsonb_typeof(tags) = 'array'
        """)).fetchall()

        tags_by_niche: dict[str, set[str]] = {}
        all_tags: set[str] = set()
        for niche, tag in rows:
            if not niche or not tag:
                continue
            all_tags.add(tag)
            tags_by_niche.setdefault(niche, set()).add(tag)

        tags = sorted(all_tags, key=str.lower)
        tags_by_niche_sorted = {
            niche: sorted(tags_by_niche[niche], key=str.lower)
            for niche in sorted(tags_by_niche, key=str.lower)
        }
        return {"tags": tags, "tags_by_niche": tags_by_niche_sorted}
    finally:
        db.close()


@app.get("/creator-profile/me", response_model=CreatorProfileResponse)
def get_my_creator_profile(current_user: CreatorProfile = Depends(get_current_user)):
    """Return the logged-in user's creator profile."""
    return _profile_to_response(current_user)


def _run_creator_description_job(
    creator_id: int,
    platform: str,
    username: str,
    niche: str,
    job_id: str,
) -> None:
    db = BatchSessionLocal()
    status_field = f"{platform}_description_status"
    job_field = f"{platform}_description_job_id"
    description_field = f"{platform}_description"
    error_field = f"{platform}_description_error"
    try:
        row = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
        if not row or getattr(row, job_field) != job_id:
            return

        setattr(row, status_field, "running")
        db.commit()

        with llm_usage_scope(creator_id, "description"):
            if platform == "instagram":
                result = generate_instagram_description(
                    db, username, niche, creator_id=creator_id, previous_handle=row.instagram_handle,   # old username: its posts are emptied
                )
            else:
                result = generate_youtube_description(db, username, niche)

        row = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
        if not row or getattr(row, job_field) != job_id:
            return
        setattr(row, description_field, result["description"])
        # Audience gender / description (the description states the age range) generated together with the description.
        for key in ("audience_gender", "audience_description"):
            if result.get(key) is not None:
                setattr(row, f"{platform}_{key}", result[key])
        # The follower count Apify read from the profile replaces the typed one.
        if platform == "instagram" and result.get("followers_count") is not None:
            row.instagram_followers = result["followers_count"]
            if (row.primary_platform or "").split(",")[0].strip().lower() == "instagram":
                row.follower_count = result["followers_count"]
        # The most views on any of the scraped reels / video posts (kept as typed when none of them has views).
        if platform == "instagram" and result.get("highest_views") is not None:
            row.instagram_highest_views = result["highest_views"]
        # 3-4 sub-niches picked by the LLM; the creator's own choice is kept if they already made one.
        if result.get("sub_niches") and not getattr(row, f"{platform}_sub_niches"):
            setattr(row, f"{platform}_sub_niches", result["sub_niches"])
        setattr(row, status_field, "completed")
        setattr(row, error_field, None)
        db.commit()
    except PoolTimeoutError:
        logger.error("Creator %s description job: no batch connection free for creator_id=%d", platform, creator_id)
        _fail_row_via_api_pool(creator_id, status_field, error_field, BATCH_BUSY_MESSAGE, job_field, job_id)
    except Exception as exc:
        db.rollback()
        logger.exception("Creator %s description job failed for creator_id=%d", platform, creator_id)
        row = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
        if row and getattr(row, job_field) == job_id:
            setattr(row, status_field, "failed")
            setattr(row, error_field, _safe_error(exc))
            db.commit()
    finally:
        db.close()


@app.post("/creator-profile/me/generate-description", response_model=CreatorDescriptionResponse)
def generate_my_creator_description(
    body: CreatorDescriptionRequest,
    background_tasks: BackgroundTasks,
    current_user: CreatorProfile = Depends(get_current_user),
):
    """Queue one platform description generation job and return immediately."""
    platform = body.platform.strip().lower()
    db = SessionLocal()
    try:
        if platform not in {"instagram", "youtube"}:
            raise HTTPException(status_code=400, detail="Platform must be instagram or youtube")
        if not body.username.strip():
            raise HTTPException(status_code=400, detail=f"{platform.title()} username is required")
        if not body.niche.strip():
            raise HTTPException(status_code=400, detail=f"{platform.title()} niche is required")

        status_field = f"{platform}_description_status"
        job_field = f"{platform}_description_job_id"
        description_field = f"{platform}_description"
        error_field = f"{platform}_description_error"
        row = db.query(CreatorProfile).filter(CreatorProfile.id == current_user.id).first()
        current_status = getattr(row, status_field) or "idle"
        current_job_id = getattr(row, job_field)
        if current_status in {"queued", "running"} and current_job_id:
            return CreatorDescriptionResponse(
                platform=platform,
                status=current_status,
                job_id=current_job_id,
                description=getattr(row, description_field),
                error=getattr(row, error_field),
            )

        usage_limits.consume(db, current_user.id, "description_generation")   # 429 over the daily cap, before the Apify / LLM job is queued
        job_id = str(uuid.uuid4())
        setattr(row, status_field, "queued")
        setattr(row, job_field, job_id)
        setattr(row, error_field, None)
        db.commit()
        background_tasks.add_task(
            _run_creator_description_job,
            current_user.id,
            platform,
            body.username.strip(),
            body.niche.strip(),
            job_id,
        )
        return CreatorDescriptionResponse(
            platform=platform,
            status="queued",
            job_id=job_id,
            description=getattr(row, description_field),
        )
    finally:
        db.close()


# AI ranking versions: v2 (profile fit) and v3 (profile fit + taglines).
# Each has its own ranking function, results table and status columns
# (<prefix>_ranking_status / <prefix>_ranking_error / <prefix>_ranked_at).
# "paused": True skips the ranking run (no LLM calls) — profile saves and
# /refresh leave the status idle and GET keeps serving the last stored results.
_AI_RANKINGS = {
    # v2 temporarily paused to save LLM credits — set paused to False to resume.
    "v2": {"rank": lambda db, cid: rank_brands_with_llm(db, cid),    "model": CreatorBrandLlmRanking,   "prefix": "llm",    "paused": True},
    "v3": {"rank": lambda db, cid: rank_brands_with_llm_v3(db, cid), "model": CreatorBrandLlmV3Ranking, "prefix": "llm_v3", "paused": False,
           "min_confidence": MIN_MATCH_CONFIDENCE},
}


def _run_ai_ranking_job(creator_id: int, version: str) -> None:
    """Run one AI ranking version for a creator, tracking its status columns."""
    config = _AI_RANKINGS[version]
    status_field = f"{config['prefix']}_ranking_status"
    error_field = f"{config['prefix']}_ranking_error"
    ranked_at_field = f"{config['prefix']}_ranked_at"
    db = BatchSessionLocal()
    try:
        row = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
        if not row:
            return
        if config["paused"]:
            logger.info("AI ranking %s is paused — skipping creator_id=%d", version, creator_id)
            setattr(row, status_field, "idle")
            db.commit()
            return
        setattr(row, status_field, "running")
        setattr(row, error_field, None)
        db.commit()

        with llm_usage_scope(creator_id, f"match_{version}"):
            config["rank"](db, creator_id)

        row = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
        setattr(row, status_field, "completed")
        setattr(row, ranked_at_field, datetime.now(timezone.utc))
        db.commit()
    except PoolTimeoutError:
        logger.error("AI ranking %s: no batch connection free for creator_id=%d", version, creator_id)
        _fail_row_via_api_pool(creator_id, status_field, error_field, BATCH_BUSY_MESSAGE)
    except Exception as exc:
        db.rollback()
        logger.exception("AI ranking %s background job failed for creator_id=%d", version, creator_id)
        row = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
        if row:
            setattr(row, status_field, "failed")
            setattr(row, error_field, _safe_error(exc))
            db.commit()
    finally:
        db.close()


def _run_llm_ranking_job(creator_id: int) -> None:
    """Matches v2: rank the creator's hard-filtered brands with the LLM."""
    _run_ai_ranking_job(creator_id, "v2")


def _run_llm_v3_ranking_job(creator_id: int) -> None:
    """Matches v3: rank with profile fit + "why it's a match" taglines."""
    _run_ai_ranking_job(creator_id, "v3")


def _run_creator_signals_job(creator_id: int) -> None:
    db = SessionLocal()
    try:
        with llm_usage_scope(creator_id, "profile_signals"):
            compute_creator_signals(db, creator_id)
    except Exception:
        logger.exception("Creator signals background job failed for creator_id=%d", creator_id)
    finally:
        db.close()
    # After signals, so the LLM sees fresh content_tags / embedding_text and
    # the hard filter's embedding ordering is up to date. v2, then v3.
    _run_llm_ranking_job(creator_id)
    _run_llm_v3_ranking_job(creator_id)


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
        payload = body.model_dump()
        payload["brand_preferences"] = (payload.get("brand_preferences") or "").strip() or None
        payload["pitch_style_preferences"] = (payload.get("pitch_style_preferences") or "").strip() or None
        if not is_profile_complete(payload):
            raise HTTPException(
                status_code=422,
                detail="Complete the required onboarding fields before continuing: full name, age, gender, primary platforms, Instagram username, follower count, primary niche, and at least one creator description.",
            )
        row = db.query(CreatorProfile).filter(CreatorProfile.id == current_user.id).first()
        if (row.instagram_description_status or "idle") in {"queued", "running"} or (row.youtube_description_status or "idle") in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="A description is still being generated. Wait for it to finish before saving your profile.")
        previous_handle = row.instagram_handle
        for field, value in payload.items():
            setattr(row, field, value)
        row.creator_tier = bucket_creator_tier(row.follower_count)
        if not _AI_RANKINGS["v2"]["paused"]:
            row.llm_ranking_status = "queued"     # signals job runs the v2 LLM ranking next,
            row.llm_ranking_error = None
        row.llm_v3_ranking_status = "queued"  # then v3
        row.llm_v3_ranking_error = None

        db.commit()
        db.refresh(row)
        # A changed Instagram username: the old username's saved posts are emptied (unless generating the description already
        # saved the new username's posts); they are filled again the next time the description is generated.
        clear_posts_if_username_changed(db, row.id, previous_handle, row.instagram_handle)
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
    website:      str | None = None   # brands_raw.website
    niche:        str | None = None
    latest_product: str | None = None   # brands_raw.latest_product (the pitch form pre-fills its product-launch field with it)
    total_score:  float
    dimensions:   dict[str, MatchDimension]
    reasons:      list[str]


class MatchesResponse(BaseModel):
    matches:     list[MatchResult]
    total:       int
    cached:      bool
    computed_at: str


@app.get("/matches/me", response_model=MatchesResponse)
def get_my_matches(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    """
    Ranked brand matches for the logged-in creator (Stage 3). Always
    computes live — no match_cache layer exists yet (deferred by design,
    per the matching doc), so `cached` is always false; the field is kept
    in the response so the contract doesn't change once caching is added.
    """
    db = SessionLocal()
    try:
        matches, total = get_matches(db, current_user.id, limit=limit, offset=offset, include_total=True)
        return MatchesResponse(matches=matches, total=total, cached=False, computed_at=datetime.now(timezone.utc).isoformat())
    finally:
        db.close()


@app.post("/matches/me/refresh", response_model=MatchesResponse)
def refresh_my_matches(
    limit: int = Query(20, ge=1, le=100),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    """
    Force-recompute matches, bypassing any cache. There's no cache to
    bypass yet, so this currently behaves identically to GET /matches/me —
    the separate endpoint is kept for when caching is added.
    """
    db = SessionLocal()
    try:
        matches, total = get_matches(db, current_user.id, limit=limit, offset=0, include_total=True)
        return MatchesResponse(matches=matches, total=total, cached=False, computed_at=datetime.now(timezone.utc).isoformat())
    finally:
        db.close()


class LlmMatchResult(BaseModel):
    brand_raw_id: int
    brand_name:   str
    website:      str | None = None   # brands_raw.website
    niche:        str | None = None
    latest_product: str | None = None   # brands_raw.latest_product (the pitch form pre-fills its product-launch field with it)
    rank:         int
    confidence:   int
    reason:       str | None = None         # the LLM's one-line fit explanation
    reasons:      list[str] = []            # "why it's a match" taglines, same as v1


class LlmMatchesResponse(BaseModel):
    status:    str                   # idle | queued | running | completed | failed
    error:     str | None = None
    ranked_at: str | None = None
    total:     int
    matches:   list[LlmMatchResult]


def _ai_matches_response(db, user_id: int, version: str) -> LlmMatchesResponse:
    config = _AI_RANKINGS[version]
    ranking_model = config["model"]
    row = db.query(CreatorProfile).filter(CreatorProfile.id == user_id).first()
    # Stored rankings are re-checked against the brand-only hard filters (geo,
    # referral, partnership, avoided niches) so a newly blocked brand disappears
    # at once, without waiting for the creator's next re-rank.
    query = apply_brand_hard_filters(
        db.query(ranking_model, BrandRaw)
        .join(BrandRaw, BrandRaw.id == ranking_model.brand_raw_id)
        .filter(ranking_model.creator_profile_id == user_id)
    )
    min_confidence = config.get("min_confidence")
    if min_confidence is not None:   # v3: weaker fits than the threshold are not shown as matches
        query = query.filter(ranking_model.confidence >= min_confidence)
    rows = query.order_by(ranking_model.rank).all()
    # Taglines are stored at ranking time; rows ranked before that existed
    # (reasons NULL) get them built live until the next re-rank.
    missing = [brand.id for ranking, brand in rows if ranking.reasons is None]
    live_reasons: dict[int, list[str]] = {}
    if missing:
        profiles = {p.brand_raw_id: p for p in db.query(BrandProfile).filter(BrandProfile.brand_raw_id.in_(missing))}
        for ranking, brand in rows:
            if ranking.reasons is None:
                live_reasons[brand.id] = generate_match_reasons(row, brand, profiles.get(brand.id), db=db)

    matches = [
        LlmMatchResult(
            brand_raw_id=brand.id,
            brand_name=brand.name or f"brand {brand.id}",
            website=brand.website,
            niche=brand.niche,
            latest_product=brand.latest_product,
            rank=ranking.rank,
            confidence=ranking.confidence,
            reason=ranking.reason,
            reasons=ranking.reasons if ranking.reasons is not None else live_reasons.get(brand.id, []),
        )
        for ranking, brand in rows
    ]
    ranked_at = getattr(row, f"{config['prefix']}_ranked_at")
    return LlmMatchesResponse(
        status=getattr(row, f"{config['prefix']}_ranking_status") or "idle",
        error=getattr(row, f"{config['prefix']}_ranking_error"),
        ranked_at=ranked_at.isoformat() if ranked_at else None,
        total=len(matches),
        matches=matches,
    )


def _refresh_ai_matches(background_tasks: BackgroundTasks, user_id: int, version: str) -> LlmMatchesResponse:
    """Queue a new ranking run for `version` (no-op if one is already queued/running)."""
    prefix = _AI_RANKINGS[version]["prefix"]
    db = SessionLocal()
    try:
        row = db.query(CreatorProfile).filter(CreatorProfile.id == user_id).first()
        if _AI_RANKINGS[version]["paused"]:
            return _ai_matches_response(db, user_id, version)
        if getattr(row, f"{prefix}_ranking_status") not in ("queued", "running"):
            usage_limits.consume(db, user_id, "match_refresh")   # 429 over the daily cap, before the LLM ranking is queued
            setattr(row, f"{prefix}_ranking_status", "queued")
            setattr(row, f"{prefix}_ranking_error", None)
            db.commit()
            background_tasks.add_task(_run_ai_ranking_job, user_id, version)
        return _ai_matches_response(db, user_id, version)
    finally:
        db.close()


@app.get("/matches/me/v2", response_model=LlmMatchesResponse)
def get_my_llm_matches(current_user: CreatorProfile = Depends(get_completed_user)):
    """
    Matches v2 — the logged-in creator's brands as ranked by the LLM
    (pipeline/matching/llm_ranking_v2.py) from the last ranking run, best
    first, plus that run's status. Ranking runs in the background after
    every profile save, or on POST /matches/me/v2/refresh.
    """
    db = SessionLocal()
    try:
        return _ai_matches_response(db, current_user.id, "v2")
    finally:
        db.close()


@app.post("/matches/me/v2/refresh", response_model=LlmMatchesResponse)
def refresh_my_llm_matches(
    background_tasks: BackgroundTasks,
    current_user: CreatorProfile = Depends(get_completed_user),
):
    """Queue a new v2 LLM ranking run (no-op if one is already queued/running)."""
    return _refresh_ai_matches(background_tasks, current_user.id, "v2")


@app.get("/matches/me/v3", response_model=LlmMatchesResponse)
def get_my_llm_v3_matches(current_user: CreatorProfile = Depends(get_completed_user)):
    """
    Matches v3 — brands ranked by the LLM from profile fit (65%) AND each
    brand's "why it's a match" taglines (35%)
    (pipeline/matching/llm_ranking_v3.py). Same response shape as v2.
    Runs in the background after every profile save (after v2), or on
    POST /matches/me/v3/refresh.
    """
    db = SessionLocal()
    try:
        return _ai_matches_response(db, current_user.id, "v3")
    finally:
        db.close()


@app.post("/matches/me/v3/refresh", response_model=LlmMatchesResponse)
def refresh_my_llm_v3_matches(
    background_tasks: BackgroundTasks,
    current_user: CreatorProfile = Depends(get_completed_user),
):
    """Queue a new v3 LLM ranking run (no-op if one is already queued/running)."""
    return _refresh_ai_matches(background_tasks, current_user.id, "v3")


class PartnershipPost(BaseModel):
    source:                 str   # "instagram" (instagram_posts) or "reverse_engineering" (test_creator_brand_partnership_posts)
    post_url:               str
    post_date:              str | None = None   # YYYY-MM-DD
    sponsorship_confidence: int
    creators:               list[str]


class BrandPartnershipPostsResponse(BaseModel):
    brand_raw_id: int
    brand_name:   str | None = None
    posts:        list[PartnershipPost]


_PARTNERSHIP_MIN_CONFIDENCE = 90           # test_creator_brand_partnership_posts (reverse engineering)
_INSTAGRAM_PARTNERSHIP_MIN_CONFIDENCE = 90 # instagram_posts (direct brand scrape): same bar as the reverse-engineering posts, the pitch and rate intelligence


def _post_date(timestamp: str | None) -> str | None:
    """YYYY-MM-DD from an ISO-8601 post timestamp (None if missing/unparseable)."""
    try:
        return datetime.strptime((timestamp or "")[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _instagram_post_creators(post: InstagramPost) -> list[str]:
    creators: list[str] = []
    for field in ("coauthor_producers", "tagged_users", "sponsors", "mentions"):
        value = getattr(post, field)
        values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
        for item in values:
            username = str(item).strip().lstrip("@")
            if username and username.lower() not in {c.lower() for c in creators}:
                creators.append(username)
    return creators


@app.get("/matches/me/brands/{brand_raw_id}/posts", response_model=BrandPartnershipPostsResponse)
def get_brand_partnership_posts(
    brand_raw_id: int,
    current_user: CreatorProfile = Depends(get_completed_user),
):
    """
    A matched brand's confirmed creator partnership posts, newest first:
      - instagram_posts (direct brand-scrape flow): sponsorship confidence
        >= 90, paid_partnership true, and at least one creator in
        tagged_users / coauthor_producers / mentions / sponsors
      - test_creator_brand_partnership_posts (reverse-engineering flow):
        sponsorship confidence >= 90
    Duplicate post URLs are kept once.
    """
    db = SessionLocal()
    try:
        brand = db.query(BrandRaw).filter(BrandRaw.id == brand_raw_id).first()
        if brand is None:
            raise HTTPException(status_code=404, detail="Brand not found")

        # (full timestamp, post) — sorted on the full timestamp so same-day
        # posts keep their order, then only the date is returned.
        posts: list[tuple[str, PartnershipPost]] = []
        seen_urls: set[str] = set()

        instagram_rows = (
            db.query(InstagramPost)
            .filter(
                InstagramPost.brand_raw_id == brand_raw_id,
                InstagramPost.sponsorship_confidence >= _INSTAGRAM_PARTNERSHIP_MIN_CONFIDENCE,
                InstagramPost.paid_partnership.is_(True),
                InstagramPost.post_url.isnot(None),
            )
            .all()
        )
        for row in instagram_rows:
            creators = _instagram_post_creators(row)
            if not creators or row.post_url in seen_urls:
                continue
            seen_urls.add(row.post_url)
            posts.append((row.timestamp or "", PartnershipPost(
                source="instagram",
                post_url=row.post_url,
                post_date=_post_date(row.timestamp),
                sponsorship_confidence=row.sponsorship_confidence,
                creators=creators,
            )))

        re_rows = (
            db.query(TestCreatorBrandPartnershipPost)
            .filter(
                TestCreatorBrandPartnershipPost.brand_raw_id == brand_raw_id,
                TestCreatorBrandPartnershipPost.sponsorship_confidence >= _PARTNERSHIP_MIN_CONFIDENCE,
            )
            .all()
        )
        for row in re_rows:
            if row.post_url in seen_urls:
                continue
            seen_urls.add(row.post_url)
            posts.append((row.post_timestamp or "", PartnershipPost(
                source="reverse_engineering",
                post_url=row.post_url,
                post_date=_post_date(row.post_timestamp),
                sponsorship_confidence=row.sponsorship_confidence,
                creators=[row.creator_username] if row.creator_username else [],
            )))

        # ISO-8601 timestamps sort chronologically as strings; posts with no time go last.
        posts.sort(key=lambda item: item[0], reverse=True)
        return BrandPartnershipPostsResponse(
            brand_raw_id=brand.id,
            brand_name=brand.name,
            posts=[post for _, post in posts],
        )
    finally:
        db.close()
