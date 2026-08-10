"""
pipeline/pitching.py

Generates a personal, relationship-building outreach pitch for a creator to
send a brand (existing brands_raw row, or a brand they typed in themselves),
and persists it as a Pitch row with status="proposal_sent".
"""

import logging

from sqlalchemy.orm import Session

from pipeline.brand_detail import _top_contact
from pipeline.db import BrandRaw, CreatorProfile, Pitch, Prompt
from pipeline.helpers.gpt_llm import call_gpt_text, fill_template
from pipeline.helpers.prompts import PITCH_DEFAULT_PROMPT, PITCH_PROMPT_NAME

logger = logging.getLogger(__name__)


def _get_pitch_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == PITCH_PROMPT_NAME).first()
    return row.content if row else PITCH_DEFAULT_PROMPT


def generate_pitch(
    db: Session,
    creator: CreatorProfile,
    *,
    is_custom: bool,
    brand_id: int | None,
    custom_brand_name: str | None,
    story: str,
    product_reference: str | None,
    past_brand_partnership: str | None,
    content_link: str | None,
) -> Pitch:
    """
    Raises ValueError if brand_id doesn't exist. Raises RuntimeError if the
    LLM call comes back empty — callers should not persist a blank pitch as
    "sent", so nothing is written to the DB in that case.
    """
    brand: BrandRaw | None = None
    contact: dict | None = None
    if is_custom:
        brand_name = custom_brand_name
        brand_niche = "unknown"
        brand_description = "none provided"
        contact_name, contact_title = "unknown", "unknown"
    else:
        brand = db.query(BrandRaw).filter(BrandRaw.id == brand_id).first()
        if not brand:
            raise ValueError(f"brand_id={brand_id} not found")
        brand_name = brand.name or brand.instagram_handle or "this brand"
        brand_niche = brand.niche or "unknown"
        brand_description = brand.description or "none provided"
        contact = _top_contact(db, brand_id)
        contact_name = contact["name"] if contact else "unknown"
        contact_title = contact["title"] if contact else "unknown"

    prompt = fill_template(
        _get_pitch_prompt(db),
        creator_name=creator.full_name or "unknown",
        creator_handle=creator.creator_handle or "unknown",
        creator_niche=creator.content_niche or "unknown",
        creator_follower_count=str(creator.follower_count) if creator.follower_count else "unknown",
        brand_name=brand_name or "unknown",
        brand_niche=brand_niche,
        brand_description=brand_description,
        contact_name=contact_name,
        contact_title=contact_title,
        story=story,
        product_reference=product_reference or "none provided",
        past_brand_partnership=past_brand_partnership or "none provided",
        content_link=content_link or "none provided",
    )
    pitch_text = call_gpt_text(prompt, context=f"pitch for creator_id={creator.id} brand={brand_name}")
    if not pitch_text:
        raise RuntimeError("Pitch generation failed — LLM returned no content")

    pitch = Pitch(
        creator_profile_id=creator.id,
        brand_raw_id=brand.id if brand else None,
        is_custom=is_custom,
        brand_name=brand_name,
        story=story,
        product_reference=product_reference,
        past_brand_partnership=past_brand_partnership,
        content_link=content_link,
        contact_name=contact["name"] if contact else None,
        contact_email=contact["email"] if contact else None,
        pitch_text=pitch_text,
        status="proposal_sent",
    )
    db.add(pitch)
    db.commit()
    db.refresh(pitch)
    logger.info("Pitch generated: creator_id=%d brand=%s pitch_id=%d", creator.id, brand_name, pitch.id)
    return pitch
