"""
pipeline/enrichment/initial_brand_scoring.py

Computes a single 0-100 brand-quality score, independent of any specific
creator — the gate that decides which brands are worth running the rest of
the pipeline on. run_brand_scoring() only scores brands that have already
been through every enrichment step (wikidata/shopify/tranco/meta_ads/
youtube/instagram); brand_signals.py's Stage 1 in turn only processes
brands with total_score >= 50. Scores are written to initial_brand_score
(one row per brand, UPSERTed on re-run — safe to call repeatedly).

Formula — 3 independently-capped sections summed into total_score (0-100):

Section 1 — Influencer Buying Activity (60 pts: YouTube 25 + Instagram 35)
  YouTube (_score_youtube, from youtube_sponsorships rows for the brand):
    Recency (0-10 pts) — days since most recent published_at:
        <=30d: 10   <=90d: 8   <=180d: 5   <=365d: 3   else/none: 0
    Count (0-8 pts) — number of sponsorship rows:
        0: 0   1-2: 4   3-9: 6   10+: 8
    Creator reach (0-7 pts) — max subscriber_count seen:
        >=1,000,000: 7   >=100,000: 5   >=10,000: 3   else: 0
    total = min(recency_pts + count_pts + subscriber_pts, 25)

    Instagram (_score_instagram, from straight and reverse-engineering evidence):
        Recency (0-12.5 pts) — days since the most recent >=90-confidence sponsorship
        timestamp in instagram_posts or test_creator_brand_partnership_posts:
        <=60d: 12.5   <=120d: 10.5   <=180d: 7.5   <=365d: 5.5   else/none: 2.5
    Paid partnership posts (0-11.5 pts) — count of >=90-confidence rows across
        instagram_posts and test_creator_brand_partnership_posts:
        0: 2.5   1-2: 6.5   3: 8.5   4+: 11.5
    Creator network (0-6.5 pts) — distinct creator usernames from
        brand_instagram_users/instagram_users and the test partnership table:
        0: 2.5   1-3: 4.5   4+: 6.5
    Creator follower reach (0-4.5 pts) — any linked non-commenter creator with
        90,000 < followers_count < 900,000: 4.5 or 0
    total = min(sum of the above, 35)

Section 2 — Advertising Budget / Meta Ads (15 pts max)
    (_score_meta_ads, currently fixed at 15 while Meta Ads is disabled)
    total = 15

Section 3 — Brand Scale & Legitimacy (25 pts max)
  (_score_legitimacy)
    Tranco rank (0-10 pts): <=10,000: 10   <=50,000: 7   <=100,000: 5
        <=500,000: 3   else: 1   none: 0
    E-commerce platform (0-7 pts, NOT additive — higher of the two wins):
        is_shopify: 7   is_woocommerce: 5   neither: 4
    Social presence (0-8 pts, additive): instagram_handle +4, youtube_channel_id
        +2, facebook_page +2
    total = min(tranco_pts + ecommerce_pts + social_pts, 25)

total_score = influencer_score (Section 1) + ad_spend_score (Section 2)
                        + legitimacy_score (Section 3)

Band (_band): >=70 HOT   >=50 WARM   >=30 COOL   else COLD

    enrichment_completeness (0-2): count of youtube_checked/instagram_checked
    that are True — lets callers filter before sending a brand to Apollo.

NOTE: Section 3 (legitimacy) measures brand quality, not fit with any specific
creator — it is NOT part of
Stage 3 matching scoring (pipeline/matching/scoring.py), same reasoning as
that module excluding Tranco rank/HQ country/traffic tier from match scores.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pipeline.db import (
    BrandRaw,
    BrandInstagramUser,
    InitialBrandScore,
    InstagramPost,
    InstagramUser,
    MetaAd,
    TestCreatorBrandPartnershipPost,
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
    """YouTube Sponsorships — 25 pts max."""
    rows = db.query(YoutubeSponsorship).filter(
        YoutubeSponsorship.brand_raw_id == brand_raw_id
    ).all()

    count = len(rows)
    details: dict[str, Any] = {"sponsorship_count": count}

    # 1a. Recency (0-10 pts) — most recent published_at
    days_list = [d for d in (_days_since(r.published_at) for r in rows) if d is not None]
    if days_list:
        min_days = min(days_list)
        details["recency_days"] = min_days
        if min_days <= 30:
            recency_pts = 10
        elif min_days <= 90:
            recency_pts = 8
        elif min_days <= 180:
            recency_pts = 5
        elif min_days <= 365:
            recency_pts = 3
        else:
            recency_pts = 0
    else:
        details["recency_days"] = None
        recency_pts = 0
    details["recency_pts"] = recency_pts

    # 1b. Count (0-8 pts)
    if count == 0:
        count_pts = 0
    elif count <= 2:
        count_pts = 4
    elif count <= 7:
        count_pts = 6
    else:
        count_pts = 8
    details["count_pts"] = count_pts

    # 1c. Creator audience reach (0-7 pts) — max subscriber_count
    subs = [r.subscriber_count for r in rows if r.subscriber_count is not None]
    max_subs = max(subs) if subs else 0
    details["max_subscriber_count"] = max_subs
    if max_subs >= 700_000:
        subscriber_pts = 7
    elif max_subs >= 100_000:
        subscriber_pts = 5
    elif max_subs >= 10_000:
        subscriber_pts = 3
    else:
        subscriber_pts = 0
    details["subscriber_pts"] = subscriber_pts

    total = min(recency_pts + count_pts + subscriber_pts, 25)
    details["total"] = total
    return total, details


def _score_instagram(db: Session, brand_raw_id: int) -> tuple[int, dict[str, Any]]:
    """Instagram sponsorship evidence — 35 pts max."""
    window_start, window_end = _sponsorship_date_bounds()
    posts = db.query(InstagramPost).filter(
        InstagramPost.brand_raw_id == brand_raw_id,
        InstagramPost.sponsorship_confidence >= 90,
        InstagramPost.timestamp >= window_start,
        InstagramPost.timestamp < window_end,
    ).all()
    reverse_posts = db.query(TestCreatorBrandPartnershipPost).filter(
        TestCreatorBrandPartnershipPost.brand_raw_id == brand_raw_id,
        TestCreatorBrandPartnershipPost.sponsorship_confidence >= 90,
        TestCreatorBrandPartnershipPost.post_timestamp >= window_start,
        TestCreatorBrandPartnershipPost.post_timestamp < window_end,
    ).all()

    details: dict[str, Any] = {}

    # 2a. Recency (0-12.5 pts) — use both straight and reverse-engineering rows.
    timestamps = [p.timestamp for p in posts] + [p.post_timestamp for p in reverse_posts]
    days_list = [d for d in (_days_since(value) for value in timestamps) if d is not None]
    if days_list:
        min_days = min(days_list)
        details["recency_days"] = min_days
        if min_days <= 60:
            recency_pts = 12.5
        elif min_days <= 120:
            recency_pts = 10.5
        elif min_days <= 180:
            recency_pts = 7.5
        elif min_days <= 365:
            recency_pts = 5.5
        else:
            recency_pts = 0
    else:
        details["recency_days"] = None
        recency_pts = 0
    details["recency_pts"] = recency_pts

    # 2b. Paid partnership posts (0-11.5 pts) — both evidence tables are already
    # sponsorship-only inputs, but retain the confidence filter in this query.
    paid_count = len(posts) + len(reverse_posts)
    details["paid_partnership_posts"] = paid_count
    if paid_count == 0:
        paid_pts = 0
    elif paid_count <= 2:
        paid_pts = 6.5
    elif paid_count <= 3:
        paid_pts = 8.5
    else:
        paid_pts = 11.5
    details["paid_pts"] = paid_pts

    # 2c. Creator network (0-6.5 pts) — union direct links and reverse rows.
    linked_users = db.query(InstagramUser).join(
        BrandInstagramUser,
        BrandInstagramUser.instagram_user_id == InstagramUser.id,
    ).filter(
        BrandInstagramUser.brand_raw_id == brand_raw_id,
        InstagramUser.user_type != "commenter",
    ).all()
    creator_usernames = {user.username.casefold() for user in linked_users if user.username}
    creator_usernames.update(
        row.creator_username.casefold()
        for row in reverse_posts
        if row.creator_username
    )
    creator_count = len(creator_usernames)
    details["creator_network_count"] = creator_count
    if creator_count == 0:
        creator_pts = 0
    elif creator_count <= 3:
        creator_pts = 4.5
    else:
        creator_pts = 6.5
    details["creator_pts"] = creator_pts

    # 2d. Creator follower reach (0-4.5 pts). Reverse rows identify creators by
    # username; join them back to InstagramUser for the follower snapshot.
    reverse_usernames = {row.creator_username.casefold() for row in reverse_posts if row.creator_username}
    reverse_users = []
    if reverse_usernames:
        reverse_users = db.query(InstagramUser).filter(
            InstagramUser.user_type != "commenter",
            func.lower(InstagramUser.username).in_(reverse_usernames),
        ).all()
    follower_users = linked_users + reverse_users
    has_mid_reach_creator = any(
        user.followers_count is not None
        and 90_000 < user.followers_count < 900_000
        and (user in linked_users or user.username.casefold() in reverse_usernames)
        for user in follower_users
    )
    details["mid_reach_creator"] = has_mid_reach_creator
    follower_pts = 4.5 if has_mid_reach_creator else 0
    details["follower_pts"] = follower_pts

    total = min(recency_pts + paid_pts + creator_pts + follower_pts, 35)
    details["total"] = total
    return total, details


#
# Section 2 — Advertising Budget / Meta Ads (15 pts max)
# 

def _score_meta_ads(db: Session, brand_raw_id: int) -> tuple[int, dict[str, Any]]:
    """Meta Ads placeholder — fixed at 15 points while Meta Ads is disabled."""
    return 15, {"disabled": True, "total": 15}


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
        tranco_pts = 5
    elif rank <= 50_000:
        tranco_pts = 10
    elif rank <= 70_000:
        tranco_pts = 9
    elif rank <= 100_000:
        tranco_pts = 8
    elif rank <= 500_000:
        tranco_pts = 7
    else:
        tranco_pts = 6
    details["tranco_pts"] = tranco_pts

    # E-commerce platform (0-8 pts, not additive — take the higher)
    details["is_shopify"]    = bool(brand.is_shopify)
    details["is_woocommerce"] = bool(brand.is_woocommerce)
    if brand.is_shopify:
        ecommerce_pts = 7
    elif brand.is_woocommerce:
        ecommerce_pts = 5
    else:
        ecommerce_pts = 4
    details["ecommerce_pts"] = ecommerce_pts

    # Social presence (0-7 pts) — Instagram/YouTube/Facebook only
    social_handles = []
    social_pts = 0
    if brand.instagram_handle:
        social_handles.append("instagram_handle")
        social_pts += 4
    if brand.youtube_channel_id:
        social_handles.append("youtube_channel_id")
        social_pts += 2
    if brand.facebook_page:
        social_handles.append("facebook_page")
        social_pts += 2
    details["social_handles"] = social_handles
    details["social_pts"]     = social_pts

    total = min(tranco_pts + ecommerce_pts + social_pts, 25)
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


def _sponsorship_date_bounds() -> tuple[str, str]:
    """Return ISO text bounds for the rolling 365-day sponsorship window."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365)
    return start.strftime("%Y-%m-%d"), (end + timedelta(days=1)).strftime("%Y-%m-%d")


