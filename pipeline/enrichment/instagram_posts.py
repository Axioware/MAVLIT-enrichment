"""
pipeline/enrichment/instagram_posts.py

Scrapes recent Instagram posts for each brand using the Apify
Instagram Scraper actor (shu8hvrXbJbY3Eb9W).

For each brand with:
  - instagram_handle set in brands_raw
  - instagram_checked = False

The actor fetches posts from the last `posts_newer_than` period,
stores each post in the instagram_posts table, and marks
instagram_checked=True on brands_raw.

"""

import logging
import time

from apify_client import ApifyClient
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config import APIFY_TOKEN
from pipeline.db import BrandRaw, InstagramPost

logger = logging.getLogger(__name__)

_ACTOR_ID       = "shu8hvrXbJbY3Eb9W"   # apify/instagram-scraper
_POSTS_NEWER    = "12 months"            # default lookback window
_RESULTS_TYPE   = "posts"


def _scrape_handle(handle: str, posts_newer_than: str) -> list[dict]:
    """Run Apify actor for one Instagram handle and return raw items."""
    client = ApifyClient(APIFY_TOKEN)
    run_input = {
        "addParentData":      True,
        "directUrls":         [f"https://www.instagram.com/{handle}/"],
        "onlyPostsNewerThan": posts_newer_than,
        "resultsType":        _RESULTS_TYPE,
    }
    logger.info("Instagram: scraping @%s (newer than %s)", handle, posts_newer_than)
    try:
        run = client.actor(_ACTOR_ID).call(run_input=run_input)
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        logger.info("Instagram: @%s → %d items from Apify", handle, len(items))
        return items
    except Exception as exc:
        logger.error("Instagram: Apify actor failed for @%s — %s", handle, exc)
        return []


def _build_row(brand_raw_id: int, handle: str, item: dict) -> dict | None:
    """Map a raw Apify item to an instagram_posts table row. Returns None if no post_id."""
    post_id = item.get("id")
    if not post_id:
        return None

    return {
        "brand_raw_id":           brand_raw_id,
        "instagram_handle":       handle,
        "post_id":                str(post_id),
        "short_code":             item.get("shortCode"),
        "post_url":               item.get("url"),
        "post_type":              item.get("type"),
        "timestamp":              item.get("timestamp"),
        "caption":                item.get("caption"),
        "hashtags":               item.get("hashtags"),
        "mentions":               item.get("mentions"),
        "tagged_users":           item.get("taggedUsers"),
        "coauthor_producers":     item.get("coauthorProducers"),
        "paid_partnership":       item.get("paidPartnership"),
        "sponsors":               item.get("sponsors"),
        "likes_count":            item.get("likesCount"),
        "comments_count":         item.get("commentsCount"),
        "video_view_count":       item.get("videoViewCount"),
        "video_play_count":       item.get("videoPlayCount"),
        "followers_count":        item.get("followersCount"),
        "follows_count":          item.get("followsCount"),
        "posts_count":            item.get("postsCount"),
        "is_business_account":    item.get("isBusinessAccount"),
        "verified":               item.get("verified"),
        "biography":              item.get("biography"),
        "external_url":           item.get("externalUrl"),
        "business_category_name": item.get("businessCategoryName"),
    }


def _insert_posts(db: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = (
        pg_insert(InstagramPost)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["post_id"])
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount


def enrich_instagram_posts(
    db: Session,
    limit: int = 50,
    posts_newer_than: str = _POSTS_NEWER,
) -> int:
    """
    For each brand with instagram_handle set and instagram_checked=False:
      1. Run Apify scraper for the brand's IG handle
      2. Store posts in instagram_posts table
      3. Mark instagram_checked=True on brands_raw

    Returns number of brands processed.
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping Instagram enrichment")
        return 0

    brands: list[BrandRaw] = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.instagram_handle.isnot(None),
            BrandRaw.instagram_checked == False,
        )
        .limit(limit)
        .all()
    )

    if not brands:
        logger.info("Instagram: no pending brands with instagram_handle")
        return 0

    logger.info("Instagram: processing %d brands", len(brands))
    total_posts = 0

    for brand in brands:
        raw = brand.instagram_handle.strip()
        # Handle both "username", "@username", and full URLs
        if "/" in raw:
            # e.g. "https://www.instagram.com/fairfaxandfavor/" → "fairfaxandfavor"
            handle = raw.rstrip("/").rsplit("/", 1)[-1]
        else:
            handle = raw.lstrip("@")

        items = _scrape_handle(handle, posts_newer_than)

        rows = [r for item in items if (r := _build_row(brand.id, handle, item))]
        inserted = _insert_posts(db, rows)
        total_posts += inserted

        logger.info(
            "Instagram: '%s' (@%s) → %d/%d posts stored",
            brand.name, handle, inserted, len(rows),
        )

        brand.instagram_checked = True
        db.commit()
        time.sleep(1.0)   # be kind to Apify rate limits between brands

    logger.info(
        "Instagram: %d brands processed, %d posts stored",
        len(brands), total_posts,
    )
    return len(brands)
