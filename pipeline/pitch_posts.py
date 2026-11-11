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
  3. The LLM gets  <url> (post caption: "..."; views: 45,000; likes: 3,200; comments: 150; engagement (likes + comments): 3,350)  for each such entry.

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
from pipeline.helpers.apify import ApifyQuotaExceeded, request_path, run_apify_actor
from pipeline.helpers.gpt_llm import llm_usage_scope

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
    return {"caption": row.caption, "views": row.video_view_count, "likes": row.likes_count, "comments": row.comments_count}


def _scrape_posts(db: Session, creator: CreatorProfile, urls: dict[str, str]) -> dict[str, dict]:
    """
    Apify, in ONE run: the posts for {short code: url}. They are saved in creator_instagram_posts for the
    creator. Returns {short code: {"caption", "views"}} for the posts that could be read ({} on failure).
    """
    if not urls or not APIFY_TOKEN:
        return {}
    db.commit()   # end the read transaction first: an Apify run can take minutes
    try:
        with request_path(), llm_usage_scope(creator.id, "pitch"):   # a person is waiting: 90 s, not the batch limit; the run is recorded for them
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

    found: dict[str, dict] = {}
    by_owner: dict[str, list[dict]] = {}
    for item in items:
        code = item.get("shortCode") or (post_code(item.get("url") or "") or "")
        if code not in urls:
            continue
        found[code] = {"caption": item.get("caption"), "views": item.get("videoViewCount"),
                       "likes": item.get("likesCount"), "comments": item.get("commentsCount")}
        by_owner.setdefault((item.get("ownerUsername") or "").strip() or "unknown", []).append(item)
    for owner, owner_items in by_owner.items():
        save_creator_posts(db, creator.id, owner, owner_items)   # never raises
    return found


def multiple_label(value: int, base: int | None) -> str | None:
    """"about 2x" / "about 1.5x": value / base rounded DOWN to the nearest half, only from 1.5x up. None otherwise."""
    if not base or base <= 0 or value <= 0 or value / base < 1.5:
        return None
    return f"about {int(value / base * 2) / 2:g}x"


