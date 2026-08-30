"""
pipeline/enrichment/youtube_sponsorship.py

Detects YouTube sponsorships for brands.

For each brand, searches YouTube for videos that mention the brand as a
sponsor, analyzes the video description, and classifies the sponsorship type.

Sponsorship types:
  paid_sponsor  — explicit "sponsored by / #ad / paid partnership" language
  affiliate     — affiliate links, promo/discount codes
  gifted        — "gifted by / sent me / c/o / collab with"
  mention       — brand name appears in description without explicit markers

Results are stored in the youtube_sponsorships table (one row per video).
Sets youtube_checked=True on brands_raw after processing.

Also fetches up to 200 top-level comments (commentThreads.list, paginated
2×100) for each video that's actually stored as a sponsorship — cheap at 1
quota unit per page regardless of maxResults, so this only runs for
genuine hits, not every video searched. Commenter display names are then
batch-classified by gender via the LLM (one call per video) and
aggregated into male_pct/female_pct — a rough proxy for the video's
audience gender split, same caveat as any name-based classification
elsewhere in this codebase (usernames/handles are often ungendered, hence
"unknown" is excluded from the percentage base rather than guessed).

Requires YOUTUBE_API_KEY in config.
YouTube Data API v3 quota cost: 14 queries × 100 units = 1,400 units per
brand for search, plus 2 units per stored video for its 200 comments.
Free tier: 10,000 units/day → ~7 brands/day (search-dominated).
"""

import json
import logging
import re
import time

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import YOUTUBE_API_KEY, YOUTUBE_API_KEY_1, YOUTUBE_API_KEY_2, YOUTUBE_API_KEY_3, YOUTUBE_API_KEY_4, YOUTUBE_API_KEY_5, YOUTUBE_API_KEY_6, YOUTUBE_API_KEY_7, YOUTUBE_API_KEY_8, YOUTUBE_API_KEY_9, YOUTUBE_API_KEY_10, YOUTUBE_API_KEY_11, YOUTUBE_API_KEY_12, OPENAI_KEY, ENABLE_LLM
from pipeline.db import BrandRaw, Prompt, YoutubeSponsorship
from pipeline.helpers.creator_tier import bucket_creator_tier
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.gpt_llm import call_gpt_json, call_gpt_text, fill_template
from pipeline.helpers.prompts import (
    GENDER_PROMPT_NAME, GENDER_DEFAULT_PROMPT,
    SPONSOR_CHECK_PROMPT_NAME, SPONSOR_CHECK_DEFAULT_PROMPT,
)

logger = logging.getLogger(__name__)

_SEARCH_URL   = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL   = "https://www.googleapis.com/youtube/v3/videos"
_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_COMMENTS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
_MAX_RESULTS  = 10    # per search query (keeps quota low)
_MAX_COMMENTS = 200   # per video — YouTube caps each page at 100, so this paginates across 2 pages

#  Prompt helpers

def _get_gender_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == GENDER_PROMPT_NAME).first()
    return row.content if row else GENDER_DEFAULT_PROMPT


def _get_sponsor_check_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == SPONSOR_CHECK_PROMPT_NAME).first()
    return row.content if row else SPONSOR_CHECK_DEFAULT_PROMPT


#  Sponsorship detection patterns

_PAID_PATTERNS = [
    r'\bsponsored\s+by\b',
    r'\bthis\s+video\s+is\s+sponsored\b',
    r'\btoday.{0,30}sponsored\s+by\b',
    r'\bpaid\s+partnership\b',
    r'\bpaid\s+promotion\b',
    r'\bin\s+paid\s+collaboration\b',
    r'\bsponsor\s*:\s*',
]

_AFFILIATE_PATTERNS = [
    r'\baffiliate\s+link\b',
    r'\bpromo\s+code\b',
    r'\bdiscount\s+code\b',
    r'\buse\s+code\b',
    r'\bcoupon\s+code\b',
    r'\bcommission\b',
    r'\baffiliate\b',
]

