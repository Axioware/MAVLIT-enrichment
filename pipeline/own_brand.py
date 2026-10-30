"""
pipeline/own_brand.py

A MAVLIT user pitches "my own brand" by giving its Instagram URL or handle (pitch page).

  1. start_own_brand_job()  - the handle is looked up in brands_raw (an existing row with the same
     handle is reused, otherwise a new row is added) and an OwnBrandJob is created for the page to
     follow. The brand is "ready" at once when brands_raw already holds everything the pitch needs:
     name, niche, description and target audience gender (the target age and the latest product may
     be empty). Nothing is scraped or asked of the LLM in that case.
  2. run_own_brand_job()    - background, and only for what is missing:
       - no Instagram posts saved for the brand -> Apify scrapes its last 40 posts (saved in
         instagram_posts, instagram_checked = true so the normal pipeline does not scrape them again);
       - a required field is empty -> one LLM call (prompt brand_from_instagram, editable in /admin)
         reads the bio + captions from the saved posts and fills ONLY the empty fields (name,
         description, niche, sub-niche tags, target audience gender and age - an unconfirmed
         estimate - and latest product). Fields that already have a value are never changed.
     The job moves queued -> running -> ready (or failed) and its `stage` tells the page what is
     happening. The pitch is then written from the saved fields.
  3. run_followup()         - background, started by the user's first pitch for a brand added through
     this flow: instagram posts (if none yet), brand tier, sponsorship scoring of the posts, Instagram
     users of the >= 90 partnership posts, then the website (from the brand's Instagram profile, the
     step that sets instagram_profile_checked) and geo reach, and last initial brand score, brand
     signals and Apollo contacts - these three only when the geo reach score is 0-40. A brand that is
     new, or already in brands_raw without a website or geo score, gets the website and geo reach;
     geo reach needs the website, so it is skipped when none was found. A step whose work is already done is skipped (logged as "already done"), so nothing paid
     runs twice; each step also keeps its own run conditions (see the step modules). The tier step
     comes first because website and geo reach skip a brand without one.

Needs APIFY_TOKEN and OPENAI_KEY.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from config import APIFY_TOKEN, OPENAI_KEY
from pipeline.db import BrandRaw, CreatorProfile, InstagramPost, OwnBrandJob, Prompt, BatchSessionLocal, SessionLocal, normalize_niche
from pipeline.helpers.apify import request_path
from pipeline.helpers.gpt_llm import LLMUnavailableError, call_gpt_json, fill_template, llm_usage_scope, record_apify_scrape
from pipeline.helpers.normalize import normalize
from pipeline.helpers.prompts import BRAND_FROM_INSTAGRAM_DEFAULT_PROMPT, BRAND_FROM_INSTAGRAM_PROMPT_NAME

logger = logging.getLogger(__name__)

POSTS_TO_SCRAPE = 40
_STALE_AFTER = timedelta(minutes=10)    # a queued/running job not updated for this long is reported as failed
_MIN_LATEST_PRODUCT_CONFIDENCE = 50

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_RESERVED_PATHS = {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "about", "web"}


# ---------------------------------------------------------------------------
# Handle / brand row
# ---------------------------------------------------------------------------

def parse_instagram_handle(raw: str) -> str:
    """
    A bare lower-case Instagram username from '@handle', 'handle' or an instagram.com URL (with
    or without www / scheme / trailing parts). Raises ValueError for anything else.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("Enter the brand's Instagram URL or handle.")
    if "instagram.com" in value.lower():
        parsed = urlparse(value if "//" in value else "https://" + value)
        segments = [s for s in parsed.path.split("/") if s]
        if not segments or segments[0].lower() in _RESERVED_PATHS:
            raise ValueError("That link is not an Instagram profile. Use the brand's profile URL, like instagram.com/brandname")
        value = segments[0]
    elif "/" in value or " " in value:
        raise ValueError("Enter the brand's Instagram URL or handle, like @brandname")
    value = value.lstrip("@")
    if not _HANDLE_RE.match(value):
        raise ValueError("That does not look like an Instagram handle. Use letters, numbers, dots and underscores only.")
    return value.lower()


def _profile_url(handle: str) -> str:
    return f"https://www.instagram.com/{handle}"


