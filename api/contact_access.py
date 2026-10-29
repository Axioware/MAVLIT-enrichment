"""
api/contact_access.py

Who may see a brand's contact people (name, email, title, LinkedIn): only a creator who has a reason to be looking at that brand, and
only for a limited number of distinct brands per day.

Entitled brands of a creator: the ones they saved, the ones in their AI matches (creator_brand_llm_v3_rankings), and the brands they added
themselves through "my own brand" (own_brand_jobs). For any other brand the contacts are simply left out (no error), including in a pitch.

The daily cap (CONTACT_REVEALS_PER_DAY, default 50; 0 turns revealing off) counts DISTINCT brands per creator per UTC day in the table
contact_reveals, so it holds across restarts and processes. Looking at the same brand again the same day costs nothing.
"""

import os
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pipeline.brand_detail import _top_contact
from pipeline.db import ContactReveal, CreatorBrandLlmV3Ranking, OwnBrandJob, SavedBrand

DEFAULT_REVEALS_PER_DAY = 50


def daily_cap() -> int:
    """CONTACT_REVEALS_PER_DAY; a non-numeric or negative value raises ValueError (it is read when the module is imported)."""
    raw = (os.environ.get("CONTACT_REVEALS_PER_DAY") or "").strip()
    if not raw:
        return DEFAULT_REVEALS_PER_DAY
    value = int(raw)
    if value < 0:
        raise ValueError(f"CONTACT_REVEALS_PER_DAY must be >= 0, got {value}")
    return value


daily_cap()   # fail at startup, not on the first request, when the setting is bad


def is_entitled(db: Session, creator_id: int, brand_id: int) -> bool:
    """True when the brand is saved by, matched to, or added by this creator."""
    return (
        db.query(SavedBrand.brand_raw_id).filter(SavedBrand.creator_profile_id == creator_id, SavedBrand.brand_raw_id == brand_id).first() is not None
        or db.query(CreatorBrandLlmV3Ranking.brand_raw_id).filter(
            CreatorBrandLlmV3Ranking.creator_profile_id == creator_id, CreatorBrandLlmV3Ranking.brand_raw_id == brand_id).first() is not None
        or db.query(OwnBrandJob.id).filter(OwnBrandJob.creator_profile_id == creator_id, OwnBrandJob.brand_raw_id == brand_id).first() is not None
    )


def reserve_reveal(db: Session, creator_id: int, brand_id: int) -> None:
    """Count this brand against today's cap (once per brand per day). Raises HTTP 429 when the cap is used up. Commits."""
    today = datetime.now(timezone.utc).date()
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 7_100_000_000 + creator_id})   # one creator's reveals one at a time
    already = db.query(ContactReveal.brand_raw_id).filter(
        ContactReveal.creator_profile_id == creator_id, ContactReveal.brand_raw_id == brand_id, ContactReveal.revealed_on == today).first()
    if already is None:
        used = db.query(func.count()).select_from(ContactReveal).filter(
            ContactReveal.creator_profile_id == creator_id, ContactReveal.revealed_on == today).scalar()
        cap = daily_cap()
        if used >= cap:
            db.rollback()
            raise HTTPException(
                status_code=429,
                detail=f"You have opened the contacts of {cap} different brands today, which is the daily limit. Try again tomorrow.",
                headers={"Retry-After": "3600"},
            )
        db.execute(pg_insert(ContactReveal).values(creator_profile_id=creator_id, brand_raw_id=brand_id, revealed_on=today).on_conflict_do_nothing())
    db.commit()


def allow_contacts(db: Session, creator_id: int, brand_id: int | None) -> bool:
    """
    Whether this creator may be shown the contact of this brand now. False for a custom brand, an unentitled brand (no error), and a
    brand that has no contact (nothing to reveal, nothing counted). Otherwise the reveal is counted; HTTP 429 over the daily cap.
    """
    if brand_id is None or not is_entitled(db, creator_id, brand_id):
        return False
    if _top_contact(db, brand_id) is None:
        return True
    reserve_reveal(db, creator_id, brand_id)
    return True
