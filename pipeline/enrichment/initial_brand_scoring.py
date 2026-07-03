"""
pipeline/enrichment/brand_scoring.py

Computes the initial brand score for each brand using the formula in scoring.md.
Scores are written to initial_brand_score (one row per brand, UPSERTed on re-run).

"""

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pipeline.db import (
    BrandRaw,
    BrandInstagramUser,
    InitialBrandScore,
    InstagramPost,
    MetaAd,
    TiktokPost,
    YoutubeSponsorship,
)

logger = logging.getLogger(__name__)


# 
# Date helpers
# 

def _days_since(date_str: str | None) -> int | None:
    """Return how many days ago an ISO date/datetime string was. None if unparseable."""
    if not date_str:
        return None
    now = datetime.now(timezone.utc)
    s = date_str.strip().replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (now - dt).days
        except ValueError:
            continue
    return None


# 
# Section 1 — Influencer Buying Activity (50 pts max)
# 

def _score_youtube(db: Session, brand_raw_id: int) -> tuple[int, dict[str, Any]]:
    """YouTube Sponsorships — 20 pts max."""
    rows = db.query(YoutubeSponsorship).filter(
        YoutubeSponsorship.brand_raw_id == brand_raw_id
    ).all()

    count = len(rows)
    details: dict[str, Any] = {"sponsorship_count": count}

    # 1a. Recency (0-8 pts) — most recent published_at
    days_list = [d for d in (_days_since(r.published_at) for r in rows) if d is not None]
    if days_list:
        min_days = min(days_list)
        details["recency_days"] = min_days
        if min_days <= 30:
            recency_pts = 8
        elif min_days <= 90:
            recency_pts = 6
        elif min_days <= 180:
            recency_pts = 4
        elif min_days <= 365:
            recency_pts = 2
        else:
            recency_pts = 0
    else:
        details["recency_days"] = None
        recency_pts = 0
    details["recency_pts"] = recency_pts

    # 1b. Count (0-7 pts)
    if count == 0:
        count_pts = 0
    elif count <= 2:
        count_pts = 3
    elif count <= 9:
        count_pts = 5
    else:
        count_pts = 7
    details["count_pts"] = count_pts

    # 1c. Creator audience reach (0-5 pts) — max subscriber_count
    subs = [r.subscriber_count for r in rows if r.subscriber_count is not None]
    max_subs = max(subs) if subs else 0
    details["max_subscriber_count"] = max_subs
    if max_subs >= 1_000_000:
        subscriber_pts = 5
    elif max_subs >= 100_000:
        subscriber_pts = 3
    elif max_subs >= 10_000:
        subscriber_pts = 2
    else:
        subscriber_pts = 0
    details["subscriber_pts"] = subscriber_pts

    total = min(recency_pts + count_pts + subscriber_pts, 20)
    details["total"] = total
    return total, details


def _score_instagram(db: Session, brand_raw_id: int) -> tuple[int, dict[str, Any]]:
    """Instagram Paid Partnerships — 20 pts max."""
    posts = db.query(InstagramPost).filter(
        InstagramPost.brand_raw_id == brand_raw_id
    ).all()

    details: dict[str, Any] = {}

    # 2a. Paid partnership posts (0-10 pts)
    paid_count = sum(1 for p in posts if p.paid_partnership)
    details["paid_partnership_posts"] = paid_count
    if paid_count == 0:
        paid_pts = 0
    elif paid_count <= 2:
        paid_pts = 5
    elif paid_count <= 5:
        paid_pts = 8
    else:
        paid_pts = 10
    details["paid_pts"] = paid_pts

    # 2b. Sponsors field populated (0-3 pts)
    sponsors_populated = any(p.sponsors for p in posts)
    details["sponsors_populated"] = sponsors_populated
    sponsors_pts = 3 if sponsors_populated else 0
    details["sponsors_pts"] = sponsors_pts

    # 2c. Creator network in brand_instagram_users (0-5 pts)
    creator_count = db.query(BrandInstagramUser).filter(
        BrandInstagramUser.brand_raw_id == brand_raw_id
    ).count()
    details["creator_network_count"] = creator_count
    if creator_count == 0:
        creator_pts = 0
    elif creator_count < 10:
        creator_pts = 3
    else:
        creator_pts = 5
    details["creator_pts"] = creator_pts

    # 2d. Collaboration signals (0-2 pts)
    collab_signals = any(
        (p.tagged_users or p.coauthor_producers) for p in posts
    )
    details["collab_signals"] = collab_signals
    collab_pts = 2 if collab_signals else 0
    details["collab_pts"] = collab_pts

    total = min(paid_pts + sponsors_pts + creator_pts + collab_pts, 20)
    details["total"] = total
    return total, details