def find_brand_by_handle(db: Session, handle: str) -> BrandRaw | None:
    """The brands_raw row whose stored Instagram handle / URL is this username (case-insensitive)."""
    row = db.execute(text(r"""
        SELECT id FROM brands_raw
        WHERE lower(regexp_replace(regexp_replace(btrim(coalesce(instagram_handle, '')),
              '^(https?://)?(www\.)?instagram\.com/', '', 'i'), '[/?#].*$|^@', '', 'g')) = :h
        ORDER BY id LIMIT 1
    """), {"h": handle}).first()
    return db.get(BrandRaw, row[0]) if row else None


def missing_fields(brand: BrandRaw) -> list[str]:
    """What the pitch needs from the brand and brands_raw does not have yet (target age / latest product may stay empty)."""
    required = {
        "name": brand.name,
        "niche": brand.niche,
        "description": brand.description,
        "target_audience_gender": brand.target_audience_gender,
    }
    return [field for field, value in required.items() if not (value or "").strip()]


def _post_count(db: Session, brand_id: int) -> int:
    """Saved posts of the brand (profile-only rows without a post id do not count)."""
    return (
        db.query(func.count(InstagramPost.id))
        .filter(InstagramPost.brand_raw_id == brand_id, InstagramPost.post_id.isnot(None))
        .scalar()
    ) or 0


def start_own_brand_job(db: Session, creator_id: int, raw_handle: str) -> OwnBrandJob:
    """
    Add (or reuse) the brand and create the job for it. Returns the job: already `ready` when
    the brand is known and has a description and niche, `queued` otherwise (the caller then runs
    run_own_brand_job in the background). A queued/running job of the same user for the same
    brand is returned instead of starting a second scrape. Raises ValueError for a bad handle.
    """
    handle = parse_instagram_handle(raw_handle)
    brand = find_brand_by_handle(db, handle)
    created = False
    if brand is None:
        brand = BrandRaw(instagram_handle=_profile_url(handle), refferls=False, source="user_added")
        db.add(brand)
        db.flush()
        created = True

    running = (
        db.query(OwnBrandJob)
        .filter(OwnBrandJob.creator_profile_id == creator_id, OwnBrandJob.brand_raw_id == brand.id,
                OwnBrandJob.status.in_(("queued", "running")))
        .first()
    )
    if running is not None and not _is_stale(running):
        db.commit()
        return running

    ready = not created and not missing_fields(brand)   # everything the pitch needs is already saved
    job = OwnBrandJob(
        id=str(uuid.uuid4()),
        creator_profile_id=creator_id,
        brand_raw_id=brand.id,
        instagram_handle=handle,
        created_brand=created,
        status="ready" if ready else "queued",
        stage="Brand info ready" if ready else "Checking what we know about the brand",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("Own brand: creator_id=%d @%s brand_id=%d created=%s missing=%s status=%s",
                creator_id, handle, brand.id, created, missing_fields(brand), job.status)
    return job


def _is_stale(job: OwnBrandJob) -> bool:
    updated = job.updated_at or job.created_at
    return job.status in ("queued", "running") and updated is not None and datetime.now(timezone.utc) - updated > _STALE_AFTER


def refresh_stale(db: Session, job: OwnBrandJob) -> OwnBrandJob:
    """Report a job that stopped updating (e.g. the server restarted mid-run) as failed instead of running forever."""
    if _is_stale(job):
        job.status, job.stage = "failed", "Stopped"
        job.error = "This took too long and was stopped. Please try again."
        db.commit()
    return job


# ---------------------------------------------------------------------------
# Gather the brand's info (background)
# ---------------------------------------------------------------------------

def _set_stage(db: Session, job: OwnBrandJob, stage: str, status: str | None = None) -> None:
    job.stage = stage
    if status:
        job.status = status
    db.commit()


def _fail(db: Session, job: OwnBrandJob, message: str) -> None:
    db.rollback()
    job = db.get(OwnBrandJob, job.id)
    job.status, job.stage, job.error = "failed", "Stopped", message
    db.commit()


def _bounded(value, low: int, high: int) -> int | None:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _apply_profile(db: Session, brand: BrandRaw, handle: str, result: dict) -> None:
    """Save the LLM's brand profile on brands_raw (+ the brands_niches mirror). Existing values are kept."""
    from pipeline.enrichment.shopify_detect import _upsert_brand_niche   # lazy: avoids loading the scraper stack at import

    name = result.get("name")
    name = " ".join(name.split()) if isinstance(name, str) else ""
    if not name or name.lower() == "unknown":
        name = handle   # the LLM could not tell the brand's name: the handle is the best clean name
    if not brand.name:
        # brands_raw.name_normalized must be unique. When another brand already has this name (e.g. the
        # same company's regional account) the visible name stays clean and only the key gets the handle.
        key = normalize(name)
        if db.query(BrandRaw.id).filter(BrandRaw.name_normalized == key, BrandRaw.id != brand.id).first():
            key = f"{key} (@{handle})"
        brand.name = name
        brand.name_normalized = key

    description = result.get("description")
    if isinstance(description, str) and description.strip() and not (brand.description or "").strip():
        brand.description = " ".join(description.split())

    niche = result.get("niche")
    if isinstance(niche, str) and niche.strip() and niche.strip().lower() != "unknown" and not (brand.niche or "").strip():
        brand.niche = normalize_niche(niche)

    # Target audience: gender is never left empty ("both" with 0 confidence when unsure); the age is only an estimate.
    if not (brand.target_audience_gender or "").strip():
        gender = str(result.get("target_audience_gender") or "").strip().lower()
        confidence = _bounded(result.get("target_audience_gender_confidence"), 0, 100) or 0
        if gender not in ("male", "female", "both"):
            gender, confidence = "both", 0
        brand.target_audience_gender, brand.target_audience_gender_confidence = gender, confidence
    if brand.target_audience_min_age is None and brand.target_audience_max_age is None:
        low, high = _bounded(result.get("target_audience_min_age"), 13, 80), _bounded(result.get("target_audience_max_age"), 13, 80)
        if low is not None and high is not None and low <= high:
            brand.target_audience_min_age, brand.target_audience_max_age = low, high
            brand.target_audience_age_confidence = _bounded(result.get("target_audience_age_confidence"), 0, 100) or 0

    product = result.get("latest_product")
    product_confidence = _bounded(result.get("latest_product_confidence"), 0, 100) or 0
    if (isinstance(product, str) and product.strip() and product_confidence >= _MIN_LATEST_PRODUCT_CONFIDENCE
            and not (brand.latest_product or "").strip()):
        brand.latest_product = " ".join(product.split())[:300]
        brand.latest_product_confidence = product_confidence

    tags = result.get("tags")
    tags = [t.strip() for t in tags if isinstance(t, str) and t.strip()][:8] if isinstance(tags, list) else []
    db.flush()
    if brand.niche:
        _upsert_brand_niche(db, brand.id, brand.niche, brand.description or "", tags or None)


def _get_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == BRAND_FROM_INSTAGRAM_PROMPT_NAME).first()
    return row.content if row else BRAND_FROM_INSTAGRAM_DEFAULT_PROMPT