_GIFTED_PATTERNS = [
    r'\bgifted\s+by\b',
    r'\bsent\s+me\b',
    r'\bc/o\b',
    r'\bprovided\s+by\b',
    r'\bin\s+collaboration\s+with\b',
    r'\bcollab(?:oration)?\s+with\b',
    r'\bpartner(?:ed|ship)?\s+with\b',
]

# Compile all patterns once
_COMPILED = {
    "paid_sponsor": [re.compile(p, re.IGNORECASE) for p in _PAID_PATTERNS],
    "affiliate":    [re.compile(p, re.IGNORECASE) for p in _AFFILIATE_PATTERNS],
    "gifted":       [re.compile(p, re.IGNORECASE) for p in _GIFTED_PATTERNS],
}

# Negative patterns — explicit denials that must disqualify the video
_NEGATIVE_PATTERNS = [
    re.compile(r'\bnot\s+sponsored\b',               re.IGNORECASE),
    re.compile(r'\bnot\s+an?\s+ad\b',                re.IGNORECASE),
    re.compile(r'\bunsponsored\b',                    re.IGNORECASE),
    re.compile(r'\bno\s+sponsor\b',                   re.IGNORECASE),
    re.compile(r'\bnot\s+paid\b',                     re.IGNORECASE),
    re.compile(r'\bdisclaimer\s*:\s*this\s+video\s+is\s+not\b', re.IGNORECASE),
]


def _snippet(text: str, match: re.Match, window: int = 150) -> str:
    """Extract a window of text around a regex match."""
    start = max(0, match.start() - window)
    end   = min(len(text), match.end() + window)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _detect_sponsorship(
    description: str,
    title: str,
    brand_name: str,
) -> tuple[str, float, list[str], str]:
    """
    Analyse description + title for sponsorship signals.

    Returns:
      (sponsorship_type, confidence, matched_keywords, description_snippet)
    """
    raw_text = (description or "").lower()
    # Strip @mentions so "@shopify" matches brand name "shopify"
    text  = re.sub(r'@(\w+)', r'\1', raw_text)
    brand = brand_name.lower()

    #  Step 1: reject videos that explicitly deny sponsorship 
    for neg in _NEGATIVE_PATTERNS:
        m = neg.search(text)
        if m:
            nearby = text[max(0, m.start() - 150) : m.end() + 150]
            if brand in nearby:
                return "none", 0.0, [], ""

    #  Step 2: match positive sponsorship patterns 
    for stype, patterns in _COMPILED.items():
        for pat in patterns:
            m = pat.search(text)
            if m:
                after_keyword  = text[m.end() : m.end() + 150]
                before_keyword = text[max(0, m.start() - 20) : m.start()]
                if brand not in after_keyword and brand not in before_keyword:
                    continue

                confidence = {
                    "paid_sponsor": 0.95,
                    "affiliate":    0.75,
                    "gifted":       0.65,
                }[stype]

                snippet = _snippet(description or "", m)
                return stype, confidence, [m.group(0).strip()], snippet

    return "none", 0.0, [], ""


#  YouTube API helpers 

class _QuotaExhausted(Exception):
    """Raised when all YouTube API keys have hit their daily quota."""


# Maps key index → env var name for clear log messages
_KEY_NAMES = ["YOUTUBE_API_KEY", "YOUTUBE_API_KEY_1", "YOUTUBE_API_KEY_2", "YOUTUBE_API_KEY_3", "YOUTUBE_API_KEY_4", "YOUTUBE_API_KEY_5", "YOUTUBE_API_KEY_6", "YOUTUBE_API_KEY_7", "YOUTUBE_API_KEY_8", "YOUTUBE_API_KEY_9", "YOUTUBE_API_KEY_10", "YOUTUBE_API_KEY_11", "YOUTUBE_API_KEY_12"]

