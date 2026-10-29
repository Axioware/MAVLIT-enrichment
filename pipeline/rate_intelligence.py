"""
pipeline/rate_intelligence.py

LLM-backed rate estimate for one Instagram deliverable of a creator/brand deal (Deal Tools > Rate Intelligence),
persisted to rate_estimates for history.

What the LLM is given (prompt: rate_intelligence_estimate):
  - the creator's Instagram followers, niche and follower bucket, and their best results (the most views and
    the most likes on one post, from creator_instagram_posts);
  - the Deal context the creator picked: deliverable type, exclusivity, usage rights;
  - the brand's name and Instagram followers;
  - the creators the brand has paid (confirmed partnership posts, sponsorship confidence >= 90, from instagram_posts
    and test_creator_brand_partnership_posts) with their follower counts, niches and buckets, and the views and
    likes of that sponsored post;
  - rate data from pipeline/data/rate_buckets_with_views_by_niche.csv (self-reported rate cards of ~2,400 Instagram
    creators): the row for the creator's niche x follower bucket x deliverable (creators, price points, min, max,
    average, median, and the views of those creators), the same niche and deliverable across all buckets, and the
    same row for each partner creator;
  - the views check: a creator whose typical views (median of their last 20 posts) are well above their follower
    bucket's, and close to the next bucket's, is priced from the next bucket (views_adjustment).
"""

import csv
import logging
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from pipeline.db import BrandRaw, CreatorInstagramPost, CreatorProfile, Prompt, RateEstimate
from pipeline.helpers.db import escape_like
from pipeline.helpers.gpt_llm import call_gpt_json, capture_llm_usage, fill_template, llm_usage_scope
from pipeline.helpers.prompts import RATE_INTEL_DEFAULT_PROMPT, RATE_INTEL_PROMPT_NAME
from pipeline.pitching import _PARTNER_CREATORS_SQL, _brand_instagram_followers

logger = logging.getLogger(__name__)

MIN_PARTNERSHIP_CONFIDENCE = 90
PARTNER_CREATORS_MAX = 10
_DATA_FILE = Path(__file__).parent / "data" / "rate_buckets_with_views_by_niche.csv"

# The follower buckets of the rate data (lower end included), smallest first.
FOLLOWER_BUCKETS = [
    (5_000, "5k-25k"), (25_000, "25k-50k"), (50_000, "50k-100k"),
    (100_000, "100k-300k"), (300_000, "300k-600k"), (600_000, "600k+"),
]
# The views check (views_adjustment): the creator's typical views are the median views of their last
# VIEWS_POSTS_WINDOW posts, needing at least MIN_POSTS_WITH_VIEWS posts that have views. The check only applies to
# the deliverables below (Story and Live views are counted differently), and the creator moves to the next bucket's
# price once they are at least NEXT_BUCKET_SHARE of the way from their bucket's median views to the next one's.
VIEWS_POSTS_WINDOW = 20
MIN_POSTS_WITH_VIEWS = 5
VIEWS_DELIVERABLES = ("Reel", "Post")
NEXT_BUCKET_SHARE = 0.5
# Creator / brand niche (lower case) -> the niche label of the rate data.
_DATASET_NICHE = {"music": "Music & Dance", "dance": "Music & Dance", "beauty": "Beauty", "fitness": "Health & Fitness", "health": "Health & Fitness"}
# Deal Tools deliverable type (lower case) -> the deliverable label of the rate data.
_DATASET_DELIVERABLE = {"reel": "Reel", "story": "Story", "static post": "Post", "post": "Post", "instagram live": "Live", "live": "Live"}

# The three choices of Deal Tools > Exclusivity, spelled out for the LLM.
EXCLUSIVITY_TEXT = {
    "None": "None (the creator may work with competing brands)",
    "Narrow": "Narrow - the creator may not promote a competing version of the brand's specific product",
    "Broad": "Broad - the creator may not promote any competing brand in the brand's whole product category",
}

def _get_rate_intel_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == RATE_INTEL_PROMPT_NAME).first()
    return row.content if row else RATE_INTEL_DEFAULT_PROMPT


