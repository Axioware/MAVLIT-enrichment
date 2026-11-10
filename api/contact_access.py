"""
api/contact_access.py

Who may see a brand's contact people (name, email, title, LinkedIn): only a creator who has a reason to be looking at that brand. There is NO
limit on how many brands' contacts a creator can open.

Entitled brands of a creator: the ones they saved, the ones in their AI matches (creator_brand_llm_v3_rankings), and the brands they added
themselves through "my own brand" (own_brand_jobs). For any other brand the contacts are simply left out (no error), including in a pitch.

Each brand a creator opens is still recorded once per day in the table contact_reveals. That is a usage record for the admin and the usage
report only; it never refuses anything.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from pipeline.brand_detail import _top_contact
from pipeline.db import ContactReveal, CreatorBrandLlmV3Ranking, OwnBrandJob, SavedBrand

logger = logging.getLogger(__name__)

def is_entitled(db: Session, creator_id: int, brand_id: int) -> bool:
    """True when the brand is saved by, matched to, or added by this creator."""
    return (
        db.query(SavedBrand.brand_raw_id).filter(SavedBrand.creator_profile_id == creator_id, SavedBrand.brand_raw_id == brand_id).first() is not None
        or db.query(CreatorBrandLlmV3Ranking.brand_raw_id).filter(
            CreatorBrandLlmV3Ranking.creator_profile_id == creator_id, CreatorBrandLlmV3Ranking.brand_raw_id == brand_id).first() is not None
        or db.query(OwnBrandJob.id).filter(OwnBrandJob.creator_profile_id == creator_id, OwnBrandJob.brand_raw_id == brand_id).first() is not None
    )


def record_reveal(db: Session, creator_id: int, brand_id: int) -> None:
    """Note that this creator opened this brand's contacts today (once per brand per day). A usage record only: it never refuses and never raises."""
    try:
        db.execute(pg_insert(ContactReveal).values(creator_profile_id=creator_id, brand_raw_id=brand_id, revealed_on=datetime.now(timezone.utc).date())
                   .on_conflict_do_nothing())
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.warning("Could not record a contact reveal (usage record only)")


def allow_contacts(db: Session, creator_id: int, brand_id: int | None) -> bool:
    """
    Whether this creator may be shown the contact of this brand. False for a custom brand and for a brand that is not saved, matched or added
    by them (no error). Otherwise True, with no limit on how many brands; the opening is recorded when the brand has a contact.
    """
    if brand_id is None or not is_entitled(db, creator_id, brand_id):
        return False
    if _top_contact(db, brand_id) is not None:
        record_reveal(db, creator_id, brand_id)
    return True