# Populated lazily so config is read after load_dotenv()
_API_KEYS: list[str] = []
_key_index: int = 0

# Counts transient API failures (network errors, timeouts, non-quota 403s)
# during the CURRENT brand's processing — reset at the start of each brand
# in enrich_youtube_sponsorships() and checked before marking
# youtube_checked=True. Without this, a search query that failed due to a
# timeout looked identical to one that legitimately found zero videos, so
# a brand could get marked "checked" off the back of a dropped connection
# and never be retried — see _yt_get().
_transient_failures: int = 0

# True once every configured YOUTUBE_API_KEY* has hit _QuotaExhausted during
# the CURRENT enrich_youtube_sponsorships() call — reset at the top of that
# function. No underscore prefix: this is deliberately public so an
# orchestrator driving many brand_id= calls in a loop (e.g.
# run_end_to_end_pipeline.py) can check it after each call and stop
# retrying further brands for the rest of today, instead of burning ~13
# doomed API calls per remaining brand only to leave every one of them
# youtube_checked=False anyway.
# Named distinctly from the *local* `quota_exhausted` variable already used
# inside enrich_youtube_sponsorships() for unrelated per-brand bookkeeping
# (whether to break the brand loop early after partial video processing) —
# same name, different scope, would've silently collided once `global` made
# every assignment to that local name write through to this one instead.
quota_fully_exhausted: bool = False


def _active_key() -> str:
    global _API_KEYS
    if not _API_KEYS:
        _API_KEYS = [k for k in [YOUTUBE_API_KEY, YOUTUBE_API_KEY_1, YOUTUBE_API_KEY_2, YOUTUBE_API_KEY_3, YOUTUBE_API_KEY_4, YOUTUBE_API_KEY_5, YOUTUBE_API_KEY_6, YOUTUBE_API_KEY_7, YOUTUBE_API_KEY_8, YOUTUBE_API_KEY_9, YOUTUBE_API_KEY_10, YOUTUBE_API_KEY_11, YOUTUBE_API_KEY_12] if k]
    if not _API_KEYS:
        raise _QuotaExhausted("No YouTube API keys configured — set YOUTUBE_API_KEY in .env")
    # _key_index is never reset once every key has been rotated through
    # (see _rotate_key) — without this bounds check, every call after the
    # first full exhaustion raises a raw IndexError instead of the
    # _QuotaExhausted the rest of this module (and every caller) is built
    # to catch and handle gracefully.
    if _key_index >= len(_API_KEYS):
        raise _QuotaExhausted("All configured YouTube API keys exhausted")
    return _API_KEYS[_key_index]


def _rotate_key() -> None:
    """Switch to the next key. Raises _QuotaExhausted when all keys are spent."""
    global _key_index
    current_name = _KEY_NAMES[_key_index] if _key_index < len(_KEY_NAMES) else f"key_{_key_index}"
    _key_index += 1
    if _key_index >= len(_API_KEYS):
        logger.warning("YouTube quota exhausted — %s daily limit reached. No more fallback keys.", current_name)
        raise _QuotaExhausted(f"{current_name} quota exhausted and no fallback key available")
    next_name = _KEY_NAMES[_key_index] if _key_index < len(_KEY_NAMES) else f"key_{_key_index}"
    logger.warning(
        "YouTube quota exhausted — %s daily limit reached. Switching to %s.",
        current_name, next_name,
    )


# YouTube Data API v3 signals daily-quota exhaustion inconsistently — the
# documented reason strings are quotaExceeded/dailyLimitExceeded/
# rateLimitExceeded/userRateLimitExceeded, but in practice a fully-exhausted
# key has also been observed returning the generic "forbidden" reason on
# every call instead. Treating "forbidden" as quota-like too means a truly
# dead/restricted key still gets the same safe behavior (rotate, and stop
# entirely once every key does this) instead of being retried 13 times per
# brand for every remaining brand in the run.
_QUOTA_403_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded", "forbidden"}