def describe_entries(
    db: Session,
    creator: CreatorProfile,
    value: str | None,
    details: dict[str, dict],
    followers: int | None = None,
    usual_views: int | None = None,
) -> str:
    """
    The text the pitch LLM gets for one field: each entry as typed, except an Instagram post URL, which
    becomes  <url> (post caption: "..."; views: N; ...)  from `details` (short code -> caption / views / likes /
    comments). With `followers` and `usual_views` (the median views of the creator's latest posts) a post's
    views are also compared to them ("about 2x their follower count") and its engagement is given as a rate of
    their followers, so the LLM never has to do that arithmetic. One entry as it is; several as numbered
    lines. "" when the field is empty.
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
            for label, base in (("their follower count", followers), ("their usual views per reel", usual_views)):
                multiple = multiple_label(info["views"], base)
                if multiple:
                    parts.append(f"views are {multiple} {label}")
        likes, comments = info.get("likes"), info.get("comments")
        if isinstance(likes, int) and likes >= 0:   # Instagram reports -1 when the likes are hidden
            parts.append(f"likes: {likes:,}")
            if isinstance(comments, int) and comments >= 0:
                parts.append(f"comments: {comments:,}")
                parts.append(f"engagement (likes + comments): {likes + comments:,}")
                if followers and followers > 0 and 0 < (likes + comments) / followers <= 1:
                    parts.append(f"engagement rate (likes + comments, as % of followers): {(likes + comments) / followers * 100:.1f}%")
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


def ensure_content_links(text: str, content_link: str | None) -> str:
    """
    The pitch text with every Instagram post URL of the "most relevant content" field in it. The LLM is told to
    keep them all but sometimes drops some; a missing one is added as a short paragraph before the closing
    paragraph and sign-off. Text that already has them all is returned unchanged.
    """
    missing = []
    for entry in split_entries(content_link):
        code = post_code(entry)
        if code and code not in text and _clean_url(entry) not in missing:
            missing.append(_clean_url(entry))
    if not missing:
        return text
    if len(missing) == 1:
        line = f"Another recent example of my content: {missing[0]}"
    else:
        line = "A few more recent examples of my content: " + ", ".join(missing[:-1]) + f" and {missing[-1]}"
    paragraphs = text.rstrip().split("\n\n")
    paragraphs.insert(max(len(paragraphs) - 2, 1), line + ".")
    return "\n\n".join(paragraphs)


_URL = r"https?://[^\s()]+"
_FIGURE_THEN_URL_IN_BRACKETS = re.compile(rf"\s*\(([^()\n]*?)\s*:\s*({_URL})\)")      # (461 likes and comments: URL)
_URL_IN_BRACKETS = re.compile(rf"\s*\(({_URL})\)")                                     # likes and comments (URL)
_BARE_FIGURE_IN_BRACKETS = re.compile(
    r"\s*\(((?:(?:about|around|over|nearly) \d[\d.,]*[KkMm]?\+?|\d[\d.,]*[KkMm]\+?|\d[\d.,]*(?= (?:likes|views|comments)))(?: [A-Za-z+]+){0,4})\)"
)   # (about 26K), (8K+ views); not (2006)


_MEDIAN_CLAUSE = re.compile(r",?\s*(?:and\s+)?with a median of (?:about |around |roughly )?[\d][\d.,]*[KkMm]?(?=[.,;])", re.IGNORECASE)
_CLOSING_QUESTION = re.compile(r"(?:^|(?<=[.!?])\s+)([^.!?\n]*\bpartnership[^.!?\n]*\?)\s*$")   # the old "...a paid partnership?" closing


def tidy_pitch(
    text: str, past_brand_partnership: str | None, content_link: str | None, brand_name: str | None = None
) -> str:
    """
    Two fixes the prompt asks for but the LLM sometimes ignores, made in code:
      - a post's figure or URL in brackets becomes plain words:  "(461 likes and comments: URL)" -> ", with 461 likes and
        comments: URL",  "comments (URL)" -> "comments: URL",  "(about 26K)" -> ", about 26K";
      - a "with a median of N" clause is dropped (many readers do not know the word);
      - an old-style closing "...a paid partnership?" becomes "Would you be open to talking about how we could feature
        <brand>'s products with my audience?" (when the brand name is known);
      - the paragraph(s) about the past brand partnerships come BEFORE those about the relevant content (only when
        no paragraph mixes the two kinds of post).
    """
    text = _MEDIAN_CLAUSE.sub("", text)
    if brand_name:
        paragraphs = text.split("\n\n")
        for i in range(len(paragraphs) - 1, -1, -1):
            if "?" in paragraphs[i]:
                paragraphs[i] = _CLOSING_QUESTION.sub(
                    lambda m: m.group(0).replace(m.group(1), f"Would you be open to talking about how we could feature {brand_name}'s products with my audience?"),
                    paragraphs[i].rstrip(),
                )
                break
        text = "\n\n".join(paragraphs)
    text = _FIGURE_THEN_URL_IN_BRACKETS.sub(lambda m: f", with {m.group(1)}: {m.group(2)}", text)
    text = _URL_IN_BRACKETS.sub(lambda m: f": {m.group(1)}", text)
    text = _BARE_FIGURE_IN_BRACKETS.sub(lambda m: f", {m.group(1)}", text)

    def codes(value):
        return {c for c in (post_code(e) for e in split_entries(value)) if c}

    past, content = codes(past_brand_partnership), codes(content_link)
    paragraphs = text.split("\n\n")
    is_past = [any(c in p for c in past) for p in paragraphs]
    is_content = [any(c in p for c in content) for p in paragraphs]
    if not any(is_past) or not any(is_content) or any(a and b for a, b in zip(is_past, is_content)):
        return text
    first_content = is_content.index(True)
    if is_past.index(True) > first_content:
        moved = [p for p, flag in zip(paragraphs, is_past) if flag]
        rest = [p for p, flag in zip(paragraphs, is_past) if not flag]
        at = first_content - sum(is_past[:first_content])   # past paragraphs sit after the content ones, so none is before
        paragraphs = rest[:at] + moved + rest[at:]
    return "\n\n".join(paragraphs)