def scrape_and_save_posts(db: Session, brand: BrandRaw, handle: str, creator_id: int) -> int | None:
    """
    Apify: the brand's last POSTS_TO_SCRAPE posts, saved in instagram_posts (the normal pipeline then does not
    scrape the brand again: instagram_checked = true). Returns the number of posts saved, or None when
    Instagram could not be reached (the brand is left as it was).
    """
    from pipeline.enrichment.instagram_posts import _build_row, _scrape_handle   # lazy: avoids loading the scraper stack at import
    from pipeline.helpers.db import upsert_rows

    items = _scrape_handle(handle, POSTS_TO_SCRAPE)
    if items is None:
        return None
    items = [i for i in items if isinstance(i, dict)]
    with llm_usage_scope(creator_id, "own_brand"):
        record_apify_scrape("instagram", handle, POSTS_TO_SCRAPE, len(items))
    rows = [r for r in (_build_row(brand.id, handle, item) for item in items) if r]
    if rows:
        upsert_rows(db, InstagramPost, rows, ["post_id"])
        db.get(BrandRaw, brand.id).instagram_checked = True
        db.commit()
    return len(rows)


def _llm_inputs_from_saved_posts(db: Session, brand_id: int) -> dict:
    """Bio, business category, follower count and the newest posts of the brand, from instagram_posts."""
    rows = (
        db.query(InstagramPost)
        .filter(InstagramPost.brand_raw_id == brand_id, InstagramPost.post_id.isnot(None))
        .order_by(InstagramPost.timestamp.desc().nullslast())
        .limit(POSTS_TO_SCRAPE)
        .all()
    )
    return {
        "bio": next((r.biography for r in rows if r.biography), None),
        "category": next((r.business_category_name for r in rows if r.business_category_name), None),
        "followers": next((r.followers_count for r in rows if r.followers_count is not None), None),
        "posts": rows,
    }