def _has_qualifying_sponsorship(db: Session, brand_raw_id: int) -> bool:
    """True when either sponsorship table has recent high-confidence evidence."""
    window_start, window_end = _sponsorship_date_bounds()

    instagram_match = db.query(InstagramPost.id).filter(
        InstagramPost.brand_raw_id == brand_raw_id,
        InstagramPost.sponsorship_confidence >= 90,
        InstagramPost.timestamp >= window_start,
        InstagramPost.timestamp < window_end,
    ).first()
    if instagram_match:
        return True

    creator_brand_match = db.query(TestCreatorBrandPartnershipPost.id).filter(
        TestCreatorBrandPartnershipPost.brand_raw_id == brand_raw_id,
        TestCreatorBrandPartnershipPost.sponsorship_confidence >= 90,
        TestCreatorBrandPartnershipPost.post_timestamp >= window_start,
        TestCreatorBrandPartnershipPost.post_timestamp < window_end,
    ).first()
    return creator_brand_match is not None


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
    if (
        brand.refferls
        or (
            brand.geo_reach_score is not None
            and not 0 <= brand.geo_reach_score <= 40
        )
        or not _has_qualifying_sponsorship(db, brand_raw_id)
    ):
        logger.info(
            "Skipping score for '%s' (id=%d): referral, geo reach over 40, or no qualifying sponsorship",
            brand.name, brand_raw_id,
        )
        return None

    # Enrichment completeness (0-3)
    completeness = sum([
        bool(brand.youtube_checked),
        bool(brand.instagram_checked),
    ])

    yt_score,    yt_details    = _score_youtube(db, brand_raw_id)
    ig_score,    ig_details    = _score_instagram(db, brand_raw_id)
    meta_score,  meta_details  = _score_meta_ads(db, brand_raw_id)
    leg_score,   leg_details   = _score_legitimacy(brand)

    influencer_score   = yt_score + ig_score   # max 50
    ad_spend_score     = meta_score            # max 15
    legitimacy_score   = leg_score              # max 25
    total_score        = influencer_score + ad_spend_score + legitimacy_score

    score_details = {
        "youtube":      yt_details,
        "instagram":    ig_details,
        "meta_ads":     meta_details,
        "legitimacy":   leg_details,
    }

    row = {
        "brand_raw_id":            brand_raw_id,
        "influencer_score":        influencer_score,
        "ad_spend_score":          ad_spend_score,
        "legitimacy_score":        legitimacy_score,
        "reachability_score":      0,
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
        "Scored '%s' (id=%d) → total=%d band=%s [infl=%d ads=%d leg=%d reach=%d] completeness=%d/3",
        brand.name, brand_raw_id,
        total_score, _band(total_score),
        influencer_score, ad_spend_score, legitimacy_score, 0,
        completeness,
    )
    return row