def _403_reason(resp: httpx.Response) -> str:
    try:
        return resp.json().get("error", {}).get("errors", [{}])[0].get("reason", "")
    except Exception:
        return ""


def _key_invalid_reason(resp: httpx.Response) -> str:
    """
    Returns the ErrorInfo.reason (e.g. API_KEY_INVALID) for a 400 caused by
    a bad/expired/revoked key, or '' if this 400 is something else (a real
    malformed-request bug in our own params, which should NOT be treated
    the same way — that needs fixing in code, not key rotation).
    """
    try:
        for d in resp.json().get("error", {}).get("details", []):
            if d.get("@type", "").endswith("ErrorInfo"):
                return d.get("reason", "")
    except Exception:
        pass
    return ""


def _looks_key_dead(resp: httpx.Response) -> bool:
    """True for a 400 caused by an invalid/expired API key — distinct from
    quota exhaustion (429/403) but needs the same response: retrying it is
    pointless, and unlike quota it will never start working again on its
    own (the key has to be renewed/replaced in .env)."""
    return resp.status_code == 400 and _key_invalid_reason(resp) == "API_KEY_INVALID"


def _looks_quota_exhausted(resp: httpx.Response) -> bool:
    """True if this response means 'stop using this key' — either the
    documented 429, or a 403 whose reason matches _QUOTA_403_REASONS."""
    if resp.status_code == 429:
        return True
    return resp.status_code == 403 and _403_reason(resp) in _QUOTA_403_REASONS


def _yt_get(url: str, params: dict) -> dict | None:
    """
    GET against a YouTube endpoint, rotating through every configured
    YOUTUBE_API_KEY* in turn on quota exhaustion or an invalid/expired key
    (loops via _rotate_key(), which itself raises _QuotaExhausted once every
    key has been tried — not just a single fallback, so this scales to
    however many keys are configured, not just two).
    """
    global _transient_failures
    while True:
        params["key"] = _active_key()
        try:
            resp = httpx.get(url, params=params, timeout=15)
        except _QuotaExhausted:
            raise
        except Exception as exc:
            # A brand makes 50-90+ calls (14 searches + video/channel/comment
            # lookups), so a single flaky network blip (SSL handshake timeout,
            # connection reset) is common, not rare. Retry it inline a couple
            # times before counting it as a real transient failure — otherwise
            # one-off blips were routinely blocking youtube_checked=True on
            # brands that had already been fully, correctly processed.
            resp = None
            for attempt in range(2):
                time.sleep(1 + attempt)
                try:
                    resp = httpx.get(url, params=params, timeout=15)
                    break
                except Exception:
                    resp = None
                    continue
            if resp is None:
                logger.warning("YouTube API call failed: %s", exc)
                _transient_failures += 1
                return None
            # resp now holds a real response from the retry — fall through
            # to the normal response handling below instead of duplicating it.

        if _looks_quota_exhausted(resp) or _looks_key_dead(resp):
            if _looks_key_dead(resp):
                logger.warning(
                    "YouTube key invalid/expired (HTTP %s: %s) — rotating to next key. "
                    "This key needs to be renewed/replaced in .env, it won't recover on its own.",
                    resp.status_code, resp.json().get("error", {}).get("message", ""),
                )
            else:
                logger.warning(
                    "YouTube key exhausted/rejected (HTTP %s, reason=%s) — rotating to next key.",
                    resp.status_code, _403_reason(resp) or "n/a",
                )
            _rotate_key()  # raises _QuotaExhausted once every configured key has been tried
            continue        # retry this same call with the newly-active key

        if resp.status_code == 403:
            reason = _403_reason(resp)
            if reason == "commentsDisabled":
                # Expected, per-video condition (uploader turned comments off) —
                # not an API key/permissions problem, so don't warn about it
                # or count it as a transient failure.
                logger.debug("YouTube API: comments disabled for this video — skipping")
            else:
                logger.warning("YouTube API forbidden (403) reason=%s — check API key or permissions", reason or "unknown")
                _transient_failures += 1
            return None

        try:
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("YouTube API call failed: %s", exc)
            _transient_failures += 1
            return None


