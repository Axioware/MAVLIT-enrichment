"""
pipeline/competitor_creators.py

A MAVLIT user adds a competitor creator (an Instagram handle) to compare themselves with, in Feed > Similar Creators > Compare Creators.

  1. start_competitor()   the handle is looked up in competitor_creators (an account another user added is reused: nothing is scraped twice) and the
                          user is linked to it. A new, failed or stuck competitor gets a job.
  2. run_competitor_job() background, for that competitor:
       - the account is already in instagram_users (a creator MAVLIT knows): its numbers are worked out from those rows, no Apify run
         (source "instagram_users");
       - it is not: Apify reads its latest 20 posts, saved in competitor_creator_posts (source "apify"). An account with no posts (it does not exist,
         or is private) is dropped again, so a wrong handle leaves nothing behind.
     Either way it fills in followers, average likes and comments, engagement rate, average Reel views, posts per week, the brands the posts are a
     partnership with, and the content mix from the same LLM prompt the creators use (product-focused share and the five biggest categories).
     A brand a partnership post names that has no row in brands_raw gets a bare one (its Instagram handle), like the reverse-engineering flow does.
     Partnerships are the posts an LLM (the brand_check prompt, same as content_creator_re.py) confirms are with a real brand account.

Competitors are kept apart from instagram_users on purpose: what users type in must not become part of the creator data that matching and
Similar Creators run on.

Needs APIFY_TOKEN and OPENAI_KEY.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from config import APIFY_TOKEN, OPENAI_KEY
from pipeline.db import (
    BatchSessionLocal, CompetitorCreator, CompetitorCreatorPost, InstagramUser, UserCompetitor,
)
from pipeline.helpers.apify import request_path
from pipeline.helpers.content_mix import content_mix_from_result
from pipeline.helpers.engagement import engagement_rate, engagement_rate_from_posts
from pipeline.helpers.gpt_llm import LLMUnavailableError, call_gpt_json, fill_template, llm_usage_scope
from pipeline.similar_creators import MIN_PARTNERSHIP_CONFIDENCE, average_views, posts_per_week

logger = logging.getLogger(__name__)

POSTS_TO_READ = 20
MAX_COMPETITORS_PER_USER = 10
STALE_AFTER = timedelta(minutes=15)    # a queued/running competitor not updated for this long is reported as failed
_CAPTIONS_FOR_THE_MIX = 15
_CAPTION_CHARS = 400


# --- the numbers (pure) -----------------------------------------------------------------------------------------------------------------------

def _count(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _average(values) -> int | None:
    numbers = [n for n in (_count(v) for v in values) if n is not None]
    return round(sum(numbers) / len(numbers)) if numbers else None


def summarize_posts(posts: list[dict], followers: int | None) -> dict:
    """
    The numbers of a competitor from its posts (dicts with likes, comments, views, timestamp): average likes and comments (hidden counts, which are -1,
    are left out), the engagement rate ((average likes + average comments) / followers * 100), the average Reel views and the posts per week.
    """
    return {
        "avg_likes": _average(p.get("likes") for p in posts),
        "avg_comments": _average(p.get("comments") for p in posts),
        "engagement_rate": engagement_rate_from_posts([(p.get("likes"), p.get("comments")) for p in posts], followers),
        "avg_reel_views": average_views([p.get("views") for p in posts]),
        "posts_per_week": posts_per_week([p.get("timestamp") for p in posts]),
    }


def partnership_brands(posts: list[dict]) -> list[dict]:
    """The brands the posts are a partnership with, the one with the most posts first: [{brand_raw_id, handle, name, posts}]. `posts` carry brand_raw_id / brand_handle / brand_name."""
    counts: dict[int, dict] = {}
    for post in posts:
        brand_id = post.get("brand_raw_id")
        if brand_id is None:
            continue
        entry = counts.setdefault(brand_id, {"brand_raw_id": brand_id, "handle": post.get("brand_handle"), "name": post.get("brand_name"), "posts": 0})
        entry["posts"] += 1
    return sorted(counts.values(), key=lambda b: (-b["posts"], b["handle"] or ""))


# --- the LLM content mix ------------------------------------------------------------------------------------------------------------------------

def estimate_mix(db: Session, handle: str, niche: str | None, bio: str | None, captions: list[str], retries: int = 2) -> dict:
    """The content mix of a creator from their bio and captions, from the creators' prompt (creator_content_mix): {"product_focused_pct", "content_categories"}; both None when it could not be made."""
    from pipeline.enrichment.creator_niches import _get_mix_prompt   # lazy: loads the script's module
    captions = [" ".join(c.split())[:_CAPTION_CHARS] for c in captions if c and c.strip()][:_CAPTIONS_FOR_THE_MIX]
    if not captions or not OPENAI_KEY:
        return {"product_focused_pct": None, "content_categories": None}
    prompt = fill_template(_get_mix_prompt(db), niche=niche or "none provided", bio=" ".join((bio or "").split())[:800] or "none provided",
                           captions="\n".join(f"- {c}" for c in captions))
    mix = {"product_focused_pct": None, "content_categories": None}
    for attempt in range(1, retries + 1):
        mix = content_mix_from_result(call_gpt_json(prompt, context=f"competitor content mix @{handle} attempt={attempt}"))
        if mix["content_categories"]:
            break
    return mix


