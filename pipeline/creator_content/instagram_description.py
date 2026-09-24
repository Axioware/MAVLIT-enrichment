"""Generate a creator description from an Instagram profile and recent posts."""

import logging

from sqlalchemy.orm import Session

from pipeline.db import Prompt
from pipeline.enrichment.instagram_posts import _scrape_handle
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import (
    CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT,
    CREATOR_NICHE_DESCRIPTION_PROMPT_NAME,
)
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_POSTS_LIMIT = 20


def _get_description_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == CREATOR_NICHE_DESCRIPTION_PROMPT_NAME).first()
    return row.content if row else CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT


def generate_instagram_description(db: Session, username: str, niche: str) -> dict:
    """Scrape up to 20 posts and generate a description from their captions."""
    handle = normalize_handle(username)
    if not handle:
        raise ValueError("Instagram username is required")
    if not niche or not niche.strip():
        raise ValueError("Instagram niche is required")

    items = _scrape_handle(handle, _POSTS_LIMIT)
    if items is None:
        raise RuntimeError("Instagram content could not be fetched")

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

    prompt = fill_template(
        _get_description_prompt(db),
        niche=niche.strip(),
        bio=bio,
        captions="\n\n".join(f"Post {index}: {caption}" for index, caption in enumerate(captions, 1)),
    )
    result = call_gpt_json(prompt, context=f"creator Instagram @{handle}")
    description = (result.get("description") or "").strip()
    if not description:
        raise RuntimeError("The LLM did not return a creator description")

    return {"description": description, "posts_analyzed": len(captions), "bio_found": bool(bio)}