def _float(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


@lru_cache(maxsize=1)
def _rate_rows() -> tuple[dict, ...]:
    with open(_DATA_FILE, newline="", encoding="utf-8") as fh:
        rows = []
        for row in csv.DictReader(fh):
            rows.append({
                "niche": row["niche"], "bucket": row["follower_bucket"], "deliverable": row["deliverable"],
                "creators": int(row["creators_in_bucket"]), "points": int(row["price_points"]),
                "min": float(row["min_usd"]), "max": float(row["max_usd"]),
                "avg": float(row["avg_usd"]), "median": float(row["median_usd"]),
                "views_creators": int(row["views_creators"] or 0),
                "views_min": _float(row["views_min"]), "views_max": _float(row["views_max"]),
                "views_avg": _float(row["views_avg"]), "views_median": _float(row["views_median"]),
            })
    return tuple(rows)


def follower_bucket(followers: int | None) -> str | None:
    """The rate-data bucket of a follower count; None below the smallest bucket (5k) or when unknown."""
    if not followers or followers < FOLLOWER_BUCKETS[0][0]:
        return None
    label = FOLLOWER_BUCKETS[0][1]
    for start, name in FOLLOWER_BUCKETS:
        if followers >= start:
            label = name
    return label


def dataset_niche(*niches: str | None) -> str | None:
    """The first of the given niches (comma-separated values allowed) that the rate data covers."""
    for value in niches:
        for part in (value or "").split(","):
            mapped = _DATASET_NICHE.get(part.strip().lower())
            if mapped:
                return mapped
    return None


def dataset_deliverable(deliverable_type: str) -> str | None:
    return _DATASET_DELIVERABLE.get((deliverable_type or "").strip().lower())


def rate_row(niche: str | None, bucket: str | None, deliverable: str | None) -> dict | None:
    return next((r for r in _rate_rows() if (r["niche"], r["bucket"], r["deliverable"]) == (niche, bucket, deliverable)), None)


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _row_text(row: dict, deliverable_label: str) -> str:
    text_ = (f"{row['creators']} creators, {row['points']} price points: min {_money(row['min'])}, median {_money(row['median'])}, "
             f"average {_money(row['avg'])}, max {_money(row['max'])} per {deliverable_label}")
    if row.get("views_median"):
        text_ += f"; their median average views per post is {row['views_median']:,.0f}"
    return text_


def _rate_data_text(niche: str | None, bucket: str | None, deliverable: str | None, followers: int | None) -> tuple[str, str]:
    """(the creator's matching row, the ladder across buckets) as prompt text."""
    if not niche or not deliverable:
        why = "the creator's niche is not covered by the rate data (Music, Beauty, Fitness and Health are)" if not niche else "this deliverable type has no rate data"
        return f"No matching rate data: {why}.", "none"
    ladder_rows = [r for r in _rate_rows() if r["niche"] == niche and r["deliverable"] == deliverable]
    ladder_rows.sort(key=lambda r: [name for _, name in FOLLOWER_BUCKETS].index(r["bucket"]))
    ladder = "\n".join(
        f"- {r['bucket']}{'  <- the creator' if r['bucket'] == bucket else ''}: {_row_text(r, deliverable)}" for r in ladder_rows
    ) or "none"
    if bucket is None:
        smallest = ladder_rows[0] if ladder_rows else None
        row_text = (f"The creator has {followers:,} followers" if followers else "The creator's follower count is not known") + \
            f", which is below the smallest bucket (5k), so there is no exact row. " + \
            (f"The smallest bucket, {smallest['bucket']}, is the nearest reference: {_row_text(smallest, deliverable)}." if smallest else "")
        return row_text.strip(), ladder
    row = rate_row(niche, bucket, deliverable)
    if not row:
        return f"No rate data for {niche}, {bucket}, {deliverable}.", ladder
    return f"{niche}, {bucket} followers, {deliverable}: {_row_text(row, deliverable)}", ladder


def next_bucket(bucket: str | None) -> str | None:
    names = [name for _, name in FOLLOWER_BUCKETS]
    return names[names.index(bucket) + 1] if bucket in names[:-1] else None


def creator_typical_views(db: Session, creator: CreatorProfile) -> tuple[int | None, int]:
    """
    (median views, posts counted): the median views of the creator's last VIEWS_POSTS_WINDOW saved Instagram posts
    that have a view count. The median, not the average, so one viral post does not inflate it. None when fewer
    than MIN_POSTS_WITH_VIEWS of those posts have views.
    """
    views_expr = func.coalesce(CreatorInstagramPost.video_view_count, CreatorInstagramPost.video_play_count)
    latest = (db.query(views_expr).filter(CreatorInstagramPost.creator_profile_id == creator.id)
              .order_by(CreatorInstagramPost.timestamp.desc().nullslast()).limit(VIEWS_POSTS_WINDOW).all())
    views = sorted(v for (v,) in latest if v and v > 0)
    if len(views) < MIN_POSTS_WITH_VIEWS:
        return None, len(views)
    mid = len(views) // 2
    return int(views[mid] if len(views) % 2 else (views[mid - 1] + views[mid]) / 2), len(views)


def views_adjustment(niche: str | None, bucket: str | None, deliverable: str | None, creator_views: int | None) -> dict:
    """
    Price a creator whose views beat their follower bucket from the next bucket. Returns {"applied", "reason", ...}.

    The creator keeps their own bucket's price unless their views are above that bucket's median views AND at least
    NEXT_BUCKET_SHARE of the way to the next bucket's median views. Then the anchor is the next bucket's median price
    x (creator views / next bucket's median views): a little below the next bucket's price while the views are below
    its median, never above it, never below the creator's own bucket's price, and never more than one bucket up.
    """
    def no(reason: str) -> dict:
        return {"applied": False, "reason": reason}

    if deliverable not in VIEWS_DELIVERABLES:
        return no(f"the views check is only used for {' and '.join(VIEWS_DELIVERABLES)} rates")
    if not creator_views:
        return no(f"fewer than {MIN_POSTS_WITH_VIEWS} of the creator's last {VIEWS_POSTS_WINDOW} posts have a view count")
    own, nxt_name = rate_row(niche, bucket, deliverable), next_bucket(bucket)
    nxt = rate_row(niche, nxt_name, deliverable) if nxt_name else None
    if not own or not own["views_median"]:
        return no("no views data for the creator's follower bucket")
    base = {"creator_views": creator_views, "bucket": bucket, "bucket_median_views": own["views_median"], "bucket_median_price": own["median"]}
    if creator_views <= own["views_median"]:
        return {**base, "applied": False, "reason": "the creator's views are not above their follower bucket's median"}
    if not nxt or not nxt["views_median"] or nxt["views_median"] <= own["views_median"]:
        return {**base, "applied": False, "reason": "there is no higher bucket with views data to compare against"}
    share = (creator_views - own["views_median"]) / (nxt["views_median"] - own["views_median"])
    base |= {"next_bucket": nxt_name, "next_bucket_median_views": nxt["views_median"], "next_bucket_median_price": nxt["median"], "share": round(share, 2)}
    if share < NEXT_BUCKET_SHARE:
        return {**base, "applied": False, "reason": f"the creator's views are above their bucket's median but only {share:.0%} of the way to the next bucket's"}
    price = nxt["median"] * min(1.0, creator_views / nxt["views_median"])
    return {**base, "applied": True, "adjusted_median_price": round(max(price, own["median"])),
            "reason": f"the creator's views are {share:.0%} of the way from their bucket's median to the next bucket's"}


def _views_text(adj: dict, creator_views: int | None, posts: int, deliverable: str | None) -> str:
    if not adj.get("applied"):
        return "Not applied: " + adj["reason"] + ". Use the creator's own bucket row."
    return (f"APPLIED. The creator's typical views ({creator_views:,}, median of their last {posts} posts) are above the {adj['bucket']} median of "
            f"{adj['bucket_median_views']:,.0f} and {adj['share']:.0%} of the way to the {adj['next_bucket']} median of {adj['next_bucket_median_views']:,.0f}. "
            f"So centre the typical rate on {_money(adj['adjusted_median_price'])} per {deliverable} instead of the {adj['bucket']} median of "
            f"{_money(adj['bucket_median_price'])}: that is the {adj['next_bucket']} median of {_money(adj['next_bucket_median_price'])} scaled by "
            f"their views against that bucket's median views. Tell the creator this in a \"views\" adjustment.")


def partner_creators_all(db: Session, brand_id: int) -> list[dict]:
    """Up to PARTNER_CREATORS_MAX creators with a confirmed (>= 90) partnership post with the brand, strongest first."""
    # the pitch's query without its "same niche as the writer" filter: every creator with a confirmed partnership post with the brand
    rows = db.execute(text(_PARTNER_CREATORS_SQL), {"bid": brand_id, "min_conf": MIN_PARTNERSHIP_CONFIDENCE, "niches": [], "restrict_niches": False}).mappings().all()
    ranked = sorted((dict(r) for r in rows), key=lambda r: str(r["posted_at"] or ""), reverse=True)
    ranked.sort(key=lambda r: r["sponsorship_confidence"] or 0, reverse=True)
    return ranked[:PARTNER_CREATORS_MAX]


def _count(value: int | None) -> str:
    if value is None or value == 0:
        return "not recorded"
    return "hidden" if value < 0 else f"{value:,}"   # Instagram stores -1 when the creator hides the like count


def creator_best_posts(db: Session, creator: CreatorProfile) -> dict:
    """
    The creator's own best results from the posts MAVLIT saved for them (creator_instagram_posts): the most views
    on one post or reel and the most likes on one post, each with its type and date. Falls back to the views the
    creator typed on their profile when no saved post has views.
    """
    views_expr = func.coalesce(CreatorInstagramPost.video_view_count, CreatorInstagramPost.video_play_count)
    base = db.query(CreatorInstagramPost).filter(CreatorInstagramPost.creator_profile_id == creator.id)
    top_views = base.filter(views_expr > 0).order_by(views_expr.desc()).first()
    top_likes = base.filter(CreatorInstagramPost.likes_count > 0).order_by(CreatorInstagramPost.likes_count.desc()).first()

    def describe(post: CreatorInstagramPost | None, value: int | None) -> str:
        if post is None or not value:
            return "not recorded"
        kind = {"video": "reel/video", "image": "photo", "sidecar": "carousel"}.get((post.post_type or "").lower(), post.post_type or "post")
        return f"{value:,} ({kind}{', ' + str(post.timestamp)[:10] if post.timestamp else ''})"

    views = (top_views.video_view_count or top_views.video_play_count) if top_views else None
    views_text = describe(top_views, views)
    if views_text == "not recorded" and creator.instagram_highest_views:
        views_text = f"{creator.instagram_highest_views:,} (entered by the creator)"
    return {
        "views_text": views_text, "likes_text": describe(top_likes, top_likes.likes_count if top_likes else None),
        "views": views or creator.instagram_highest_views, "likes": top_likes.likes_count if top_likes else None,
    }


def _partner_text(creators: list[dict], deliverable: str | None) -> str:
    if not creators:
        return "none on file"
    lines = []
    for i, c in enumerate(creators, 1):
        followers = c.get("followers")
        bucket = follower_bucket(followers)
        niche = dataset_niche(c.get("niche"))
        parts = [f"@{c['username']}", f"{followers:,} followers" if followers else "followers unknown"]
        if c.get("niche"):
            parts.append(f"niche: {c['niche']}")
        parts.append(f"sponsorship confidence {c['sponsorship_confidence']}")
        if c.get("posted_at"):
            parts.append(f"sponsored post on {str(c['posted_at'])[:10]}")
        parts.append(f"that post: {_count(c.get('views'))} views, {_count(c.get('likes_count'))} likes")
        row = rate_row(niche, bucket, deliverable)
        if row:
            parts.append(f"rate data for their bucket ({bucket}, {niche}): median {_money(row['median'])}, "
                         f"min {_money(row['min'])}, max {_money(row['max'])} per {deliverable}")
        elif followers and bucket is None:
            parts.append("below the smallest rate-data bucket (5k)")
        lines.append(f"{i}. " + "; ".join(parts))
    return "\n".join(lines)


def _resolve_brand(db: Session, brand_id: int | None, brand_name: str | None) -> BrandRaw | None:
    if brand_id is not None:
        brand = db.query(BrandRaw).filter(BrandRaw.id == brand_id).first()
        if not brand:
            raise ValueError(f"brand_id={brand_id} not found")
        return brand
    name = (brand_name or "").strip()
    if not name:
        raise ValueError("brand_id or brand_name is required")
    return db.query(BrandRaw).filter(BrandRaw.name.ilike(escape_like(name), escape="\\")).order_by(BrandRaw.id).first()   # None: a brand outside the database


def _fill_llm_fields(estimate: RateEstimate, calls: list[dict]) -> None:
    """What the LLM call(s) used: model, raw answer, tokens, cost, time."""
    def total(key: str) -> int:
        return sum(int(c.get(key) or 0) for c in calls)
    costs = [c["cost_usd"] for c in calls if c.get("cost_usd") is not None]
    estimate.model = calls[-1]["model"] if calls else None
    estimate.output = "\n".join(str(c["output"]) for c in calls if c.get("output")) or None
    estimate.input_tokens, estimate.cached_tokens = total("prompt_tokens"), total("cached_tokens")
    estimate.output_tokens, estimate.reasoning_tokens = total("completion_tokens"), total("reasoning_tokens")
    estimate.cost_usd = sum(costs) if costs else None
    estimate.duration_ms = total("duration_ms") if calls else None


def _save_failed(db: Session, estimate: RateEstimate, calls: list[dict], error: str) -> None:
    """Keep a failed request as an error row (no rates). Never raises: logging must not hide the real error."""
    try:
        _fill_llm_fields(estimate, calls)
        estimate.status, estimate.error = "error", error
        db.add(estimate)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Rate estimate: could not save the failed request - %s", exc)


def estimate_rate(
    db: Session,
    creator: CreatorProfile,
    *,
    brand_id: int | None = None,
    brand_name: str | None = None,
    platform: str = "Instagram",
    deliverable_type: str,
    exclusivity: str | None = None,
    usage: str | None = None,
    duration_months: int | None = None,
) -> RateEstimate:
    """
    Raises ValueError if neither a brand id nor a brand name is given, or brand_id doesn't exist. Raises
    RuntimeError if the LLM call comes back empty - the failed request is kept as an error row (status "error",
    no rates) in rate_estimates, with its prompt and token usage.
    """
    brand = _resolve_brand(db, brand_id, brand_name)
    name = (brand.name if brand and brand.name else (brand_name or "").strip()) or "this brand"

    followers = creator.instagram_followers or creator.follower_count
    niche = dataset_niche(creator.instagram_primary_niche, creator.content_niche)
    bucket = follower_bucket(followers)
    deliverable = dataset_deliverable(deliverable_type)
    row_text, ladder_text = _rate_data_text(niche, bucket, deliverable, followers)
    partners = partner_creators_all(db, brand.id) if brand else []
    best = creator_best_posts(db, creator)
    typical_views, posts_counted = creator_typical_views(db, creator)
    views_adj = views_adjustment(niche, bucket, deliverable, typical_views)
    if views_adj["applied"]:
        row_text += f" Views-adjusted median for this creator: {_money(views_adj['adjusted_median_price'])} (see the VIEWS CHECK)."

    exclusivity_text = EXCLUSIVITY_TEXT.get(exclusivity or "", "none specified")
    if exclusivity in ("Narrow", "Broad"):
        exclusivity_text += f", for {duration_months} months" if duration_months else ", duration not specified"
    prompt = fill_template(
        _get_rate_intel_prompt(db),
        creator_followers=f"{followers:,}" if followers else "unknown",
        creator_niche=(creator.instagram_primary_niche or creator.content_niche or "unknown"),
        creator_bucket=bucket or "below the smallest bucket (5k) or unknown",
        creator_highest_views=best["views_text"],
        creator_highest_likes=best["likes_text"],
        creator_typical_views=(f"{typical_views:,} (median of their last {posts_counted} posts with views)" if typical_views else "not enough saved posts with views"),
        views_adjustment=_views_text(views_adj, typical_views, posts_counted, deliverable),
        deliverable_type=deliverable_type,
        exclusivity=exclusivity_text,
        usage=usage or "none specified",
        brand_name=name,
        brand_followers=_brand_instagram_followers(db, brand.id) if brand else "unknown (the brand is not in MAVLIT's database)",
        partner_creators=_partner_text(partners, deliverable),
        data_creators="about 2,400",
        rate_row=row_text,
        rate_ladder=ladder_text,
    )
    estimate = RateEstimate(
        creator_profile_id=creator.id, creator_handle=(creator.instagram_handle or creator.creator_handle or None),
        brand_raw_id=brand.id if brand else None, brand_name=name, platform=platform,
        deliverable_type=deliverable_type, exclusivity=exclusivity, usage=usage, duration_months=duration_months,
        prompt=prompt, status="ok",
    )
    try:
        with llm_usage_scope(creator.id, "rate_estimate"), capture_llm_usage() as calls:
            result = call_gpt_json(prompt, context=f"rate estimate for creator_id={creator.id} brand={name!r}")
    except Exception as exc:   # OpenAI unavailable: kept as an error row, then raised as before
        _save_failed(db, estimate, [], f"{type(exc).__name__}: {exc}")
        raise
    if not isinstance(result, dict) or not result:
        _save_failed(db, estimate, calls, "The LLM returned no usable JSON")
        raise RuntimeError("Rate estimation failed - LLM returned no content")

    def whole(key: str) -> int | None:
        try:
            return max(0, round(float(result.get(key))))
        except (TypeError, ValueError):
            return None
    low, typical, high = whole("rate_min"), whole("rate_typical"), whole("rate_max")
    if low is not None and high is not None and low > high:
        low, high = high, low
    if typical is not None and low is not None and high is not None:
        typical = min(max(typical, low), high)
    confidence = str(result.get("confidence") or "").strip().lower()
    adjustments = [a for a in (result.get("adjustments") or []) if isinstance(a, dict) and a.get("effect")] if isinstance(result.get("adjustments"), list) else []

    if views_adj["applied"] and not any(str(a.get("factor")).strip().lower() == "views" for a in adjustments):   # the LLM skipped it
        adjustments.insert(0, {"factor": "views", "effect": (
            f"Your typical views ({typical_views:,}) are above the {bucket} median of {views_adj['bucket_median_views']:,.0f} and "
            f"{views_adj['share']:.0%} of the way to the {views_adj['next_bucket']} median of {views_adj['next_bucket_median_views']:,.0f}, "
            f"so the rate is based on the {views_adj['next_bucket']} bucket, scaled to your views.")})

    _fill_llm_fields(estimate, calls)
    estimate.rate_min, estimate.rate_typical, estimate.rate_max = low, typical, high
    estimate.currency = result.get("currency") or "USD"
    estimate.confidence = confidence if confidence in ("low", "medium", "high") else None
    estimate.reasoning = result.get("reasoning")
    estimate.details = {
        "basis": result.get("basis"),
        "adjustments": [{"factor": str(a.get("factor") or ""), "effect": str(a["effect"])} for a in adjustments],
        "creator_niche": niche, "follower_bucket": bucket, "creator_followers": followers,
        "creator_highest_views": best["views"], "creator_highest_likes": best["likes"],
        "data_row": rate_row(niche, bucket, deliverable),
        "creator_typical_views": typical_views, "views_adjustment": views_adj,
        "brand_followers": _brand_instagram_followers(db, brand.id) if brand else None,
        "partner_creators": [
            {"username": c["username"], "followers": c.get("followers"), "niche": c.get("niche"), "confidence": c.get("sponsorship_confidence"),
             "post_views": c.get("views"), "post_likes": c.get("likes_count")}
            for c in partners
        ],
    }
    db.add(estimate)
    db.commit()
    db.refresh(estimate)
    logger.info("Rate estimate: creator_id=%d brand=%r id=%d", creator.id, name, estimate.id)
    return estimate