def _score_tiktok(db: Session, brand_raw_id: int) -> tuple[int, dict[str, Any]]:
    """TikTok Sponsorships — 10 pts max."""
    sponsored_count = db.query(TiktokPost).filter(
        TiktokPost.brand_raw_id == brand_raw_id,
        (TiktokPost.is_sponsored == True) | (TiktokPost.is_ad == True),
    ).count()

    details: dict[str, Any] = {"sponsored_posts": sponsored_count}
    if sponsored_count == 0:
        total = 0
    elif sponsored_count < 3:
        total = 6
    else:
        total = 10
    details["total"] = total
    return total, details


# 
# Section 2 — Advertising Budget / Meta Ads (15 pts max)
# 

def _score_meta_ads(db: Session, brand_raw_id: int) -> tuple[int, dict[str, Any]]:
    """Meta Ads — 15 pts max."""
    ads = db.query(MetaAd).filter(MetaAd.brand_raw_id == brand_raw_id).all()
    details: dict[str, Any] = {}

    ad_count = len(ads)
    details["ad_count"] = ad_count

    # Volume (0-5 pts)
    if ad_count == 0:
        volume_pts = 0
    elif ad_count <= 4:
        volume_pts = 2
    elif ad_count <= 9:
        volume_pts = 3
    else:
        volume_pts = 5
    details["volume_pts"] = volume_pts

    # Active ads — end_date IS NULL (0-3 pts)
    active_count = sum(1 for a in ads if a.end_date is None)
    details["active_no_end_date"] = active_count
    if active_count == 0:
        active_pts = 0
    elif active_count <= 5:
        active_pts = 2
    else:
        active_pts = 3
    details["active_pts"] = active_pts

    # Recency — most recent start_date (0-3 pts)
    days_list = [d for d in (_days_since(a.start_date) for a in ads) if d is not None]
    if days_list:
        min_days = min(days_list)
        details["recency_days"] = min_days
        if min_days <= 30:
            recency_pts = 3
        elif min_days <= 90:
            recency_pts = 2
        elif min_days <= 180:
            recency_pts = 1
        else:
            recency_pts = 0
    else:
        details["recency_days"] = None
        recency_pts = 0
    details["recency_pts"] = recency_pts

    # Spend — sum of spend.lower_bound across all ads (0-2 pts)
    spend_sum = 0
    for a in ads:
        if a.spend and isinstance(a.spend, dict):
            try:
                spend_sum += int(a.spend.get("lower_bound") or 0)
            except (ValueError, TypeError):
                pass
    details["spend_sum_lower"] = spend_sum
    if spend_sum == 0:
        spend_pts = 0
    elif spend_sum < 1000:
        spend_pts = 1
    else:
        spend_pts = 2
    details["spend_pts"] = spend_pts

    # Impressions — sum of impressions.lower_bound (0-1 pt)
    impressions_sum = 0
    for a in ads:
        if a.impressions and isinstance(a.impressions, dict):
            try:
                impressions_sum += int(a.impressions.get("lower_bound") or 0)
            except (ValueError, TypeError):
                pass
    details["impressions_sum_lower"] = impressions_sum
    impressions_pts = 1 if impressions_sum >= 100_000 else 0
    details["impressions_pts"] = impressions_pts

    # Platform coverage — both facebook AND instagram (0-1 pt)
    has_facebook = has_instagram_platform = False
    for a in ads:
        if a.publisher_platforms and isinstance(a.publisher_platforms, list):
            platforms = [str(p).lower() for p in a.publisher_platforms]
            if "facebook" in platforms:
                has_facebook = True
            if "instagram" in platforms:
                has_instagram_platform = True
    both_platforms = has_facebook and has_instagram_platform
    details["both_platforms"] = both_platforms
    platform_pts = 1 if both_platforms else 0
    details["platform_pts"] = platform_pts

    total = min(
        volume_pts + active_pts + recency_pts + spend_pts + impressions_pts + platform_pts,
        15,
    )
    details["total"] = total
    return total, details


