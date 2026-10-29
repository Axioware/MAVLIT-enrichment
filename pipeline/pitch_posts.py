"""
pipeline/pitch_posts.py

The "Past brand partnerships" and "Link to your most relevant content" fields of a pitch hold up to 3
comma-separated entries. An entry that is an Instagram post URL (instagram.com/p/<code>, /reel/<code>
or /tv/<code>) is turned into real information for the pitch LLM: the post URL, its caption and its view
count.

  1. The post is looked up in creator_instagram_posts (by its short code) - posts already scraped for
     any MAVLIT user are reused, no Apify run.
  2. Only the posts NOT found there are scraped with Apify (one run for all of them), and saved in
     creator_instagram_posts for the user.
  3. The LLM gets  <url> (post caption: "..."; views: 45,000)  for each such entry.

Any other entry (a brand name, a description, a non-Instagram link) is passed on as typed, and an entry
that never needs Apify never runs it. A post that cannot be read (private, deleted, Apify failure) is
passed on as just its URL.
"""

import logging
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from config import APIFY_TOKEN
from pipeline.creator_content.instagram_description import save_creator_posts
from pipeline.db import CreatorInstagramPost, CreatorProfile
from pipeline.helpers.apify import ApifyQuotaExceeded, run_apify_actor
from pipeline.helpers.gpt_llm import llm_usage_scope, record_apify_scrape

logger = logging.getLogger(__name__)

_ACTOR_ID = "shu8hvrXbJbY3Eb9W"   # the Instagram scraper used everywhere else
_MAX_ENTRIES = 3
_CAPTION_CHARS = 300
_POST_URL_RE = re.compile(r"instagram\.com/(?:[A-Za-z0-9._]+/)?(?:p|reels?|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE)


def split_entries(value: str | None) -> list[str]:
    """The comma-separated entries of a pitch field (at most 3, empty ones dropped)."""
    return [e.strip() for e in (value or "").split(",") if e.strip()][:_MAX_ENTRIES]


def post_code(entry: str) -> str | None:
    """The Instagram short code when the entry is a post / reel URL, else None."""
    match = _POST_URL_RE.search(entry)
    return match.group(1) if match else None


def has_post_urls(*values: str | None) -> bool:
    return any(post_code(entry) for value in values for entry in split_entries(value))


def _clean_url(entry: str) -> str:
    """The entry as a clean https URL (scheme added, query string and fragment dropped)."""
    url = entry.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return re.split(r"[?#]", url, maxsplit=1)[0]


def _saved_post(db: Session, code: str) -> dict | None:
    """The post with this short code from creator_instagram_posts (any user), or None."""
    row = (
        db.query(CreatorInstagramPost)
        .filter(CreatorInstagramPost.post_id.isnot(None), func.strpos(CreatorInstagramPost.post_url, "/" + code) > 0)
        .order_by(CreatorInstagramPost.fetched_at.desc())
        .first()
    )
    if row is None:
        return None
    return {"caption": row.caption, "views": row.video_view_count}


def _scrape_posts(db: Session, creator: CreatorProfile, urls: dict[str, str]) -> dict[str, dict]:
    """
    Apify, in ONE run: the posts for {short code: url}. They are saved in creator_instagram_posts for the
    creator. Returns {short code: {"caption", "views"}} for the posts that could be read ({} on failure).
    """
    if not urls or not APIFY_TOKEN:
        return {}
    db.commit()   # end the read transaction first: an Apify run can take minutes
    try:
        items = run_apify_actor(
            _ACTOR_ID,
            {"directUrls": list(urls.values()), "resultsType": "posts", "resultsLimit": len(urls)},
            label=f"pitch posts ({len(urls)})",
            require_success=True,
        )
    except ApifyQuotaExceeded:
        logger.warning("Pitch posts: Apify quota exceeded - using the URLs without details")
        return {}
    items = [i for i in (items or []) if isinstance(i, dict)]
    with llm_usage_scope(creator.id, "pitch"):
        record_apify_scrape("instagram", "post urls", len(urls), len(items))

    found: dict[str, dict] = {}
    by_owner: dict[str, list[dict]] = {}
    for item in items:
        code = item.get("shortCode") or (post_code(item.get("url") or "") or "")
        if code not in urls:
            continue
        found[code] = {"caption": item.get("caption"), "views": item.get("videoViewCount")}
        by_owner.setdefault((item.get("ownerUsername") or "").strip() or "unknown", []).append(item)
    for owner, owner_items in by_owner.items():
        save_creator_posts(db, creator.id, owner, owner_items)   # never raises
    return found


def describe_entries(db: Session, creator: CreatorProfile, value: str | None, details: dict[str, dict]) -> str:
    """
    The text the pitch LLM gets for one field: each entry as typed, except an Instagram post URL, which
    becomes  <url> (post caption: "..."; views: N)  from `details` (short code -> caption / views).
    One entry as it is; several as numbered lines. "" when the field is empty.
    """
    lines = []
    for entry in split_entries(value):
        code = post_code(entry)
        if code is None:
            lines.append(entry)
            continue
        info = details.get(code) or {}
        parts = []
        caption = " ".join((info.get("caption") or "").split())
        if caption:
            parts.append(f'post caption: "{caption[:_CAPTION_CHARS]}{"..." if len(caption) > _CAPTION_CHARS else ""}"')
        if isinstance(info.get("views"), int) and info["views"] > 0:
            parts.append(f"views: {info['views']:,}")
        lines.append(f"{_clean_url(entry)} ({'; '.join(parts)})" if parts else _clean_url(entry))
    if len(lines) <= 1:
        return lines[0] if lines else ""
    return "\n".join(f"{i}) {line}" for i, line in enumerate(lines, 1))


def resolve_post_details(db: Session, creator: CreatorProfile, *values: str | None) -> dict[str, dict]:
    """
    {short code: {"caption", "views"}} for every Instagram post URL in the given pitch fields: from
    creator_instagram_posts when saved there, otherwise scraped (all missing posts in one Apify run) and saved.
    Posts that cannot be read are simply absent.
    """
    urls: dict[str, str] = {}
    for value in values:
        for entry in split_entries(value):
            code = post_code(entry)
            if code:
                urls.setdefault(code, _clean_url(entry))
    details: dict[str, dict] = {}
    missing: dict[str, str] = {}
    for code, url in urls.items():
        saved = _saved_post(db, code)
        if saved is not None:
            details[code] = saved
        else:
            missing[code] = url
    if missing:
        logger.info("Pitch posts: %d post(s) saved already, scraping %d", len(details), len(missing))
        details.update(_scrape_posts(db, creator, missing))
    return details