def _search_videos(query: str) -> list[str]:
    """Search YouTube and return a list of video IDs."""
    data = _yt_get(_SEARCH_URL, {
        "part":       "id",
        "q":          query,
        "type":       "video",
        "maxResults": _MAX_RESULTS,
        "relevanceLanguage": "en",
    })
    if not data:
        return []
    return [
        item["id"]["videoId"]
        for item in data.get("items", [])
        if item.get("id", {}).get("kind") == "youtube#video"
    ]


def _fetch_video_details(video_ids: list[str]) -> list[dict]:
    """Fetch snippet + statistics for a list of video IDs (max 50 per request)."""
    if not video_ids:
        return []
    results: list[dict] = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        data = _yt_get(_VIDEOS_URL, {
            "part": "snippet,statistics",
            "id":   ",".join(chunk),
        })
        if data:
            results.extend(data.get("items", []))
    return results


def _fetch_channel_subscribers_batch(channel_ids: list[str]) -> dict[str, int | None]:
    """
    Fetch subscriber counts for up to 50 channels per request (channels.list
    supports comma-separated IDs, same as videos.list). Replaces one call
    per channel — that pattern (dozens of rapid sequential HTTPS connections)
    is also the likely cause of the intermittent SSL handshake timeouts seen
    in real runs.
    """
    sub_counts: dict[str, int | None] = {}
    for i in range(0, len(channel_ids), 50):
        chunk = channel_ids[i : i + 50]
        data = _yt_get(_CHANNELS_URL, {
            "part": "statistics",
            "id":   ",".join(chunk),
        })
        if not data:
            continue
        for item in data.get("items", []):
            count = item.get("statistics", {}).get("subscriberCount")
            sub_counts[item["id"]] = int(count) if count else None
    return sub_counts