# 
# Section 3 — Brand Scale & Legitimacy (25 pts max)
# 

def _score_legitimacy(brand: BrandRaw) -> tuple[int, dict[str, Any]]:
    """Brand Scale & Legitimacy — 25 pts max."""
    details: dict[str, Any] = {}

    # Tranco rank (0-10 pts)
    rank = brand.tranco_rank
    details["tranco_rank"] = rank
    if rank is None:
        tranco_pts = 0
    elif rank <= 10_000:
        tranco_pts = 10
    elif rank <= 50_000:
        tranco_pts = 7
    elif rank <= 100_000:
        tranco_pts = 5
    elif rank <= 500_000:
        tranco_pts = 3
    else:
        tranco_pts = 1
    details["tranco_pts"] = tranco_pts

    # E-commerce platform (0-8 pts, not additive — take the higher)
    details["is_shopify"]    = bool(brand.is_shopify)
    details["is_woocommerce"] = bool(brand.is_woocommerce)
    if brand.is_shopify:
        ecommerce_pts = 8
    elif brand.is_woocommerce:
        ecommerce_pts = 5
    else:
        ecommerce_pts = 0
    details["ecommerce_pts"] = ecommerce_pts

    # Social presence (0-7 pts)
    social_handles = []
    social_pts = 0
    if brand.instagram_handle:
        social_handles.append("instagram_handle")
        social_pts += 2
    if brand.tiktok_handle:
        social_handles.append("tiktok_handle")
        social_pts += 2
    if brand.youtube_channel_id:
        social_handles.append("youtube_channel_id")
        social_pts += 1
    if brand.facebook_page:
        social_handles.append("facebook_page")
        social_pts += 1
    if brand.twitter_handle:
        social_handles.append("twitter_handle")
        social_pts += 1
    details["social_handles"] = social_handles
    details["social_pts"]     = social_pts

    total = min(tranco_pts + ecommerce_pts + social_pts, 25)
    details["total"] = total
    return total, details


# 
# Section 4 — Contact Reachability (10 pts max)
# 

def _score_reachability(brand: BrandRaw) -> tuple[int, dict[str, Any]]:
    """Contact Reachability — 10 pts max."""
    details: dict[str, Any] = {}

    has_linkedin = bool(brand.linkedin_id)
    details["has_linkedin"] = has_linkedin
    linkedin_pts = 5 if has_linkedin else 0
    details["linkedin_pts"] = linkedin_pts

    has_fb_page_id = bool(brand.facebook_page_id)
    details["has_facebook_page_id"] = has_fb_page_id
    fb_pts = 3 if has_fb_page_id else 0
    details["fb_pts"] = fb_pts

    is_shopify = bool(brand.is_shopify)
    details["is_shopify"] = is_shopify
    shopify_pts = 2 if is_shopify else 0
    details["shopify_pts"] = shopify_pts

    total = min(linkedin_pts + fb_pts + shopify_pts, 10)
    details["total"] = total
    return total, details


# 
# Band classification
# 

def _band(score: int) -> str:
    if score >= 70:
        return "HOT"
    if score >= 50:
        return "WARM"
    if score >= 30:
        return "COOL"
    return "COLD"


# 
# Public API
# 

