"""
pipeline/enrichment/twitter_posts.py

Scrapes recent tweets for each brand using the Apify
X (Twitter) Scraper actor (dLXTJpRPXyh3j4d5V).

For each brand with:
  - twitter_handle set in brands_raw
  - twitter_checked = False

Only saves tweets that have at least one sponsorship signal:
  - is_sponsored = True  (matched #ad, #sponsored, paid partnership, etc.)

If the Apify actor run fails, the brand is NOT marked as twitter_checked=True
so it will be retried on the next run.

Requires APIFY_TOKEN in config / .env.
"""

import logging
import re
import time

from apify_client import ApifyClient
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config import APIFY_TOKEN
from pipeline.db import BrandRaw, TwitterPost

logger = logging.getLogger(__name__)

_ACTOR_ID   = "dLXTJpRPXyh3j4d5V"
_MAX_ITEMS  = 50

_SPONSOR_KEYWORDS = [
    "#ad", "#sponsored", "#paidpartnership", "#partner",
    "#gifted", "#collab", "#promotion", "#promo",
    "paid partnership", "in partnership with", "partnered with",
    "thanks to", "thank you to", "brought to you by",
]


def _extract_hashtags(text: str) -> list[str]:
    return list(set(re.findall(r"#\w+", text, flags=re.IGNORECASE)))


def _extract_mentions(text: str) -> list[str]:
    return list(set(re.findall(r"@\w+", text, flags=re.IGNORECASE)))


def _detect_sponsorship(text: str) -> tuple[bool, list[str]]:
    """Return (is_sponsored, matched_signals) based on keyword matching."""
    text_lower = text.lower()
    matched = [kw for kw in _SPONSOR_KEYWORDS if kw in text_lower]
    return bool(matched), matched


def _scrape_handle(handle: str) -> list[dict]:
    """Run Apify actor for one Twitter handle and return raw tweet items."""
    client = ApifyClient(APIFY_TOKEN)
    run_input = {
        "username":  handle,
        "maxItems":  _MAX_ITEMS,
        "retweets":  "exclude",
        "replies":   "exclude",
        "quotes":    "exclude",
    }
    logger.info("Twitter: scraping @%s", handle)
    try:
        run = client.actor(_ACTOR_ID).call(run_input=run_input)
        if run.get("status") != "SUCCEEDED":
            logger.error("Twitter: Apify actor failed for @%s — status: %s", handle, run.get("status"))
            return None   # None = actor failed (different from [] = no tweets)
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        logger.info("Twitter: @%s → %d items from Apify", handle, len(items))
        return items
    except Exception as exc:
        logger.error("Twitter: Apify actor error for @%s — %s", handle, exc)
        return None   # None = failed


def _build_row(brand_raw_id: int, handle: str, item: dict) -> dict | None:
    """Map a raw Apify tweet to a twitter_posts row. Returns None if no signal."""
    tweet_id = item.get("id")
    if not tweet_id:
        return None

    text = item.get("text", "")
    is_sponsored, signals = _detect_sponsorship(text)

    if not is_sponsored:
        return None

    hashtags = _extract_hashtags(text)
    mentions = _extract_mentions(text)
    tagged   = list(set(
        [f"@{u}" for u in (item.get("replyingTo") or [])] + mentions
    ))

    return {
        "brand_raw_id":    brand_raw_id,
        "twitter_handle":  handle,
        "tweet_id":        str(tweet_id),
        "permalink":       item.get("permalink"),
        "created_at":      item.get("createdAt"),
        "text":            text,
        "hashtags":        hashtags or None,
        "mentions":        tagged or None,
        "is_sponsored":    True,
        "sponsor_signals": signals or None,
        "likes":           item.get("likes") or 0,
        "retweets":        item.get("retweets") or 0,
        "quotes":          item.get("quotes") or 0,
        "comments":        item.get("comments") or 0,
        "has_media":       bool(item.get("images")),
        "username":        item.get("username"),
        "fullname":        item.get("fullname"),
        "verified":        item.get("verified"),
    }


def _insert_posts(db: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = (
        pg_insert(TwitterPost)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["tweet_id"])
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount


def enrich_twitter_posts(db: Session, limit: int = 50) -> int:
    """
    For each brand with twitter_handle set and twitter_checked=False:
      1. Run Apify Twitter scraper for the brand's handle
      2. Store sponsored tweets in twitter_posts table
      3. Mark twitter_checked=True ONLY if the actor run succeeded

    If the actor fails, twitter_checked stays False so it will be retried.
    Returns number of brands processed.
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping Twitter enrichment")
        return 0

    brands: list[BrandRaw] = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.twitter_handle.isnot(None),
            BrandRaw.twitter_checked == False,
        )
        .limit(limit)
        .all()
    )

    if not brands:
        logger.info("Twitter: no pending brands with twitter_handle")
        return 0

    logger.info("Twitter: processing %d brands", len(brands))
    total_posts = 0
    processed   = 0

    for brand in brands:
        raw = brand.twitter_handle.strip()
        # Handle full URL, @username, or bare username
        if "/" in raw:
            handle = raw.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        else:
            handle = raw.lstrip("@")

        items = _scrape_handle(handle)

        if items is None:
            # Actor failed — do NOT mark as checked, will retry next run
            logger.warning("Twitter: skipping DB update for '%s' due to actor failure", brand.name)
            time.sleep(1.0)
            continue

        rows     = [r for item in items if (r := _build_row(brand.id, handle, item))]
        inserted = _insert_posts(db, rows)
        total_posts += inserted

        logger.info(
            "Twitter: '%s' (@%s) → %d stored / %d with signals / %d total fetched",
            brand.name, handle, inserted, len(rows), len(items),
        )

        # Only mark checked if actor succeeded (items is a list, not None)
        brand.twitter_checked = True
        db.commit()
        processed += 1
        time.sleep(1.0)

    logger.info("Twitter: %d brands processed, %d tweets stored", processed, total_posts)
    return processed
