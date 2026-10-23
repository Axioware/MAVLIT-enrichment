"""
pipeline/dashboard.py

Aggregates the creator-facing dashboard overview: live match count, active
pitches, saved brands, and the number of contacts (with an email) across the
creator's matched brands (the Matches v3 list) — not a platform-wide total.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from pipeline.db import BrandContact, BrandRaw, CreatorBrandLlmV3Ranking, CreatorProfile, Pitch, SavedBrand
from pipeline.matching.matcher import apply_brand_hard_filters

# Nothing sets these yet (no status-update endpoint exists), but "active"
# is defined against this set now so the definition doesn't need to change
# once one does.
_TERMINAL_PITCH_STATUSES = {"closed_won", "closed_lost", "declined"}

def get_dashboard_summary(db: Session, creator: CreatorProfile) -> dict:
    # The brands shown on the Brand Matches page: the stored v3 ranking, re-checked against the
    # brand-only hard filters exactly like GET /matches/me/v3.
    matched_brands = apply_brand_hard_filters(
        db.query(CreatorBrandLlmV3Ranking.brand_raw_id)
        .join(BrandRaw, BrandRaw.id == CreatorBrandLlmV3Ranking.brand_raw_id)
        .filter(CreatorBrandLlmV3Ranking.creator_profile_id == creator.id)
    )
    matched_brand_ids = [bid for (bid,) in matched_brands.all()]
    brand_matches = len(matched_brand_ids)

    active_deals = (
        db.query(Pitch)
        .filter(Pitch.creator_profile_id == creator.id, ~Pitch.status.in_(_TERMINAL_PITCH_STATUSES))
        .count()
    )

    saved_brands = (
        db.query(SavedBrand).filter(SavedBrand.creator_profile_id == creator.id).count()
    )

    # Every email we hold for the matched brands (enriched contacts only), not just one per brand,
    # whether or not Apollo/Hunter verified it. Only contacts LinkedIn showed to have left the brand
    # (still_at_brand = false) are left out; unchecked (NULL) and confirmed (true) ones count.
    verified_contacts = 0
    if matched_brand_ids:
        verified_contacts = (
            db.query(BrandContact)
            .filter(
                BrandContact.brand_raw_id.in_(matched_brand_ids),
                BrandContact.is_enriched.is_(True),
                func.btrim(func.coalesce(BrandContact.email, "")) != "",   # enriched contacts without an email are not counted
                BrandContact.still_at_brand.isnot(False),
            )
            .count()
        )

    return {
        "brand_matches": brand_matches,
        "active_deals": active_deals,
        "saved_brands": saved_brands,
        "verified_contacts": verified_contacts,
    }
