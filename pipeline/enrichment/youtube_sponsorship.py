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

Requires YOUTUBE_API_KEY in config.
YouTube Data API v3 quota cost: ~200 units per brand (2 searches × 100 units).
Free tier: 10,000 units/day → ~50 brands/day.
"""

import logging
import re
import time

import httpx
from sqlalchemy.orm import Session

from config import YOUTUBE_API_KEY, YOUTUBE_API_KEY_1, MISTRAL_API_KEY, ENABLE_LLM
from pipeline.db import BrandRaw, YoutubeSponsorship
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.llm import call_mistral_text

logger = logging.getLogger(__name__)

_SEARCH_URL   = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL   = "https://www.googleapis.com/youtube/v3/videos"
_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_MAX_RESULTS  = 10   # per search query (keeps quota low)

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
_KEY_NAMES = ["YOUTUBE_API_KEY", "YOUTUBE_API_KEY_1"]

# Populated lazily so config is read after load_dotenv()
_API_KEYS: list[str] = []
_key_index: int = 0


def _active_key() -> str:
    global _API_KEYS
    if not _API_KEYS:
        _API_KEYS = [k for k in [YOUTUBE_API_KEY, YOUTUBE_API_KEY_1] if k]
    if not _API_KEYS:
        raise _QuotaExhausted("No YouTube API keys configured — set YOUTUBE_API_KEY in .env")
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


def _yt_get(url: str, params: dict) -> dict | None:
    params["key"] = _active_key()
    try:
        resp = httpx.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            _rotate_key()                       # logs which key ran out and which is next
            params["key"] = _active_key()
            resp = httpx.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                next_name = _KEY_NAMES[_key_index] if _key_index < len(_KEY_NAMES) else f"key_{_key_index}"
                logger.warning("YouTube quota exhausted — %s daily limit also reached. All keys used up.", next_name)
                raise _QuotaExhausted("All YouTube API keys exhausted for today")
        if resp.status_code == 403:
            logger.warning("YouTube API forbidden (403) — check API key or permissions")
            return None
        resp.raise_for_status()
        return resp.json()
    except _QuotaExhausted:
        raise
    except Exception as exc:
        logger.warning("YouTube API call failed: %s", exc)
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


def _fetch_channel_subscribers(channel_id: str) -> int | None:
    """Return subscriber count for a channel, or None on failure."""
    data = _yt_get(_CHANNELS_URL, {
        "part": "statistics",
        "id":   channel_id,
    })
    if not data:
        return None
    items = data.get("items", [])
    if not items:
        return None
    count = items[0].get("statistics", {}).get("subscriberCount")
    return int(count) if count else None


#  LLM false-positive filter 

def _llm_verify_sponsorship(
    brand_name: str,
    title: str,
    description: str,
    detected_type: str,
) -> tuple[bool, str]:
    """
    Ask Mistral to verify whether a video is a genuine sponsorship for the brand
    or a false positive detected by the regex.

    Returns (is_genuine: bool, reason: str).
    Falls back to True (keep the video) on any error so no data is lost.
    Only called when ENABLE_LLM=true and MISTRAL_API_KEY is set.
    """
    prompt = f"""You are verifying whether a YouTube video is a genuine brand sponsorship or a false positive.

Brand: {brand_name}
Detected sponsorship type: {detected_type}
Video title: {title}

Video description (first 1500 chars):
{description[:1500]}

---

Is this video genuinely sponsored by or affiliated with "{brand_name}"?

Answer with ONLY this format:
RESULT: YES or NO
REASON: one short sentence explaining why"""

    text = call_mistral_text(prompt, context=title[:60])
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


def enrich_youtube_sponsorships(db: Session, limit: int = 50, brand_id: int | None = None) -> int:
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

    Returns number of brands processed.
    """
    if not YOUTUBE_API_KEY:
        logger.warning("YOUTUBE_API_KEY not set — skipping YouTube sponsorship detection")
        return 0

    query = db.query(BrandRaw)
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    else:
        query = query.filter(BrandRaw.youtube_checked == False)

    brands: list[BrandRaw] = query.limit(limit).all()

    if not brands:
        logger.info("YouTube sponsorships: no pending brands")
        return 0

    logger.info("YouTube sponsorships: processing %d brands", len(brands))
    total_videos = 0

    for brand in brands:
        name = brand.name
        queries = _build_queries(name)

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
            logger.warning(
                "YouTube daily quota exhausted — stopping. "
                "'%s' and remaining brands left as youtube_checked=False and will retry tomorrow.",
                name,
            )
            break  # exit brand loop; brand stays unchecked

        if not seen_video_ids:
            brand.youtube_checked = True
            db.commit()
            logger.debug("YouTube: '%s' — no videos found", name)
            continue

        # Fetch full details for all found videos
        videos = _fetch_video_details(list(seen_video_ids))
        rows_to_insert = []

        # Collect unique channel IDs to batch-fetch subscriber counts
        channel_ids = list({v.get("snippet", {}).get("channelId") for v in videos if v.get("snippet", {}).get("channelId")})
        sub_counts: dict[str, int | None] = {}
        for cid in channel_ids:
            sub_counts[cid] = _fetch_channel_subscribers(cid)
            time.sleep(0.1)

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
            if ENABLE_LLM and MISTRAL_API_KEY:
                is_genuine, llm_reason = _llm_verify_sponsorship(
                    name, title, description, stype
                )
                if not is_genuine:
                    logger.info(
                        "LLM rejected '%s' for '%s' as false positive: %s",
                        title[:60], name, llm_reason,
                    )
                    continue

            rows_to_insert.append({
                "brand_raw_id":       brand.id,
                "video_id":           video_id,
                "video_title":        title,
                "video_url":          f"https://www.youtube.com/watch?v={video_id}",
                "channel_id":         channel_id,
                "channel_name":       snippet.get("channelTitle", ""),
                "subscriber_count":   sub_counts.get(channel_id),
                "published_at":       snippet.get("publishedAt", ""),
                "view_count":         int(stats.get("viewCount", 0) or 0),
                "like_count":         int(stats.get("likeCount", 0) or 0),
                "description_snippet": desc_snippet,
                "sponsorship_type":   stype,
                "confidence":         confidence,
                "matched_keywords":   keywords,
            })

        if rows_to_insert:
            inserted = upsert_rows(db, YoutubeSponsorship, rows_to_insert, ["video_id"])
            total_videos += inserted
            logger.info("YouTube: '%s' → %d/%d videos stored", name, inserted, len(rows_to_insert))

        brand.youtube_checked = True
        db.commit()
        time.sleep(0.5)

    processed_names = ", ".join(b.name for b in brands)
    logger.info(
        "YouTube sponsorships: %d brands processed (%s), %d videos stored",
        len(brands), processed_names, total_videos,
    )
    return len(brands)
