import uuid
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqladmin import Admin, ModelView
from sqlalchemy import text

from pipeline.db import Base, Brand, BrandRaw, Contact, InstagramPost, MetaAd, Prompt, TiktokPost, TwitterPost, YoutubeSponsorship, SessionLocal, engine
from pipeline.enrichment.instagram_posts import (
    FULL_PROMPT_NAME, FULL_DEFAULT_PROMPT,
    COAUTHOR_PROMPT_NAME, COAUTHOR_DEFAULT_PROMPT,
)
from pipeline.enrichment.orchestrator import run_signal_enrichment
from pipeline.seed import run_seed


def _run_migrations() -> None:
    """Create tables and apply column migrations before accepting requests."""
    Base.metadata.create_all(bind=engine)
    stmts = [
        # Original columns (idempotent)
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS enrichment_failed BOOLEAN NOT NULL DEFAULT false",
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
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS top_commenters",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS is_comment_profile_scraped",
        "ALTER TABLE instagram_posts DROP COLUMN IF EXISTS confirmed_creators",
        "ALTER TABLE instagram_posts ADD COLUMN IF NOT EXISTS llm_checked BOOLEAN NOT NULL DEFAULT false",
        "DROP TABLE IF EXISTS instagram_commenters",
    ]
    with engine.connect() as conn:
        for sql in stmts:
            conn.execute(text(sql))
        conn.commit()

    # Seed default prompts (on conflict = already exists, keep existing content)
    with SessionLocal() as db:
        for name, content in [
            (FULL_PROMPT_NAME,    FULL_DEFAULT_PROMPT),
            (COAUTHOR_PROMPT_NAME, COAUTHOR_DEFAULT_PROMPT),
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

# ---------------------------------------------------------------------------
# SQLAdmin — /admin
# ---------------------------------------------------------------------------

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
        BrandRaw.enriched,
        BrandRaw.enrichment_failed,
        BrandRaw.wikidata_enriched,
        BrandRaw.shopify_checked,
        BrandRaw.tranco_checked,
        BrandRaw.meta_ads_fetched,
        BrandRaw.youtube_checked,
        BrandRaw.instagram_checked,
        BrandRaw.tiktok_checked,
        BrandRaw.twitter_checked,
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
        BrandRaw.in_tranco_list,
        BrandRaw.tranco_rank,
        BrandRaw.enriched,
        BrandRaw.wikidata_enriched,
        BrandRaw.shopify_checked,
        BrandRaw.tranco_checked,
        BrandRaw.meta_ads_fetched,
        BrandRaw.youtube_checked,
        BrandRaw.instagram_checked,
        BrandRaw.tiktok_checked,
        BrandRaw.twitter_checked,
        BrandRaw.created_at,
        BrandRaw.enrichment_failed,
    ]
    column_default_sort = [(BrandRaw.id, True)]
    page_size = 50


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
    column_sortable_list   = [MetaAd.id, MetaAd.brand_raw_id, MetaAd.start_date, MetaAd.fetched_at]
    column_default_sort    = [(MetaAd.id, True)]
    page_size = 50


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
        YoutubeSponsorship.sponsorship_type,
        YoutubeSponsorship.confidence,
        YoutubeSponsorship.matched_keywords,
        YoutubeSponsorship.view_count,
        YoutubeSponsorship.published_at,
        YoutubeSponsorship.video_url,
        YoutubeSponsorship.description_snippet,
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
        YoutubeSponsorship.sponsorship_type,
        YoutubeSponsorship.confidence,
        YoutubeSponsorship.matched_keywords,
        YoutubeSponsorship.view_count,
        YoutubeSponsorship.like_count,
        YoutubeSponsorship.published_at,
        YoutubeSponsorship.fetched_at,
    ]
    column_default_sort    = [(YoutubeSponsorship.confidence, True)]
    page_size = 50


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
        InstagramPost.mentions,
        InstagramPost.tagged_users,
        InstagramPost.coauthor_producers,
        InstagramPost.followers_count,
        InstagramPost.caption,
        InstagramPost.post_url,
        InstagramPost.fetched_at,
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
        InstagramPost.coauthor_producers,
        InstagramPost.mentions,
        InstagramPost.tagged_users,
        InstagramPost.fetched_at,
    ]
    column_default_sort    = [(InstagramPost.id, True)]
    page_size = 50


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
    page_size = 50


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
    page_size = 50


