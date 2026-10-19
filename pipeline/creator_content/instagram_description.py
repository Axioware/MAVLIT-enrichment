"""
Generate a creator description from an Instagram profile and recent posts.

ONE LLM call (prompt creator_niche_description, editable in /admin) returns the
description AND the creator's estimated audience - gender (male / female /
both) and age min / max - from the same bio + captions. They are stored on the
profile as instagram_audience_gender / _age_min / _age_max when the creator
chooses "let MAVLIT generate it". youtube_description.py reuses
audience_from_result for its channel.
"""

import logging

from sqlalchemy.orm import Session

from pipeline.db import Prompt
from pipeline.enrichment.instagram_posts import _scrape_handle
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template, record_apify_scrape
from pipeline.helpers.prompts import (
    CREATOR_AUDIENCE_NOTE,
    CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT,
    CREATOR_NICHE_DESCRIPTION_PROMPT_NAME,
)
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_POSTS_LIMIT = 20


def _get_description_prompt(db: Session) -> str:
    """
    The description prompt (stored copy in /admin, else the code default). A stored copy
    that predates the audience fields gets CREATOR_AUDIENCE_NOTE appended, so the same
    single call still returns the audience gender and age range.
    """
    row = db.query(Prompt).filter(Prompt.name == CREATOR_NICHE_DESCRIPTION_PROMPT_NAME).first()
    if not row:
        return CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT
    if "audience_gender" not in row.content:
        logger.info("Prompt %s has no audience fields - appending the audience note", CREATOR_NICHE_DESCRIPTION_PROMPT_NAME)
        return row.content.rstrip() + "\n" + CREATOR_AUDIENCE_NOTE
    return row.content


def _audience_age(value) -> int | None:
    try:
        age = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return age if 13 <= age <= 100 else None


def audience_from_result(result: dict) -> dict:
    """
    The audience part of the description LLM's JSON: {"audience_gender":
    "male"|"female"|"both"|None, "audience_age_min": int|None, "audience_age_max":
    int|None}. Missing or unusable values become None (the description is still
    used) - e.g. when the prompt copy in /admin predates the audience fields.
    """
    gender = str(result.get("audience_gender") or "").strip().lower()
    gender = gender if gender in ("male", "female", "both") else None
    low, high = _audience_age(result.get("audience_age_min")), _audience_age(result.get("audience_age_max"))
    if low is not None and high is not None and low > high:
        low, high = high, low
    if gender is None and low is None and high is None:
        logger.info("Description LLM returned no audience estimate (prompt copy without the audience fields?)")
    return {"audience_gender": gender, "audience_age_min": low, "audience_age_max": high}


def generate_instagram_description(db: Session, username: str, niche: str) -> dict:
    """Scrape up to 20 posts; one LLM call returns the description and the audience estimate."""
    handle = normalize_handle(username)
    if not handle:
        raise ValueError("Instagram username is required")
    if not niche or not niche.strip():
        raise ValueError("Instagram niche is required")

    items = _scrape_handle(handle, _POSTS_LIMIT)
    if items is None:
        raise RuntimeError("Instagram content could not be fetched")
    record_apify_scrape("instagram", handle, _POSTS_LIMIT, len(items[:_POSTS_LIMIT]))

    bio = ""
    captions: list[str] = []
    for item in items[:_POSTS_LIMIT]:
        metadata = item.get("metaData") or {}
        bio = bio or metadata.get("biography") or item.get("biography") or ""
        caption = (item.get("caption") or "").strip()
        if caption:
            captions.append(caption)

    if not bio and items:
        bio = (items[0].get("biography") or "").strip()
    if not bio and not captions:
        raise RuntimeError("Instagram profile has no bio or captions to analyze")

    caption_text = "\n\n".join(f"Post {index}: {caption}" for index, caption in enumerate(captions, 1))
    prompt = fill_template(_get_description_prompt(db), niche=niche.strip(), bio=bio, captions=caption_text)
    result = call_gpt_json(prompt, context=f"creator Instagram @{handle}")
    description = (result.get("description") or "").strip()
    if not description:
        raise RuntimeError("The LLM did not return a creator description")

    return {"description": description, "posts_analyzed": len(captions), "bio_found": bool(bio),
            **audience_from_result(result)}