"""
pipeline/brand_detail.py

Assembles a single brand's detail view for the creator-facing API
(api/brands.py). Reuses signals already computed elsewhere rather than
recomputing them:
  - _score_youtube/_score_instagram (initial_brand_scoring.py) for
    per-platform signal strength + last partnership date
  - BrandProfile for creator-tier fit, meta ads, sponsorship activity score
  - BrandContact for the top (marketing-first) contact
  - BrandNiche for the long, Shopify-scraped description
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from pipeline.db import BrandContact, BrandNiche, BrandProfile, BrandRaw
from pipeline.enrichment.initial_brand_scoring import _score_instagram, _score_youtube

# Buckets over the 0-25 point scale _score_youtube/_score_instagram already
# use (same scale compute_sponsorship_activity's yt/ig components are built
# from) — not a new set of thresholds.
def _bucket_signal(points: int) -> str:
    if points <= 0:
        return "not_available"
    if points <= 8:
        return "limited"
    if points <= 16:
        return "moderate"
    return "strong"


def _top_contact(db: Session, brand_raw_id: int) -> dict | None:
    contact = (
        db.query(BrandContact)
        .filter(
            BrandContact.brand_raw_id == brand_raw_id,
            BrandContact.is_enriched.is_(True),
            BrandContact.email.isnot(None),
        )
        .order_by(BrandContact.rank.asc())
        .first()
    )
    if not contact:
        return None
    return {"name": contact.full_name, "email": contact.email, "title": contact.title}


def get_brand_detail(db: Session, brand_raw_id: int) -> dict | None:
    brand = db.query(BrandRaw).filter(BrandRaw.id == brand_raw_id).first()
    if not brand:
        return None
    profile = db.query(BrandProfile).filter(BrandProfile.brand_raw_id == brand_raw_id).first()

    niche_row = (
        db.query(BrandNiche)
        .filter(BrandNiche.brand_raw_id == brand_raw_id, BrandNiche.description.isnot(None))
        .first()
    )
    long_description = niche_row.description if niche_row else brand.description

    yt_pts, yt_details = _score_youtube(db, brand_raw_id)
    ig_pts, ig_details = _score_instagram(db, brand_raw_id)

    recency_candidates = [
        d for d in (yt_details.get("recency_days"), ig_details.get("recency_days")) if d is not None
    ]
    last_partnership_date = None
    if recency_candidates:
        most_recent_days = min(recency_candidates)
        last_partnership_date = (
            (datetime.now(timezone.utc) - timedelta(days=most_recent_days)).date().isoformat()
        )

    followers_low_candidates = [
        v for v in (
            profile.insta_lowest if profile else None,
            profile.youtube_lowest if profile else None,
        ) if v is not None
    ]
    followers_high_candidates = [
        v for v in (
            profile.insta_highest if profile else None,
            profile.youtube_highest if profile else None,
        ) if v is not None
    ]

    return {
        "brand_id": brand.id,
        "name": brand.name,
        "short_bio": brand.description,
        "description": long_description,
        "niche": brand.niche,
        "creator_tier_fit": {
            "typical_tier": profile.typical_creator_tier if profile else None,
            "followers_low": min(followers_low_candidates) if followers_low_candidates else None,
            "followers_high": max(followers_high_candidates) if followers_high_candidates else None,
        },
        "last_partnership_date": last_partnership_date,
        "meta_ads_active": profile.meta_ads_active if profile else None,
        "platform_signals": {
            "instagram": _bucket_signal(ig_pts),
            "youtube": _bucket_signal(yt_pts),
        },
        "sponsorship_activity_score": profile.sponsorship_activity_score if profile else None,
        "top_contact": _top_contact(db, brand_raw_id),
    }