def run_brand_scoring(db: Session, limit: int = 500, brand_id: int | None = None) -> int:
    """
    Score only non-referral brands with geo reach null or 0-40 and a rolling-365-day Instagram partnership
    confidence of at least 90 in InstagramPost or the creator-brand test table.
    Brands are scored regardless of enrichment_completeness; the completeness
    value in the output row lets callers filter before sending to Apollo.
    Returns number of brands scored.

    Pass brand_id to target one specific brand directly — this bypasses the
    initial_brand_scored filters. The sponsorship and referral gate always
    applies, including when brand_id is supplied.
    """
    window_start, window_end = _sponsorship_date_bounds()
    if brand_id is not None:
        brands = db.query(BrandRaw).filter(BrandRaw.id == brand_id).all()
    else:
        brands = (
            db.query(BrandRaw)
            .filter(
                BrandRaw.has_official_website == True,
                BrandRaw.shopify_checked    == True,
                BrandRaw.tranco_checked     == True,
                BrandRaw.youtube_checked    == True,
                BrandRaw.instagram_checked  == True,
                BrandRaw.initial_brand_scored == False,
                BrandRaw.refferls.is_(False),
                (
                    BrandRaw.geo_reach_score.is_(None)
                    | BrandRaw.geo_reach_score.between(0, 40)
                ),
            )
            .filter(
                db.query(InstagramPost.id).filter(
                    InstagramPost.brand_raw_id == BrandRaw.id,
                    InstagramPost.sponsorship_confidence >= 90,
                    InstagramPost.timestamp >= window_start,
                    InstagramPost.timestamp < window_end,
                ).exists()
                |
                db.query(TestCreatorBrandPartnershipPost.id).filter(
                    TestCreatorBrandPartnershipPost.brand_raw_id == BrandRaw.id,
                    TestCreatorBrandPartnershipPost.sponsorship_confidence >= 90,
                    TestCreatorBrandPartnershipPost.post_timestamp >= window_start,
                    TestCreatorBrandPartnershipPost.post_timestamp < window_end,
                ).exists()
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
            if score_brand(db, brand.id) is not None:
                scored += 1
        except Exception:
            logger.exception("Brand scoring failed for brand_raw_id=%d", brand.id)

    logger.info("Brand scoring: %d/%d brands scored successfully", scored, len(brands))
    return scored