# --- starting -----------------------------------------------------------------------------------------------------------------------------------

def _stale(comp: CompetitorCreator) -> bool:
    updated = comp.updated_at or comp.created_at
    return comp.status in ("queued", "running") and updated is not None and datetime.now(timezone.utc) - updated > STALE_AFTER


def needs_job(comp: CompetitorCreator | None) -> bool:
    """A competitor nobody has looked up, one whose lookup failed, or one whose lookup got stuck needs a (new) job; a ready or a running one does not."""
    return comp is None or comp.status == "failed" or _stale(comp)


def find_competitor(db: Session, handle: str) -> CompetitorCreator | None:
    return db.query(CompetitorCreator).filter(CompetitorCreator.instagram_handle == handle).first()


def start_competitor(db: Session, creator_id: int, raw_handle: str) -> tuple[CompetitorCreator, bool]:
    """
    Add the competitor for this user. Returns (competitor, whether a job has to run). Raises ValueError for a bad handle or when the user already
    has MAX_COMPETITORS_PER_USER competitors (nothing is created then).
    """
    from pipeline.own_brand import parse_instagram_handle   # lazy: the module that parses the handle the same way for brands
    handle = parse_instagram_handle(raw_handle)
    comp = find_competitor(db, handle)
    already = comp is not None and db.get(UserCompetitor, (creator_id, comp.id)) is not None
    if not already and db.query(func.count(UserCompetitor.competitor_creator_id)).filter(UserCompetitor.creator_profile_id == creator_id).scalar() >= MAX_COMPETITORS_PER_USER:
        raise ValueError(f"You can compare with up to {MAX_COMPETITORS_PER_USER} competitors. Remove one first.")
    run = needs_job(comp)
    if comp is None:
        comp = CompetitorCreator(instagram_handle=handle, status="queued", stage="Checking what we know about the creator")
        db.add(comp)
        db.flush()
    elif run:
        comp.status, comp.stage, comp.error = "queued", "Checking what we know about the creator", None
    if not already:
        db.add(UserCompetitor(creator_profile_id=creator_id, competitor_creator_id=comp.id))
    db.commit()
    db.refresh(comp)
    return comp, run


def refresh_stale(db: Session, comp: CompetitorCreator) -> CompetitorCreator:
    """Report a lookup that stopped updating (for example the server restarted mid-run) as failed instead of running forever."""
    if _stale(comp):
        comp.status, comp.stage, comp.error = "failed", "Stopped", "This took too long and was stopped. Please add the creator again."
        db.commit()
    return comp


# --- the job ------------------------------------------------------------------------------------------------------------------------------------

def _set_stage(db: Session, comp: CompetitorCreator, stage: str, status: str | None = None) -> None:
    comp.stage = stage
    if status:
        comp.status = status
    db.commit()


def _fail(db: Session, comp_id: int, message: str) -> None:
    db.rollback()
    comp = db.get(CompetitorCreator, comp_id)
    if comp is not None:
        comp.status, comp.stage, comp.error = "failed", "Stopped", message
        db.commit()


_KNOWN_ROWS = text("""
    SELECT u.username, u.full_name, u.bio, u.followers_count, u.caption, u.post_timestamp AS timestamp, u.likes_count AS likes, u.comments_count AS comments,
           coalesce(u.video_view_count, u.video_play_count) AS views
    FROM instagram_users u WHERE u.user_type != 'commenter' AND lower(btrim(u.username)) = :handle
""")

_KNOWN_BRANDS = text("""
    SELECT b.id AS brand_raw_id, b.name, b.instagram_handle, count(*) AS posts FROM (
        SELECT p.brand_raw_id, p.post_url
        FROM instagram_users u JOIN instagram_posts p ON p.post_id = u.post_id
        WHERE u.user_type != 'commenter' AND lower(btrim(u.username)) = :handle AND p.sponsorship_confidence >= :min
        UNION
        SELECT t.brand_raw_id, t.post_url FROM test_creator_brand_partnership_posts t
        WHERE lower(btrim(t.creator_username)) = :handle AND t.sponsorship_confidence >= :min
    ) x JOIN brands_raw b ON b.id = x.brand_raw_id
    GROUP BY b.id, b.name, b.instagram_handle ORDER BY posts DESC, b.id
""")