class PromptAdmin(ModelView, model=Prompt):
    name         = "Prompt"
    name_plural  = "Prompts"
    icon         = "fa-solid fa-wand-magic-sparkles"
    column_list  = [Prompt.id, Prompt.name, Prompt.content, Prompt.updated_at]
    column_searchable_list = [Prompt.name]
    column_default_sort    = [(Prompt.id, True)]
    page_size = 20


class BrandAdmin(ModelView, model=Brand):
    name         = "Brand"
    name_plural  = "Brands"
    icon         = "fa-solid fa-building"
    column_list  = [
        Brand.id, Brand.name, Brand.domain, Brand.industry,
        Brand.employee_count, Brand.hq_country, Brand.enrichment_source,
        Brand.contacts_fetched, Brand.contacts_fetch_failed, Brand.enriched_at,
    ]
    column_searchable_list = [Brand.name, Brand.domain, Brand.industry]
    column_sortable_list   = [Brand.id, Brand.domain, Brand.contacts_fetched, Brand.enriched_at]
    column_default_sort    = [(Brand.id, True)]
    page_size = 50


class ContactAdmin(ModelView, model=Contact):
    name         = "Contact"
    name_plural  = "Contacts"
    icon         = "fa-solid fa-user"
    column_list  = [
        Contact.id, Contact.full_name, Contact.title, Contact.title_score,
        Contact.email, Contact.email_verified, Contact.email_status,
        Contact.email_guessed, Contact.outreach_sent, Contact.created_at,
    ]
    column_searchable_list = [Contact.full_name, Contact.email, Contact.title]
    column_sortable_list   = [Contact.id, Contact.title_score, Contact.email_verified, Contact.created_at]
    column_default_sort    = [(Contact.id, True)]
    page_size = 50


admin = Admin(app, engine)
admin.add_view(BrandRawAdmin)
admin.add_view(MetaAdAdmin)
admin.add_view(YoutubeSponsorshipAdmin)
admin.add_view(InstagramPostAdmin)
admin.add_view(PromptAdmin)
admin.add_view(TiktokPostAdmin)
admin.add_view(TwitterPostAdmin)
admin.add_view(BrandAdmin)
admin.add_view(ContactAdmin)

# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Background job store (in-memory; single-process)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Frontend — /
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/", include_in_schema=False)
def frontend():
    return FileResponse("frontend/index.html")


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

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Prompt endpoints
# ---------------------------------------------------------------------------

class PromptResponse(BaseModel):
    name:       str
    content:    str
    updated_at: str | None = None


class PromptUpdateRequest(BaseModel):
    content: str


@app.get("/prompts/{name}", response_model=PromptResponse)
def get_prompt(name: str):
    """Return a prompt by name. Returns the hardcoded default if not found in DB."""
    db = SessionLocal()
    try:
        row = db.query(Prompt).filter(Prompt.name == name).first()
        if row:
            return PromptResponse(
                name=row.name,
                content=row.content,
                updated_at=str(row.updated_at) if row.updated_at else None,
            )
        _defaults = {
            FULL_PROMPT_NAME:    FULL_DEFAULT_PROMPT,
            COAUTHOR_PROMPT_NAME: COAUTHOR_DEFAULT_PROMPT,
        }
        if name in _defaults:
            return PromptResponse(name=name, content=_defaults[name])
        raise HTTPException(status_code=404, detail=f"Prompt '{name}' not found")
    finally:
        db.close()


@app.put("/prompts/{name}", response_model=PromptResponse)
def update_prompt(name: str, body: PromptUpdateRequest):
    """Create or update a prompt by name."""
    db = SessionLocal()
    try:
        row = db.query(Prompt).filter(Prompt.name == name).first()
        if row:
            row.content = body.content
        else:
            row = Prompt(name=name, content=body.content)
            db.add(row)
        db.commit()
        db.refresh(row)
        return PromptResponse(
            name=row.name,
            content=row.content,
            updated_at=str(row.updated_at) if row.updated_at else None,
        )
    finally:
        db.close()