def _posts_text(rows: list[InstagramPost]) -> str:
    """Compact JSON lines of the posts' caption / hashtags / mentions, newest first, size-capped."""
    lines, total = [], 0
    for r in rows:
        entry = {
            "caption": " ".join((r.caption or "").split())[:450],
            "hashtags": (r.hashtags or [])[:8] if isinstance(r.hashtags, list) else [],
            "mentions": (r.mentions or [])[:5] if isinstance(r.mentions, list) else [],
        }
        if not (entry["caption"] or entry["hashtags"]):
            continue
        line = json.dumps(entry, ensure_ascii=False)
        total += len(line)
        if total > 14000:
            break
        lines.append(line)
    return "\n".join(lines) or "none"


def run_own_brand_job(job_id: str) -> None:
    """
    Background: bring the brand up to what the pitch needs, doing only what is missing - scrape its posts if none are
    saved, then let the LLM fill the empty fields from the saved bio + captions. Never raises.
    """
    with SessionLocal() as db:
        job = db.get(OwnBrandJob, job_id)
        if job is None or job.status != "queued":
            return
        creator_id, handle = job.creator_profile_id, job.instagram_handle
        try:
            brand = db.get(BrandRaw, job.brand_raw_id)
            missing = missing_fields(brand)
            if not missing:   # filled in by someone else since the job was created
                _set_stage(db, job, "Brand info ready", "ready")
                return

            if _post_count(db, brand.id) == 0:
                if not APIFY_TOKEN:
                    return _fail(db, job, "Brand lookup is not available right now. Please try again later.")
                _set_stage(db, job, "Reading the brand's Instagram posts (this can take a minute)", "running")
                with request_path():   # the creator is waiting: 90 s, not the batch limit
                    saved = scrape_and_save_posts(db, brand, handle, creator_id)
                if saved is None:
                    return _fail(db, job, "Could not reach this Instagram account right now. Please try again.")
                if saved == 0:
                    return _fail(db, job, "No posts found. The account may not exist, be private or have no posts yet.")
            else:
                _set_stage(db, job, "Reading the brand's saved Instagram posts", "running")

            if not OPENAI_KEY:
                return _fail(db, job, "Brand lookup is not available right now. Please try again later.")
            _set_stage(db, job, "Writing the brand profile")
            info = _llm_inputs_from_saved_posts(db, brand.id)
            prompt = fill_template(
                _get_prompt(db),
                handle=handle,
                bio=" ".join((info["bio"] or "").split())[:800] or "none",
                business_category=info["category"] or "none",
                followers=f"{info['followers']:,}" if isinstance(info["followers"], int) else "unknown",
                posts=_posts_text(info["posts"]),
            )
            db.commit()   # end the read transaction (saved posts, prompt) before the slow LLM call: no idle-in-transaction session meanwhile
            with llm_usage_scope(creator_id, "own_brand"):
                result = call_gpt_json(prompt, context=f"own brand @{handle}")
            if not isinstance(result, dict) or not result:
                return _fail(db, job, "Could not write the brand profile. Please try again.")
            brand = db.get(BrandRaw, brand.id)
            _apply_profile(db, brand, handle, result)
            job = db.get(OwnBrandJob, job_id)
            job.status, job.stage, job.error = "ready", "Brand info ready", None
            db.commit()
            logger.info("Own brand ready: job=%s brand_id=%d @%s (filled: %s)", job_id, brand.id, handle, missing)
        except LLMUnavailableError:
            logger.exception("Own brand job %s: OpenAI unavailable", job_id)
            _fail(db, job, "The AI service is unavailable right now. Please try again in a few minutes.")
        except Exception:
            logger.exception("Own brand job %s failed", job_id)
            _fail(db, job, "Something went wrong while gathering this brand's info. Please try again.")