_KNOWN_STORED = text("SELECT engagement_rate, product_focused_pct, content_categories, niche FROM creator_niches WHERE lower(btrim(username)) = :handle LIMIT 1")


def _handle_of(instagram_handle: str | None) -> str:
    return (instagram_handle or "").rstrip("/").rsplit("/", 1)[-1].lstrip("@").lower()


def _fill_from_known_creator(db: Session, comp: CompetitorCreator, rows: list) -> None:
    """The account is a creator MAVLIT already holds in instagram_users: work its numbers out from those rows (no Apify run)."""
    handle = comp.instagram_handle
    followers = max((r.followers_count for r in rows if r.followers_count), default=None)
    posts = [{"likes": r.likes, "comments": r.comments, "views": r.views, "timestamp": r.timestamp} for r in rows]
    numbers = summarize_posts(posts, followers)
    stored = db.execute(_KNOWN_STORED, {"handle": handle}).first()
    brands = [{"brand_raw_id": b.brand_raw_id, "handle": _handle_of(b.instagram_handle), "name": b.name, "posts": b.posts}
              for b in db.execute(_KNOWN_BRANDS, {"handle": handle, "min": MIN_PARTNERSHIP_CONFIDENCE}).all()]
    mix = {"product_focused_pct": None, "content_categories": None}
    if stored is not None and stored.content_categories:
        mix = {"product_focused_pct": stored.product_focused_pct, "content_categories": stored.content_categories}
    else:
        newest = sorted((r for r in rows if r.caption), key=lambda r: r.timestamp or "", reverse=True)
        mix = estimate_mix(db, handle, stored.niche if stored is not None else None, next((r.bio for r in rows if r.bio), None), [r.caption for r in newest])
    comp.source = "instagram_users"
    comp.full_name = next((r.full_name for r in rows if r.full_name), None)
    comp.followers_count = followers
    comp.posts_analyzed = len(rows)
    comp.avg_likes, comp.avg_comments = numbers["avg_likes"], numbers["avg_comments"]
    comp.engagement_rate = float(stored.engagement_rate) if stored is not None and stored.engagement_rate is not None else numbers["engagement_rate"]
    comp.avg_reel_views, comp.posts_per_week = numbers["avg_reel_views"], numbers["posts_per_week"]
    comp.product_focused_pct, comp.content_categories = mix["product_focused_pct"], mix["content_categories"]
    comp.partnership_count = sum(b["posts"] for b in brands)
    comp.partnership_brands = brands


def _post_row(comp_id: int, item: dict) -> dict:
    from pipeline.enrichment.instagram_posts import _real_coauthors, _usernames_only   # lazy: the scraper stack
    from pipeline.enrichment.instagram_users import _post_url
    return {
        "competitor_creator_id": comp_id, "post_id": str(item.get("id")), "post_url": _post_url(item), "post_type": item.get("type"),
        "timestamp": item.get("timestamp"), "caption": item.get("caption"), "likes_count": item.get("likesCount"), "comments_count": item.get("commentsCount"),
        "video_view_count": item.get("videoViewCount"), "video_play_count": item.get("videoPlayCount"), "paid_partnership": item.get("paidPartnership"),
        "mentions": item.get("mentions"), "tagged_users": _usernames_only(item.get("taggedUsers")), "coauthor_producers": _usernames_only(_real_coauthors(item, "")),
    }


def _detect_partnerships(db: Session, comp: CompetitorCreator, raw_posts: list[dict]) -> list[dict]:
    """
    Ask the brand_check LLM (the prompt content_creator_re.py uses) which accounts a post names are real brands. Every confirmed brand that has no row in
    brands_raw gets a bare one. Returns one dict per post with a confirmed brand: brand_raw_id, brand_handle, brand_name.
    """
    from pipeline.enrichment_re.content_creator_re import _check_post_for_brands, _get_or_create_brand_id, _mark_brand_referral   # lazy: the script's module
    from pipeline.db import BrandRaw
    found: list[dict] = []
    for item in raw_posts:
        if not item.get("id"):
            continue
        db.commit()   # no lock held across the LLM call
        matches = _check_post_for_brands(db, comp.instagram_handle, comp.full_name, item)
        for match in matches[:1]:   # one brand per post is the partnership; a second tagged brand is not counted twice
            brand_id = _get_or_create_brand_id(db, match["username"])
            if not brand_id:
                continue
            _mark_brand_referral(db, brand_id, match.get("has_referral_code", False))
            brand = db.get(BrandRaw, brand_id)
            found.append({"post_id": str(item["id"]), "brand_raw_id": brand_id, "brand_handle": match["username"], "brand_name": brand.name if brand is not None else None})
    return found


