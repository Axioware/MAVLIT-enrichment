"""
pipeline/enrichment/creator_signals.py

Stage 2 of the creator-brand matching algorithm: creator-profile setup.
Runs once per creator, on profile submit/edit — never on a schedule, unlike
the brand-side pipelines in this package. Computes and caches:

  1) creator_tier       — bucketed from follower_count (pipeline.helpers.creator_tier,
                           same nano/micro/macro/mega buckets used on the brand side)
  2) content_tags       — LLM-extracted content tags + audience-value keywords,
                           from niche/sub_niches/content_description
  3) embedding + embedding_text — prose built the same way as
                           build_brand_embedding_text() in brand_signals.py,
                           embedded via the same Mistral mistral-embed model
                           (pipeline.helpers.llm.embed_text) so creator and
                           brand vectors are directly comparable by cosine
                           similarity in Stage 3.

None of this recomputes unless the creator edits their profile — there's no
run_*(db, limit=...) batch entry point here like the brand-side modules,
just compute_creator_signals(db, creator_id) called directly from the
PUT /creator-profile/me route.
"""

import logging

from sqlalchemy.orm import Session

from pipeline.db import CreatorProfile, Prompt
from pipeline.helpers.creator_tier import bucket_creator_tier
from pipeline.helpers.llm import call_mistral_json, embed_text, fill_template
from pipeline.helpers.prompts import TAGS_PROMPT_NAME, TAGS_DEFAULT_PROMPT

logger = logging.getLogger(__name__)

#  Prompt helpers

def _get_tags_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == TAGS_PROMPT_NAME).first()
    return row.content if row else TAGS_DEFAULT_PROMPT


def _extract_content_tags(db: Session, profile: CreatorProfile) -> list[str]:
    """
    Merges content_tags + audience_value_keywords into one flat list — Stage 3
    only consumes a single content_tags field (no separate downstream use for
    the two categories), so keeping one column avoids an unused split.
    Returns [] if there's nothing to extract from, or on any LLM failure.
    """
    if not profile.content_niche and not profile.content_description:
        return []

    prompt = fill_template(
        _get_tags_prompt(db),
        niche=profile.content_niche or "unknown",
        sub_niches=", ".join(profile.sub_niches) if profile.sub_niches else "none provided",
        content_description=profile.content_description or "none provided",
    )
    result = call_mistral_json(prompt, context=f"creator tags for creator_id={profile.id}")
    if not isinstance(result, dict):
        return []

    tags = result.get("content_tags") or []
    keywords = result.get("audience_value_keywords") or []
    return [t for t in (tags + keywords) if isinstance(t, str) and t.strip()]


#  Embedding text

def _gender_skew_phrase(male_pct: float | None, female_pct: float | None) -> str | None:
    if male_pct is not None and male_pct >= 0.6:
        return "audience skews mostly male"
    if female_pct is not None and female_pct >= 0.6:
        return "audience skews mostly female"
    if male_pct is not None or female_pct is not None:
        return "audience is gender-balanced"
    return None


def _top_countries_phrase(top_countries: list | None, n: int = 3) -> str | None:
    if not top_countries:
        return None
    names = [c["country"] for c in top_countries[:n] if c.get("country")]
    return ", ".join(names) if names else None


def build_creator_embedding_text(profile: CreatorProfile) -> str:
    """
    Build the prose text that gets embedded for semantic creator matching.
    Same style as build_brand_embedding_text() in brand_signals.py — only
    includes a clause for fields that are actually known.
    """
    parts = [profile.creator_handle or profile.full_name or "Creator"]

    if profile.content_niche:
        parts.append(f"creates {profile.content_niche} content")
    if profile.sub_niches:
        parts.append(f"covering {', '.join(profile.sub_niches)}")
    if profile.content_description:
        parts.append(profile.content_description.strip())
    if profile.content_tags:
        parts.append(f"known for {', '.join(profile.content_tags)}")
    if profile.primary_platform:
        parts.append(f"primarily active on {profile.primary_platform}")
    if profile.creator_tier:
        parts.append(f"{profile.creator_tier}-tier creator")

    gender_phrase = _gender_skew_phrase(profile.audience_gender_male_pct, profile.audience_gender_female_pct)
    if gender_phrase:
        parts.append(gender_phrase)

    if profile.audience_age_min is not None and profile.audience_age_max is not None:
        parts.append(f"audience aged {profile.audience_age_min}-{profile.audience_age_max}")
    elif profile.audience_age_bracket:
        parts.append(f"audience aged {profile.audience_age_bracket}")

    countries = _top_countries_phrase(profile.audience_top_countries)
    if countries:
        parts.append(f"top audience countries: {countries}")

    return ". ".join(parts) + "."


def compute_creator_signals(db: Session, creator_id: int) -> dict | None:
    """
    Full Stage 2 pipeline for one creator: buckets tier from follower_count,
    LLM-extracts content tags, builds embedding text, embeds via Mistral.
    Writes creator_tier, content_tags, embedding, embedding_text to
    creator_profiles. Returns None if the creator doesn't exist.

    Safe to call on its own (e.g. from a background task) without any
    other setup having run first — recomputes creator_tier itself rather
    than assuming a caller already did it.
    """
    profile = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
    if not profile:
        logger.warning("Creator signals: creator_id=%d not found", creator_id)
        return None

    profile.creator_tier = bucket_creator_tier(profile.follower_count)
    profile.content_tags = _extract_content_tags(db, profile)

    text = build_creator_embedding_text(profile)
    vector = embed_text(text, context=f"creator embedding for creator_id={creator_id}")
    if vector:
        profile.embedding = vector
        profile.embedding_text = text
    else:
        logger.warning("Creator signals: creator_id=%d — embed call failed, embedding not updated", creator_id)

    db.commit()

    logger.info(
        "Creator signals: creator_id=%d tier=%s tags=%d text=%r",
        creator_id, profile.creator_tier, len(profile.content_tags or []), text[:150],
    )
    return {
        "creator_tier":   profile.creator_tier,
        "content_tags":   profile.content_tags,
        "embedding_text": profile.embedding_text,
    }