def score_brand(db: Session, brand_raw_id: int) -> dict[str, Any] | None:
    """
    Compute and UPSERT the score for a single brand.
    Returns the score row as a dict, or None if the brand doesn't exist.
    Safe to call multiple times — always overwrites the previous score.
    """
    brand = db.query(BrandRaw).filter(BrandRaw.id == brand_raw_id).first()
    if not brand:
        logger.warning("score_brand: brand_raw_id=%d not found", brand_raw_id)
        return None

    # Enrichment completeness (0-4)
    completeness = sum([
        bool(brand.youtube_checked),
        bool(brand.instagram_checked),
        bool(brand.tiktok_checked),
        bool(brand.meta_ads_fetched),
    ])

    yt_score,    yt_details    = _score_youtube(db, brand_raw_id)
    ig_score,    ig_details    = _score_instagram(db, brand_raw_id)
    tt_score,    tt_details    = _score_tiktok(db, brand_raw_id)
    meta_score,  meta_details  = _score_meta_ads(db, brand_raw_id)
    leg_score,   leg_details   = _score_legitimacy(brand)
    reach_score, reach_details = _score_reachability(brand)

    influencer_score   = yt_score + ig_score + tt_score   # max 50
    ad_spend_score     = meta_score                        # max 15
    legitimacy_score   = leg_score                         # max 25
    reachability_score = reach_score                       # max 10
    total_score        = influencer_score + ad_spend_score + legitimacy_score + reachability_score

    score_details = {
        "youtube":      yt_details,
        "instagram":    ig_details,
        "tiktok":       tt_details,
        "meta_ads":     meta_details,
        "legitimacy":   leg_details,
        "reachability": reach_details,
    }

    row = {
        "brand_raw_id":            brand_raw_id,
        "influencer_score":        influencer_score,
        "ad_spend_score":          ad_spend_score,
        "legitimacy_score":        legitimacy_score,
        "reachability_score":      reachability_score,
        "total_score":             total_score,
        "score_band":              _band(total_score),
        "enrichment_completeness": completeness,
        "score_details":           score_details,
        "scored_at":               datetime.now(timezone.utc),
    }

    stmt = (
        pg_insert(InitialBrandScore)
        .values(**row)
        .on_conflict_do_update(
            index_elements=["brand_raw_id"],
            set_={k: v for k, v in row.items() if k != "brand_raw_id"},
        )
    )
    db.execute(stmt)
    
    # Mark brand as scored
    brand.initial_brand_scored = True
    db.add(brand)
    db.commit()

    logger.info(
        "Scored '%s' (id=%d) → total=%d band=%s [infl=%d ads=%d leg=%d reach=%d] completeness=%d/4",
        brand.name, brand_raw_id,
        total_score, _band(total_score),
        influencer_score, ad_spend_score, legitimacy_score, reachability_score,
        completeness,
    )
    return row


def run_brand_scoring(db: Session, limit: int = 500) -> int:
    """
    Score all brands that have at least one enrichment step complete.
    Brands are scored regardless of enrichment_completeness; the completeness
    value in the output row lets callers filter before sending to Apollo.
    Returns number of brands scored.
    """
    brands = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.wikidata_enriched  == True,
            BrandRaw.shopify_checked    == True,
            BrandRaw.tranco_checked     == True,
            BrandRaw.meta_ads_fetched   == True,
            BrandRaw.youtube_checked    == True,
            BrandRaw.instagram_checked  == True,
            BrandRaw.tiktok_checked     == True,
            BrandRaw.twitter_checked    == True,
            BrandRaw.initial_brand_scored == False,
        )
        .limit(limit)
        .all()
    )

    if not brands:
        logger.info("Brand scoring: no fully-enriched brands to score")
        return 0

    logger.info("Brand scoring: scoring %d brands", len(brands))
    scored = 0
    for brand in brands:
        try:
            score_brand(db, brand.id)
            scored += 1
        except Exception:
            logger.exception("Brand scoring failed for brand_raw_id=%d", brand.id)

    logger.info("Brand scoring: %d/%d brands scored successfully", scored, len(brands))
    return scored