def _fill_from_apify(db: Session, comp: CompetitorCreator, creator_id: int) -> bool:
    """Read the competitor's latest posts with Apify. False when the account has nothing to read (it does not exist, or is private): the caller drops it."""
    from pipeline.enrichment.instagram_users import _profile_from_posts, _scrape_posts   # lazy: the scraper stack
    from pipeline.helpers.db import upsert_rows
    if not APIFY_TOKEN:
        raise RuntimeError("Competitor lookup is not available right now. Please try again later.")
    _set_stage(db, comp, "Reading the creator's latest Instagram posts (this can take a minute)", "running")
    with request_path(), llm_usage_scope(creator_id, "competitor"):   # a person is waiting: Apify is cut off at 90 s; the run is recorded for this user
        raw_posts = _scrape_posts(comp.instagram_handle, POSTS_TO_READ)
    if raw_posts is None:
        raise RuntimeError("Could not reach this Instagram account right now. Please try again.")
    raw_posts = [p for p in raw_posts if isinstance(p, dict) and not p.get("error") and p.get("id")]
    if not raw_posts:
        return False
    profile = _profile_from_posts(raw_posts)
    comp.full_name = profile.get("fullName")
    comp.followers_count = profile.get("followersCount")
    rows = [_post_row(comp.id, p) for p in raw_posts]
    upsert_rows(db, CompetitorCreatorPost, rows, ["post_id"])
    numbers = summarize_posts([{"likes": p.get("likesCount"), "comments": p.get("commentsCount"), "views": p.get("videoViewCount") or p.get("videoPlayCount"),
                                "timestamp": p.get("timestamp")} for p in raw_posts], comp.followers_count)

    _set_stage(db, comp, "Checking the posts for brand partnerships")
    with llm_usage_scope(creator_id, "competitor"):
        found = _detect_partnerships(db, comp, raw_posts)
        by_post = {f["post_id"]: f for f in found}
        for row in rows:
            hit = by_post.get(row["post_id"])
            if hit:
                db.query(CompetitorCreatorPost).filter(CompetitorCreatorPost.post_id == row["post_id"]).update({"brand_raw_id": hit["brand_raw_id"], "brand_handle": hit["brand_handle"]})
        db.commit()
        _set_stage(db, comp, "Writing the content mix")
        mix = estimate_mix(db, comp.instagram_handle, None, profile.get("biography"), [p.get("caption") for p in raw_posts])

    comp.source = "apify"
    comp.posts_analyzed = len(raw_posts)
    comp.avg_likes, comp.avg_comments, comp.engagement_rate = numbers["avg_likes"], numbers["avg_comments"], numbers["engagement_rate"]
    comp.avg_reel_views, comp.posts_per_week = numbers["avg_reel_views"], numbers["posts_per_week"]
    comp.product_focused_pct, comp.content_categories = mix["product_focused_pct"], mix["content_categories"]
    comp.partnership_count = len(found)
    comp.partnership_brands = partnership_brands(found)
    return True


def run_competitor_job(competitor_id: int, creator_id: int) -> None:
    """Background: fill the competitor in (see the module docstring). Never raises."""
    with BatchSessionLocal() as db:
        comp = db.get(CompetitorCreator, competitor_id)
        if comp is None or comp.status != "queued":
            return
        try:
            _set_stage(db, comp, "Checking what we know about the creator", "running")
            rows = db.execute(_KNOWN_ROWS, {"handle": comp.instagram_handle}).all()
            if rows:
                with llm_usage_scope(creator_id, "competitor"):
                    _set_stage(db, comp, "Reading what MAVLIT already knows about the creator")
                    _fill_from_known_creator(db, comp, rows)
            elif not _fill_from_apify(db, comp, creator_id):
                # no such account (or private, or empty): take the competitor back out, so a wrong handle leaves nothing behind
                # (the page, which was waiting for it, sees it gone from the user's list and says the account was not found)
                db.query(CompetitorCreator).filter(CompetitorCreator.id == comp.id).delete(synchronize_session=False)
                db.commit()
                logger.info("Competitor @%s: no posts - removed", comp.instagram_handle)
                return
            comp = db.get(CompetitorCreator, competitor_id)
            comp.status, comp.stage, comp.error = "ready", "Ready", None
            db.commit()
        except LLMUnavailableError:
            logger.exception("Competitor job %s: OpenAI unavailable", competitor_id)
            _fail(db, competitor_id, "The AI service is unavailable right now. Please try again in a few minutes.")
        except RuntimeError as exc:   # a message written for the user (see _fill_from_apify)
            _fail(db, competitor_id, str(exc))
        except Exception:
            logger.exception("Competitor job %s failed", competitor_id)
            _fail(db, competitor_id, "Something went wrong while reading this creator. Please try again.")
