"""
pipeline/enrichment/tiktok_posts.py

Scrapes recent TikTok videos for each brand using the Apify
TikTok Scraper actor (LDHaKxTPJBXL4tgAt).

For each brand with:
  - tiktok_handle set in brands_raw
  - tiktok_checked = False

Only saves posts that have at least one signal:
  - is_sponsored = True
  - is_ad = True
  - mentions (any @username in the video)

Requires APIFY_TOKEN in config / .env.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from config import APIFY_TOKEN
from pipeline.db import BrandRaw, TiktokPost
from pipeline.helpers.apify import run_apify_actor
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_ACTOR_ID        = "LDHaKxTPJBXL4tgAt"   # apify/tiktok-scraper
_RESULTS_PER_PAGE = 5000
_LOOKBACK_DAYS   = 30


def _scrape_handle(handle: str, lookback_days: int) -> list[dict]:
    """Run Apify actor for one TikTok handle and return raw post items."""
    logger.info("TikTok: scraping @%s (last %d days)", handle, lookback_days)
    start_date = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")
    run_input = {
        "profiles":              [handle],
        "profileScrapeSections": ["videos"],
        "profileSorting":        "latest",
        "resultsPerPage":        _RESULTS_PER_PAGE,
        "oldestPostDateUnified": start_date,
        "excludePinnedPosts":    True,
    }
    return run_apify_actor(_ACTOR_ID, run_input, label=f"TikTok @{handle}")


def _has_signal(item: dict) -> bool:
    """Return True only if the post has at least one sponsorship/collaboration signal."""
    return bool(
        item.get("isSponsored")
        or item.get("isAd")
        or item.get("mentions")
        or item.get("detailedMentions")
    )


def _extract_mentions(item: dict) -> list[str]:
    """Combine mentions and detailedMentions into a flat list of usernames."""
    seen: set[str] = set()
    for m in item.get("mentions", []):
        seen.add(str(m))
    for m in item.get("detailedMentions", []):
        username = m.get("uniqueId") or m.get("username") if isinstance(m, dict) else str(m)
        if username:
            seen.add(username)
    return list(seen)


def _build_row(brand_raw_id: int, handle: str, item: dict) -> dict | None:
    """Map a raw Apify item to a tiktok_posts row. Returns None if no signal or no video_id."""
    video_id = item.get("id")
    if not video_id:
        return None
    if not _has_signal(item):
        return None

    hashtags = []
    for tag in item.get("hashtags", []):
        if isinstance(tag, dict):
            name = tag.get("name")
            if name:
                hashtags.append(name)
        else:
            hashtags.append(str(tag))

    return {
        "brand_raw_id":  brand_raw_id,
        "tiktok_handle": handle,
        "video_id":      str(video_id),
        "video_url":     item.get("webVideoUrl"),
        "create_time":   item.get("createTimeISO"),
        "play_count":    item.get("playCount") or 0,
        "like_count":    item.get("diggCount") or 0,
        "comment_count": item.get("commentCount") or 0,
        "share_count":   item.get("shareCount") or 0,
        "collect_count": item.get("collectCount") or 0,
        "is_sponsored":  item.get("isSponsored") or False,
        "is_ad":         item.get("isAd") or False,
        "mentions":      _extract_mentions(item) or None,
        "hashtags":      hashtags or None,
    }


def enrich_tiktok_posts(
    db: Session,
    limit: int = 50,
    lookback_days: int = _LOOKBACK_DAYS,
) -> int:
    """
    For each brand with tiktok_handle set and tiktok_checked=False:
      1. Run Apify TikTok scraper for the brand's handle
      2. Store posts with sponsorship signals in tiktok_posts table
      3. Mark tiktok_checked=True on brands_raw

    Only saves posts where is_sponsored, is_ad, or mentions are present.
    Returns number of brands processed.
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping TikTok enrichment")
        return 0

    brands: list[BrandRaw] = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.tiktok_handle.isnot(None),
            BrandRaw.tiktok_checked == False,
        )
        .limit(limit)
        .all()
    )

    if not brands:
        logger.info("TikTok: no pending brands with tiktok_handle")
        return 0

    logger.info("TikTok: processing %d brands", len(brands))
    total_posts = 0

    for brand in brands:
        handle = normalize_handle(brand.tiktok_handle)

        items    = _scrape_handle(handle, lookback_days)
        rows     = [r for item in items if (r := _build_row(brand.id, handle, item))]
        inserted = upsert_rows(db, TiktokPost, rows, ["video_id"])
        total_posts += inserted

        logger.info(
            "TikTok: '%s' (@%s) → %d stored / %d with signals / %d total fetched",
            brand.name, handle, inserted, len(rows), len(items),
        )

        brand.tiktok_checked = True
        db.commit()
        time.sleep(1.0)

    logger.info("TikTok: %d brands processed, %d posts stored", len(brands), total_posts)
    return len(brands)