# ---------------------------------------------------------------------------
# Follow-up pipeline after the first pitch (background)
# ---------------------------------------------------------------------------

_FOLLOWUP_RUNNING_FOR = timedelta(hours=2)   # another user's follow-up for the same brand counts as running this long


def claim_followup(db: Session, creator_id: int, brand_id: int | None) -> str | None:
    """
    Called after a pitch was written: if this user added the brand through this flow and its follow-up has
    not started, mark it started and return the job id to run (else None). When another user's follow-up for
    the same brand is still running, this one is not started (the brand is being processed already).
    """
    if brand_id is None:
        return None
    job = (
        db.query(OwnBrandJob)
        .filter(OwnBrandJob.creator_profile_id == creator_id, OwnBrandJob.brand_raw_id == brand_id,
                OwnBrandJob.status == "ready", OwnBrandJob.followup_started_at.is_(None))
        .order_by(OwnBrandJob.created_at.desc())
        .with_for_update()
        .first()
    )
    if job is None:
        db.rollback()
        return None
    now = datetime.now(timezone.utc)
    busy = (
        db.query(OwnBrandJob.id)
        .filter(OwnBrandJob.brand_raw_id == brand_id, OwnBrandJob.id != job.id, OwnBrandJob.followup_status == "running",
                OwnBrandJob.followup_started_at > now - _FOLLOWUP_RUNNING_FOR)
        .first()
    )
    job.followup_started_at = now
    if busy:
        job.followup_status, job.followup_log = "skipped", [{"step": "all", "skipped": "another follow-up for this brand is already running"}]
        db.commit()
        return None
    job.followup_status, job.followup_log = "running", []
    db.commit()
    return job.id


def _geo_gate(brand: BrandRaw) -> str | None:
    """A reason to skip when the brand's geo reach score is not 0-40 (missing counts as not in range), else None."""
    score = brand.geo_reach_score
    if score is None:
        return "geo reach has no score, so the scoring steps are not run"
    if not 0 <= score <= 40:
        return f"geo reach score {score} is outside 0-40"
    return None


def _followup_steps():
    """
    (name, skip_if_done, run) for each follow-up step, in order. skip_if_done(db, brand) returns a reason
    when the step's work already exists for the brand (the step is then skipped), else None. Several step
    functions ignore their own "already done" filters when given a brand id, so the check is made here.
    """
    from pipeline.db import BrandContact, BrandProfile, InitialBrandScore
    from pipeline.enrichment.apollo_contacts import run_apollo_contacts
    from pipeline.enrichment.brand_signals import run_brand_signals
    from pipeline.enrichment.brand_tier import score_brand_tier
    from pipeline.enrichment.geo_reach.geo_reach import enrich_geo_reach
    from pipeline.enrichment.initial_brand_scoring import run_brand_scoring
    from pipeline.enrichment.instagram_posts import score_instagram_post_sponsorship
    from pipeline.enrichment.instagram_users import enrich_instagram_users
    from pipeline.enrichment_re.brand_instagram_profile import enrich_brand_instagram_profile

    def has(db, model, brand_id):
        return db.query(model.brand_raw_id).filter(model.brand_raw_id == brand_id).first() is not None

    def instagram_users(db, brand_id):
        done = 0
        for _ in range(15):   # posts with sponsorship_confidence >= 90, a few at a time
            processed = enrich_instagram_users(db, limit=3, brand_raw_id=brand_id)
            if not processed:
                break
            done += processed
        return done

    return [
        ("instagram_posts", lambda db, b: "posts already saved" if _post_count(db, b.id) else None, None),   # run is bound in run_followup
        ("brand_tier", lambda db, b: "tier already set" if (b.brand_tier or "").strip().lower() not in ("", "null") else None,
         lambda db, b: score_brand_tier(db, brand_raw_id=b.id)),
        ("post_sponsorship_scores", lambda db, b: None, lambda db, b: score_instagram_post_sponsorship(db, brand_raw_id=b.id)),   # only unscored posts
        ("instagram_users", lambda db, b: None, lambda db, b: instagram_users(db, b.id)),   # only posts not yet processed
        # Last: a brand with no website (new, or in brands_raw without one) gets it from its Instagram profile
        # (the step that sets instagram_profile_checked), and only then its geo reach can be scored.
        ("website",
         lambda db, b: ("website already known" if (b.website or "").strip()
                        else "profile already checked, no website was found" if b.instagram_profile_checked else None),
         lambda db, b: enrich_brand_instagram_profile(db, limit=1, brand_id=b.id)),
        ("geo_reach",
         lambda db, b: ("geo reach already scored" if b.geo_reach_score is not None
                        else "no website to read, so geo reach cannot be scored" if not (b.website or "").strip() else None),
         lambda db, b: enrich_geo_reach(db, brand_id=b.id)),
        # The last three only run for a brand whose geo reach score is 0-40 (local / small reach, the range the
        # matching shows). Any other score, or none, stops here.
        ("initial_brand_score", lambda db, b: _geo_gate(b) or ("already scored" if has(db, InitialBrandScore, b.id) else None),
         lambda db, b: run_brand_scoring(db, brand_id=b.id)),
        ("brand_signals", lambda db, b: _geo_gate(b) or ("signals already computed" if (db.query(BrandProfile.brand_raw_id)
            .filter(BrandProfile.brand_raw_id == b.id, BrandProfile.embedding.isnot(None)).first() is not None) else None),
         lambda db, b: run_brand_signals(db, brand_id=b.id)),
        ("apollo_contacts", lambda db, b: _geo_gate(b) or ("contacts already looked up" if has(db, BrandContact, b.id) else None),
         lambda db, b: run_apollo_contacts(db, brand_id=b.id)),
    ]


