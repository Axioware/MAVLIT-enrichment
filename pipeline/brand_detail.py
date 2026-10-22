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

from sqlalchemy import func
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


def _contact_dict(contact: BrandContact) -> dict:
    return {
        "name": contact.full_name,
        "email": contact.email,
        "title": contact.title,
        "apollo_verified": (contact.email_status or "").lower() == "verified",
        "hunter_verified": (contact.hunter_status or "").lower() == "valid",
        "still_at_brand": contact.still_at_brand,
    }


def _contacts(db: Session, brand_raw_id: int) -> tuple[dict | None, list[dict]]:
    """
    The contact shown first plus the brand's other contacts.

    Only enriched contacts with an email are considered. The first is the best-ranked contact (rank 1
    = best) of the strictest group that has anyone, relaxing one condition at a time:
      1. email verified by Apollo AND by Hunter AND still at the brand
      2. verified by Apollo and Hunter (still_at_brand dropped)
      3. verified by Apollo (Hunter dropped)
      4. any email (Apollo verification dropped)
    The others (verified or not), best rank first, are what "view more" lists.
    """
    contacts = (
        db.query(BrandContact)
        .filter(
            BrandContact.brand_raw_id == brand_raw_id,
            BrandContact.is_enriched.is_(True),
            func.btrim(func.coalesce(BrandContact.email, "")) != "",
        )
        .order_by(BrandContact.rank.asc().nulls_last(), BrandContact.id.asc())
        .all()
    )
    if not contacts:
        return None, []
    rows = [(c, _contact_dict(c)) for c in contacts]
    tiers = (
        lambda d: d["still_at_brand"] is True and d["apollo_verified"] and d["hunter_verified"],
        lambda d: d["apollo_verified"] and d["hunter_verified"],
        lambda d: d["apollo_verified"],
        lambda d: True,
    )
    for tier in tiers:
        top = next((row for row in rows if tier(row[1])), None)
        if top:
            break
    return top[1], [d for c, d in rows if c is not top[0]]


def _top_contact(db: Session, brand_raw_id: int) -> dict | None:
    """The contact shown first for a brand (also used to address a pitch)."""
    return _contacts(db, brand_raw_id)[0]


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

    top_contact, other_contacts = _contacts(db, brand_raw_id)
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
        "top_contact": top_contact,
        "other_contacts": other_contacts,
    }