def _fetch_video_comments(video_id: str, max_results: int = _MAX_COMMENTS) -> list[dict]:
    """
    Fetch up to max_results top-level comments for a video via
    commentThreads.list — 1 quota unit per page regardless of maxResults.
    YouTube caps each page at 100, so max_results > 100 paginates across
    multiple calls (e.g. 200 = 2 pages = 2 quota units). Returns [] if
    comments are disabled for the video or the first call fails (never
    raises for that case — only _QuotaExhausted propagates, same as every
    other _yt_get call).
    """
    comments: list[dict] = []
    page_token: str | None = None

    while len(comments) < max_results:
        params = {
            "part":       "snippet",
            "videoId":    video_id,
            "maxResults": min(max_results - len(comments), 100),   # YouTube's own hard cap per page
            "order":      "relevance",
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token

        data = _yt_get(_COMMENTS_URL, params)
        if not data:
            break

        for item in data.get("items", []):
            top = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            comments.append({
                "author": top.get("authorDisplayName", ""),
                "text":   top.get("textDisplay", ""),
                "likes":  top.get("likeCount", 0),
            })

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return comments


_GENDER_BATCH_SIZE = 50   # keeps each LLM response comfortably short — a
                          # single call for all 200 names risked the response
                          # getting truncated mid-array (confirmed live: a
                          # 200-name call failed with "Unterminated string"
                          # partway through the JSON).


def _classify_commenter_genders(db: Session, comments: list[dict]) -> tuple[float | None, float | None]:
    """
    Batch-classifies all commenter display names via the LLM (in chunks of
    _GENDER_BATCH_SIZE — see that constant's comment) and returns
    (male_pct, female_pct) computed only over classifications that came
    back "male"/"female" — "unknown" is excluded from the percentage base
    rather than guessed, same pattern as demographics elsewhere in this
    codebase. A batch that's not a usable list at all is skipped (logged),
    not fatal — the other batches still contribute. A batch whose length
    merely doesn't match is truncated/padded rather than discarded outright
    (confirmed live: the LLM sometimes pads a few extra trailing "unknown"
    entries; the entries up to the requested count still line up correctly
    with the input in order). Returns (None, None) if there are no
    comments, OPENAI_KEY isn't set, or every batch fails outright.
    """
    names = [c["author"] for c in comments if c.get("author")]
    if not names or not OPENAI_KEY:
        return None, None

    prompt_template = _get_gender_prompt(db)
    all_genders: list[str] = []

    for i in range(0, len(names), _GENDER_BATCH_SIZE):
        chunk = names[i : i + _GENDER_BATCH_SIZE]
        prompt = fill_template(prompt_template, names=json.dumps(chunk, ensure_ascii=False))
        result = call_gpt_json(prompt, context=f"commenter gender classification (batch {i // _GENDER_BATCH_SIZE + 1}, {len(chunk)} names)")
        genders = result.get("genders") if isinstance(result, dict) else None

        if not isinstance(genders, list) or not genders:
            logger.warning(
                "YouTube commenter gender classification: batch at offset %d returned no usable list — skipping this batch",
                i,
            )
            continue

        if len(genders) != len(chunk):
            logger.debug(
                "YouTube commenter gender classification: batch at offset %d returned %d genders for %d names (%s)",
                i, len(genders), len(chunk), "truncating" if len(genders) > len(chunk) else "padding with unknown",
            )
            genders = genders[:len(chunk)] + ["unknown"] * max(0, len(chunk) - len(genders))
        all_genders.extend(genders)

    known = [g for g in all_genders if g in ("male", "female")]
    if not known:
        return None, None
    return round(all_genders.count("male") / len(known), 3), round(all_genders.count("female") / len(known), 3)


#  LLM false-positive filter

def _llm_verify_sponsorship(
    db: Session,
    brand_name: str,
    title: str,
    description: str,
    detected_type: str,
) -> tuple[bool, str]:
    """
    Ask the LLM to verify whether a video is a genuine sponsorship for the brand
    or a false positive detected by the regex.

    Returns (is_genuine: bool, reason: str).
    Falls back to True (keep the video) on any error so no data is lost.
    Only called when ENABLE_LLM=true and OPENAI_KEY is set.
    """
    prompt = fill_template(
        _get_sponsor_check_prompt(db),
        brand_name=brand_name,
        detected_type=detected_type,
        title=title,
        description=description[:1500],
    )

    text = call_gpt_text(prompt, context=title[:60])
    if not text:
        return True, "LLM error — kept by default"

    result_line = next((l for l in text.splitlines() if l.startswith("RESULT:")), "")
    reason_line = next((l for l in text.splitlines() if l.startswith("REASON:")), "")
    is_genuine  = "YES" in result_line.upper()
    reason      = reason_line.replace("REASON:", "").strip()
    logger.debug(
        "LLM verify '%s' for brand '%s': %s — %s",
        title[:60], brand_name, "GENUINE" if is_genuine else "FALSE POSITIVE", reason,
    )
    return is_genuine, reason


#  Main enrichment function 

def _build_queries(brand_name: str) -> list[tuple[str, str]]:
    """
    Return (query, tier) pairs ordered high → low confidence.

    Every query is a single exact phrase that combines the sponsorship keyword
    and brand name together — e.g. "sponsored by Nike" — so YouTube only
    returns videos where those words appear next to each other.
    Using two separate quoted terms like "sponsored by" "Nike" would also match
    fan videos that happen to mention the brand anywhere in the description.
    """
    high: list[tuple[str, str]] = [
        (f'"sponsored by {brand_name}"',                     "paid_sponsor"),
        (f'"paid partnership with {brand_name}"',            "paid_sponsor"),
        (f'"paid promotion by {brand_name}"',                "paid_sponsor"),
        (f'"ad {brand_name}"',                               "paid_sponsor"),
    ]
    medium: list[tuple[str, str]] = [
        (f'"{brand_name} affiliate link"',                   "affiliate"),
        (f'"{brand_name} promo code"',                       "affiliate"),
        (f'"{brand_name} discount code"',                    "affiliate"),
        (f'"use code {brand_name}"',                         "affiliate"),
        (f'"partner with {brand_name}"',                     "affiliate"),
    ]
    low: list[tuple[str, str]] = [
        (f'"gifted by {brand_name}"',                        "gifted"),
        (f'"collab with {brand_name}"',                      "gifted"),
        (f'"in collaboration with {brand_name}"',            "gifted"),
        (f'"{brand_name} sent me"',                          "gifted"),
    ]
    return high + medium + low


def enrich_youtube_sponsorships(
    db: Session, limit: int = 50, brand_id: int | None = None, niche: str | None = None
) -> int:
    """
    For each brand with youtube_checked=False:
      1. Run 14 tier-based YouTube searches (brand name embedded in every query)
      2. Fetch video details and analyse descriptions
      3. Store sponsorship rows in youtube_sponsorships
      4. Mark youtube_checked=True

    Quota cost: 14 queries × 100 units = 1,400 units per brand.
    Free tier (10,000 units/day) → ~7 brands/day.

    Pass brand_id to target one specific brand directly — this bypasses the
    youtube_checked filter (so you can re-run/test a brand that was already
    processed).

    Pass niche to scope the run to brands.niche matching that value exactly
    (case-insensitive) — brands_raw.niche is stored verbatim as typed at
    seed time (see pipeline/seed.py), so this must match that same string.
    Ignored if brand_id is also given.

    Returns number of brands processed.
    """
    if not YOUTUBE_API_KEY:
        logger.warning("YOUTUBE_API_KEY not set — skipping YouTube sponsorship detection")
        return 0

    global quota_fully_exhausted
    quota_fully_exhausted = False

    # name is required — this module's whole methodology embeds the brand
    # name in every search query (_build_queries), which a bare brand (from
    # content_creator_re / brand_wikidata_lookup, name IS NULL) has none of.
    query = db.query(BrandRaw).filter(
        BrandRaw.name.isnot(None),
        BrandRaw.has_official_website.is_(True),
    )
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    else:
        query = query.filter(BrandRaw.youtube_checked == False)
        if niche:
            query = query.filter(func.lower(BrandRaw.niche) == niche.strip().lower())

    brands: list[BrandRaw] = query.limit(limit).all()

    if not brands:
        logger.info("YouTube sponsorships: no pending brands")
        return 0

    logger.info("YouTube sponsorships: processing %d brands", len(brands))
    total_videos = 0

    for brand in brands:
        name = brand.name
        queries = _build_queries(name)

        global _transient_failures
        _transient_failures = 0

        seen_video_ids: set[str] = set()
        try:
            for query, tier in queries:
                ids = _search_videos(query)
                new_ids = [vid for vid in ids if vid not in seen_video_ids]
                seen_video_ids.update(new_ids)
                if new_ids:
                    logger.debug("YouTube: '%s' [%s] → %d new videos", name, tier, len(new_ids))
                time.sleep(0.3)
        except _QuotaExhausted:
            quota_fully_exhausted = True
            logger.warning(
                "YouTube daily quota exhausted — stopping. "
                "'%s' and remaining brands left as youtube_checked=False and will retry tomorrow.",
                name,
            )
            break  # exit brand loop; brand stays unchecked

        if not seen_video_ids:
            if _transient_failures:
                logger.warning(
                    "YouTube: '%s' — no videos found, but %d search call(s) failed "
                    "(timeout/network/API error) — leaving youtube_checked=False so "
                    "this brand is retried instead of wrongly treated as a real zero-result search",
                    name, _transient_failures,
                )
                continue
            brand.youtube_checked = True
            db.commit()
            logger.debug("YouTube: '%s' — no videos found", name)
            continue

        # Fetch full details for all found videos
        videos = _fetch_video_details(list(seen_video_ids))
        rows_to_insert = []

        # Collect unique channel IDs to batch-fetch subscriber counts
        channel_ids = list({v.get("snippet", {}).get("channelId") for v in videos if v.get("snippet", {}).get("channelId")})
        sub_counts = _fetch_channel_subscribers_batch(channel_ids)

        quota_exhausted = False
        try:
            for video in videos:
                snippet    = video.get("snippet", {})
                stats      = video.get("statistics", {})
                title      = snippet.get("title", "")
                description = snippet.get("description", "")
                channel_id  = snippet.get("channelId", "")
                video_id    = video.get("id", "")

                stype, confidence, keywords, desc_snippet = _detect_sponsorship(
                    description, title, name
                )

                if stype == "none":
                    continue  # skip videos with no sponsorship signal

                #  LLM false-positive check (only when ENABLE_LLM=true)
                if ENABLE_LLM and OPENAI_KEY:
                    is_genuine, llm_reason = _llm_verify_sponsorship(
                        db, name, title, description, stype
                    )
                    if not is_genuine:
                        logger.info(
                            "LLM rejected '%s' for '%s' as false positive: %s",
                            title[:60], name, llm_reason,
                        )
                        continue

                # Comments only fetched for videos actually kept — cheap
                # (1 quota unit/page), but no reason to spend it on rejected videos.
                comments = _fetch_video_comments(video_id)
                time.sleep(0.1)

                male_pct, female_pct = _classify_commenter_genders(db, comments)

                rows_to_insert.append({
                    "brand_raw_id":       brand.id,
                    "video_id":           video_id,
                    "video_title":        title,
                    "video_url":          f"https://www.youtube.com/watch?v={video_id}",
                    "channel_id":         channel_id,
                    "channel_name":       snippet.get("channelTitle", ""),
                    "subscriber_count":   sub_counts.get(channel_id),
                    "tier_fit":           bucket_creator_tier(sub_counts.get(channel_id)),
                    "published_at":       snippet.get("publishedAt", ""),
                    "view_count":         int(stats.get("viewCount", 0) or 0),
                    "like_count":         int(stats.get("likeCount", 0) or 0),
                    "description_snippet": desc_snippet,
                    "sponsorship_type":   stype,
                    "confidence":         confidence,
                    "matched_keywords":   keywords,
                    "comments":           comments or None,
                    "male_pct":           male_pct,
                    "female_pct":         female_pct,
                })
        except _QuotaExhausted:
            quota_exhausted = True
            quota_fully_exhausted = True
            logger.warning(
                "YouTube daily quota exhausted while fetching comments for '%s' — "
                "storing what's already been built; brand stays unchecked and the "
                "remaining videos will be retried on the next run.",
                name,
            )

        if rows_to_insert:
            inserted = upsert_rows(db, YoutubeSponsorship, rows_to_insert, ["video_id"])
            total_videos += inserted    
            logger.info("YouTube: '%s' → %d/%d videos stored", name, inserted, len(rows_to_insert))

        if quota_exhausted:
            db.commit()
            break  # exit brand loop; brand stays unchecked, no point trying the next one either

        if _transient_failures:
            db.commit()  # keep whatever rows_to_insert already stored above
            logger.warning(
                "YouTube: '%s' — %d API call(s) failed (timeout/network/API error) while "
                "fetching video/channel/comment details — leaving youtube_checked=False so "
                "this brand is retried instead of being treated as fully processed",
                name, _transient_failures,
            )
            time.sleep(0.5)
            continue

        brand.youtube_checked = True
        db.commit()
        time.sleep(0.5)

    processed_names = ", ".join(b.name for b in brands)
    logger.info(
        "YouTube sponsorships: %d brands processed (%s), %d videos stored",
        len(brands), processed_names, total_videos,
    )
    return len(brands)
