"""
pipeline/dashboard.py

Aggregates the creator-facing dashboard overview: live match count, active
pitches, saved brands, and verified contacts across brands the creator has
actually engaged with (saved or pitched) — not a platform-wide total.
"""

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
    brand_matches = (
        apply_brand_hard_filters(
            db.query(CreatorBrandLlmV3Ranking.brand_raw_id)
            .join(BrandRaw, BrandRaw.id == CreatorBrandLlmV3Ranking.brand_raw_id)
            .filter(CreatorBrandLlmV3Ranking.creator_profile_id == creator.id)
        ).count()
    )

    active_deals = (
        db.query(Pitch)
        .filter(Pitch.creator_profile_id == creator.id, ~Pitch.status.in_(_TERMINAL_PITCH_STATUSES))
        .count()
    )

    saved_brands = (
        db.query(SavedBrand).filter(SavedBrand.creator_profile_id == creator.id).count()
    )

    relevant_brand_ids = {
        bid for (bid,) in db.query(SavedBrand.brand_raw_id).filter(SavedBrand.creator_profile_id == creator.id).all()
    }
    relevant_brand_ids |= {
        bid for (bid,) in db.query(Pitch.brand_raw_id)
        .filter(Pitch.creator_profile_id == creator.id, Pitch.brand_raw_id.isnot(None))
        .all()
    }
    verified_contacts = 0
    if relevant_brand_ids:
        verified_contacts = (
            db.query(BrandContact)
            .filter(BrandContact.brand_raw_id.in_(relevant_brand_ids), BrandContact.is_enriched.is_(True))
            .count()
        )

    return {
        "brand_matches": brand_matches,
        "active_deals": active_deals,
        "saved_brands": saved_brands,
        "verified_contacts": verified_contacts,
    }
