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

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert

from pipeline.db import CreatorInstagramPost, Prompt
from pipeline.enrichment.instagram_posts import _scrape_handle, _usernames_only
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template, record_apify_scrape
from pipeline.helpers.prompts import (
    CREATOR_AUDIENCE_NOTE,
    CREATOR_SUB_NICHES_NOTE,
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
    prompt = row.content
    if "audience_gender" not in prompt:
        logger.info("Prompt %s has no audience fields - appending the audience note", CREATOR_NICHE_DESCRIPTION_PROMPT_NAME)
        prompt = prompt.rstrip() + "\n" + CREATOR_AUDIENCE_NOTE
    if "sub_niches" not in prompt:
        logger.info("Prompt %s has no sub_niches - appending the sub-niche note", CREATOR_NICHE_DESCRIPTION_PROMPT_NAME)
        prompt = prompt.rstrip() + "\n" + CREATOR_SUB_NICHES_NOTE
    return prompt


_SUB_NICHE_OPTIONS_LIMIT = 150
_SUB_NICHES_MAX = 4


def sub_niche_options(db: Session, niche: str) -> list[str]:
    """
    The sub-niches a creator of `niche` can pick from: the tags of the brands in that
    niche (brands_niches.tags - the same tags the profile page's sub-niche picker lists),
    most common first, at most _SUB_NICHE_OPTIONS_LIMIT.
    """
    niches = [n.strip().lower() for n in (niche or "").split(",") if n.strip()]
    if not niches:
        return []
    rows = db.execute(text("""
        SELECT min(btrim(tag)) AS tag, COUNT(*) AS n
        FROM brands_niches bn, jsonb_array_elements_text(bn.tags) AS tag
        WHERE jsonb_typeof(bn.tags) = 'array' AND lower(btrim(bn.niche)) = ANY(:niches) AND btrim(tag) <> ''
        GROUP BY lower(btrim(tag))
        ORDER BY n DESC, 1
        LIMIT :limit
    """), {"niches": niches, "limit": _SUB_NICHE_OPTIONS_LIMIT}).all()
    return [r[0] for r in rows]


def sub_niches_from_result(result: dict, options: list[str]) -> list[str]:
    """
    The LLM's sub_niches as clean strings: at most _SUB_NICHES_MAX, no duplicates and - when
    there is a list of options - only values from it (matched ignoring case, returned as
    written in the options). Anything else is dropped.
    """
    raw = result.get("sub_niches")
    if not isinstance(raw, list):
        return []
    canonical = {o.strip().lower(): o for o in options}
    picked: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            continue
        value = canonical.get(value.strip().lower()) if canonical else value.strip()
        if value and value not in picked:
            picked.append(value)
    return picked[:_SUB_NICHES_MAX]


def followers_from_items(items: list[dict]) -> int | None:
    """The account's follower count from the Apify items (profile data sits under metaData)."""
    for item in items:
        value = (item.get("metaData") or {}).get("followersCount", item.get("followersCount"))
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


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


def save_creator_posts(db: Session, creator_id: int, handle: str, items: list[dict]) -> int:
    """
    Store every scraped post for the MAVLIT user (creator_instagram_posts), updating
    posts already stored for them. Returns the number of posts saved. Never raises:
    a storage problem must not stop the description from being generated.
    """
    rows = []
    for item in items:
        post_id = item.get("id")
        if not post_id:
            continue
        meta = item.get("metaData") or {}
        rows.append({
            "creator_profile_id": creator_id,
            "instagram_handle":   handle,
            "post_id":            str(post_id),
            "post_url":           item.get("url"),
            "post_type":          item.get("type"),
            "timestamp":          item.get("timestamp"),
            "caption":            item.get("caption"),
            "hashtags":           item.get("hashtags"),
            "mentions":           item.get("mentions"),
            "tagged_users":       _usernames_only(item.get("taggedUsers")),
            "paid_partnership":   item.get("paidPartnership"),
            "likes_count":        item.get("likesCount"),
            "comments_count":     item.get("commentsCount"),
            "video_view_count":   item.get("videoViewCount"),
            "video_play_count":   item.get("videoPlayCount"),
            "followers_count":    meta.get("followersCount") or item.get("followersCount"),
            "biography":          meta.get("biography") or item.get("biography"),
        })
    if not rows:
        return 0
    try:
        stmt = insert(CreatorInstagramPost).values(rows)
        update = {c: stmt.excluded[c] for c in rows[0] if c not in ("creator_profile_id", "post_id")}
        db.execute(stmt.on_conflict_do_update(constraint="uq_creator_instagram_post", set_={**update, "fetched_at": func.now()}))
        db.commit()
        return len(rows)
    except Exception as exc:
        db.rollback()
        logger.warning("Could not store the scraped posts of creator_id=%s (@%s) - %s", creator_id, handle, exc)
        return 0


def generate_instagram_description(db: Session, username: str, niche: str, creator_id: int | None = None) -> dict:
    """
    Scrape up to 20 posts (stored in creator_instagram_posts when creator_id is given);
    one LLM call returns the description and the audience estimate.
    """
    handle = normalize_handle(username)
    if not handle:
        raise ValueError("Instagram username is required")
    if not niche or not niche.strip():
        raise ValueError("Instagram niche is required")

    items = _scrape_handle(handle, _POSTS_LIMIT)
    if items is None:
        raise RuntimeError("Instagram content could not be fetched")
    record_apify_scrape("instagram", handle, _POSTS_LIMIT, len(items[:_POSTS_LIMIT]))
    if creator_id is not None:
        save_creator_posts(db, creator_id, handle, items[:_POSTS_LIMIT])

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
    options = sub_niche_options(db, niche)
    prompt = fill_template(
        _get_description_prompt(db), niche=niche.strip(), bio=bio, captions=caption_text,
        sub_niche_options="; ".join(options) or "none",
    )
    result = call_gpt_json(prompt, context=f"creator Instagram @{handle}")
    description = (result.get("description") or "").strip()
    if not description:
        raise RuntimeError("The LLM did not return a creator description")

    return {"description": description, "posts_analyzed": len(captions), "bio_found": bool(bio),
            "followers_count": followers_from_items(items), "sub_niches": sub_niches_from_result(result, options),
            **audience_from_result(result)}