BATCH_BUSY_MESSAGE = "Another job is running, retry shortly."


def run_followup(job_id: str) -> None:
    """
    Background: the follow-up pipeline (see _run_followup). When the batch pool has no free connection within its 10 s
    pool_timeout the job is marked failed with a clear message and can be claimed again by the next pitch. Never raises.
    """
    try:
        _run_followup(job_id)
    except PoolTimeoutError:
        logger.error("Own brand follow-up %s: no batch connection free", job_id)
        with SessionLocal() as db:   # the batch pool is exhausted: use the API pool for this one short write
            job = db.get(OwnBrandJob, job_id)
            if job is not None:
                job.followup_status, job.followup_started_at = "failed", None
                job.followup_log = [{"step": "all", "error": BATCH_BUSY_MESSAGE}]
                db.commit()


def _run_followup(job_id: str) -> None:
    """
    Background: run the rest of the pipeline on the brand, in order, skipping the steps that are already done.
    A step that fails or is skipped by its own run conditions is logged in followup_log and the next one still
    runs, except when OpenAI is unavailable (the run stops). Never raises.
    """
    with BatchSessionLocal() as db:   # the steps keep a transaction open across Apify/LLM calls: long idle limit
        job = db.get(OwnBrandJob, job_id)
        if job is None:
            return
        brand_id, creator_id, handle = job.brand_raw_id, job.creator_profile_id, job.instagram_handle
        log: list[dict] = []
        status = "done"
        try:
            for name, skip_if_done, run in _followup_steps():
                try:
                    brand = db.get(BrandRaw, brand_id)
                    db.refresh(brand)
                    reason = skip_if_done(db, brand)
                    if reason:
                        log.append({"step": name, "skipped": reason})
                    else:
                        with llm_usage_scope(creator_id, "own_brand_followup"):
                            if name == "instagram_posts":   # needs the creator and the handle
                                result = scrape_and_save_posts(db, brand, handle, creator_id)
                            else:
                                result = run(db, brand)
                        log.append({"step": name, "result": result})
                except LLMUnavailableError:
                    log.append({"step": name, "error": "OpenAI unavailable - stopped"})
                    status = "failed"
                    break
                except Exception as exc:
                    db.rollback()
                    logger.exception("Own brand follow-up: step %s failed for brand_id=%s", name, brand_id)
                    log.append({"step": name, "error": f"{type(exc).__name__}: {exc}"[:300]})
                job = db.get(OwnBrandJob, job_id)
                job.followup_log = list(log)
                db.commit()
        finally:
            job = db.get(OwnBrandJob, job_id)
            job.followup_status, job.followup_log = status, list(log)
            db.commit()
            logger.info("Own brand follow-up %s for brand_id=%s: %s", status, brand_id, log)
