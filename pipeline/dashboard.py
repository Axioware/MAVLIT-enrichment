"""
pipeline/dashboard.py

Aggregates the creator-facing dashboard overview: live match count, active
pitches, saved brands, and verified contacts across brands the creator has
actually engaged with (saved or pitched) — not a platform-wide total.
"""

from sqlalchemy.orm import Session

from pipeline.db import BrandContact, CreatorProfile, Pitch, SavedBrand
from pipeline.matching.matcher import get_matches

# Nothing sets these yet (no status-update endpoint exists), but "active"
# is defined against this set now so the definition doesn't need to change
# once one does.
_TERMINAL_PITCH_STATUSES = {"closed_won", "closed_lost", "declined"}

_MATCH_COUNT_LIMIT = 100


def get_dashboard_summary(db: Session, creator: CreatorProfile) -> dict:
    brand_matches = len(get_matches(db, creator.id, limit=_MATCH_COUNT_LIMIT))

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
