"""
pipeline/rate_intelligence.py

LLM-backed rate estimate for a creator/brand/deliverable combo, persisted
to rate_estimates for history.
"""

import logging

from sqlalchemy.orm import Session

from pipeline.db import BrandProfile, BrandRaw, CreatorProfile, Prompt, RateEstimate
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import RATE_INTEL_DEFAULT_PROMPT, RATE_INTEL_PROMPT_NAME

logger = logging.getLogger(__name__)


def _get_rate_intel_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == RATE_INTEL_PROMPT_NAME).first()
    return row.content if row else RATE_INTEL_DEFAULT_PROMPT


def estimate_rate(
    db: Session,
    creator: CreatorProfile,
    *,
    brand_id: int,
    platform: str,
    deliverable_type: str,
    exclusivity: str | None,
    usage: str | None,
    duration_months: int | None,
) -> RateEstimate:
    """
    Raises ValueError if brand_id doesn't exist. Raises RuntimeError if the
    LLM call comes back empty — nothing is persisted in that case.
    """
    brand = db.query(BrandRaw).filter(BrandRaw.id == brand_id).first()
    if not brand:
        raise ValueError(f"brand_id={brand_id} not found")
    profile = db.query(BrandProfile).filter(BrandProfile.brand_raw_id == brand_id).first()

    prompt = fill_template(
        _get_rate_intel_prompt(db),
        creator_tier=creator.creator_tier or "unknown",
        creator_follower_count=str(creator.follower_count) if creator.follower_count else "unknown",
        creator_primary_platform=creator.primary_platform or "unknown",
        brand_name=brand.name or "this brand",
        sponsorship_activity_score=str(profile.sponsorship_activity_score) if profile and profile.sponsorship_activity_score is not None else "unknown",
        meta_ads_active=str(profile.meta_ads_active) if profile else "unknown",
        platform=platform,
        deliverable_type=deliverable_type,
        exclusivity=exclusivity or "none specified",
        usage=usage or "none specified",
        duration_months=str(duration_months) if duration_months is not None else "one-off / not specified",
    )
    result = call_gpt_json(prompt, context=f"rate estimate for creator_id={creator.id} brand_id={brand_id}")
    if not isinstance(result, dict) or not result:
        raise RuntimeError("Rate estimation failed — LLM returned no content")

    estimate = RateEstimate(
        creator_profile_id=creator.id,
        brand_raw_id=brand_id,
        platform=platform,
        deliverable_type=deliverable_type,
        exclusivity=exclusivity,
        usage=usage,
        duration_months=duration_months,
        rate_min=result.get("rate_min"),
        rate_max=result.get("rate_max"),
        currency=result.get("currency"),
        reasoning=result.get("reasoning"),
    )
    db.add(estimate)
    db.commit()
    db.refresh(estimate)
    logger.info("Rate estimate: creator_id=%d brand_id=%d id=%d", creator.id, brand_id, estimate.id)
    return estimate
