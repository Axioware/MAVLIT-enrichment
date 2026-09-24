"""Generate a creator description from a YouTube channel and recent videos."""

import logging

from sqlalchemy.orm import Session

from pipeline.db import Prompt
from pipeline.enrichment.youtube_sponsorship import (
    _CHANNELS_URL,
    _SEARCH_URL,
    _fetch_video_details,
    _yt_get,
)
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import (
    CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT,
    CREATOR_NICHE_DESCRIPTION_PROMPT_NAME,
)

logger = logging.getLogger(__name__)

_VIDEOS_LIMIT = 20


def _get_description_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == CREATOR_NICHE_DESCRIPTION_PROMPT_NAME).first()
    return row.content if row else CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT


def _find_channel(username: str) -> dict | None:
    data = _yt_get(_SEARCH_URL, {
        "part": "snippet",
        "q": username.strip().lstrip("@"),
        "type": "channel",
        "maxResults": 5,
    })
    candidates = data.get("items", []) if data else []
    if not candidates:
        return None

    wanted = username.strip().lstrip("@").casefold()
    for item in candidates:
        snippet = item.get("snippet") or {}
        title = (snippet.get("title") or "").casefold().replace(" ", "")
        if wanted and wanted in title:
            return item
    return candidates[0]


def generate_youtube_description(db: Session, username: str, niche: str) -> dict:
    """Find a channel by username, fetch 20 video descriptions, and summarize them."""
    if not username or not username.strip():
        raise ValueError("YouTube username is required")
    if not niche or not niche.strip():
        raise ValueError("YouTube niche is required")

    channel = _find_channel(username)
    if not channel:
        raise RuntimeError("YouTube channel could not be found")
    channel_id = ((channel.get("id") or {}).get("channelId"))
    if not channel_id:
        raise RuntimeError("YouTube search did not return a channel")

    channel_data = _yt_get(_CHANNELS_URL, {"part": "snippet", "id": channel_id}) or {}
    channel_item = (channel_data.get("items") or [channel])[0]
    bio = ((channel_item.get("snippet") or {}).get("description") or "").strip()

    video_search = _yt_get(_SEARCH_URL, {
        "part": "id",
        "channelId": channel_id,
        "type": "video",
        "order": "date",
        "maxResults": _VIDEOS_LIMIT,
    }) or {}
    video_ids = [
        item.get("id", {}).get("videoId")
        for item in video_search.get("items", [])
        if item.get("id", {}).get("videoId")
    ][: _VIDEOS_LIMIT]
    videos = _fetch_video_details(video_ids)
    captions = []
    for video in videos[:_VIDEOS_LIMIT]:
        snippet = video.get("snippet") or {}
        title = (snippet.get("title") or "").strip()
        description = (snippet.get("description") or "").strip()
        if title or description:
            captions.append(f"Title: {title}\nDescription: {description}".strip())

    if not bio and not captions:
        raise RuntimeError("YouTube channel has no bio or video descriptions to analyze")

    prompt = fill_template(
        _get_description_prompt(db),
        niche=niche.strip(),
        bio=bio,
        captions="\n\n".join(f"Video {index}: {caption}" for index, caption in enumerate(captions, 1)),
    )
    result = call_gpt_json(prompt, context=f"creator YouTube {username.strip()}")
    description = (result.get("description") or "").strip()
    if not description:
        raise RuntimeError("The LLM did not return a creator description")

    return {"description": description, "videos_analyzed": len(captions), "bio_found": bool(bio)}