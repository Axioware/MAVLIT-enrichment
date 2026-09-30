"""Fetch and store five recent Instagram posts for qualifying brands.

Run from the project root:
    python -m pipeline.enrichment.run_qualifying_brand_instagram_posts --dry-run
    python -m pipeline.enrichment.run_qualifying_brand_instagram_posts --limit 10
"""

import argparse
import logging
import time

from sqlalchemy import text

from config import APIFY_TOKEN, OPENAI_KEY
from pipeline.db import BrandRaw, InstagramPost, SessionLocal
from pipeline.enrichment.instagram_posts import (
    _build_profile_only_row,
    _build_row,
    _get_sponsorship_confidence_prompt,
    _llm_filter_all,
    _real_coauthors,
    _save_post_with_confidence,
    _scrape_handle,
    _usernames_only,
)
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.social import normalize_handle

POSTS_PER_BRAND = 5
logger = logging.getLogger(__name__)

# This selects the same population as the user's count query, at brand level.
QUALIFYING_BRANDS_QUERY = text("""
    WITH best_per_brand AS (
      SELECT DISTINCT ON (tcbp.brand_raw_id)
        tcbp.brand_raw_id,
        ccr.niche,
        br.has_official_website,
        br.description
      FROM content_creator_re ccr
      JOIN test_creator_brand_partnership_posts tcbp
        ON tcbp.content_creator_re_id = ccr.id
      JOIN brands_raw br ON br.id = tcbp.brand_raw_id
      WHERE ccr.id BETWEEN 1 AND 208
        AND tcbp.sponsorship_confidence >= 90
        AND br.refferls = false
        AND (br.geo_reach_score BETWEEN 0 AND 40 OR br.geo_reach_score IS NULL)
        AND tcbp.post_timestamp >= '2026-01-01'
        AND tcbp.post_timestamp < '2027-01-01'
      ORDER BY tcbp.brand_raw_id,
        tcbp.sponsorship_confidence DESC NULLS LAST,
        tcbp.post_timestamp DESC NULLS LAST
    ),
    brands_with_posts AS (
      SELECT brand_raw_id
      FROM instagram_posts
      GROUP BY brand_raw_id
      HAVING COUNT(*) > 4
    )
    SELECT b.brand_raw_id
    FROM best_per_brand b
    JOIN brands_with_posts ip ON ip.brand_raw_id = b.brand_raw_id
    WHERE b.niche IN ('Music', 'Beauty', 'Fitness', 'Health')
      AND b.has_official_website = true
      AND b.description IS NOT NULL
      AND TRIM(b.description) <> ''
    ORDER BY b.niche, b.brand_raw_id
""")


def _process_brand(db, brand: BrandRaw) -> int | None:
    """Scrape, filter, and save one brand's five recent posts.

    Returns None on scrape failure (so the caller can leave the brand eligible
    for retry), otherwise the number of inserted rows.
    """
    handle = normalize_handle(brand.instagram_handle)
    if not handle:
        logger.warning("Skipping brand_raw_id=%s: no Instagram handle", brand.id)
        return 0

    items = _scrape_handle(handle, POSTS_PER_BRAND)
    if items is None:
        logger.warning("Instagram scrape failed for brand_raw_id=%s (@%s)", brand.id, handle)
        return None

    sponsorship_prompt = _get_sponsorship_confidence_prompt(db) if OPENAI_KEY else None
    inserted = 0
    for item in items[:POSTS_PER_BRAND]:
        row = _build_row(brand.id, handle, item)
        if row is None:
            continue

        has_creator_signals = bool(
            item.get("sponsors")
            or _real_coauthors(item, handle)
            or item.get("taggedUsers")
            or item.get("mentions")
        )
        if has_creator_signals:
            filtered = _llm_filter_all(db, item, brand.name or "Unknown", handle)
            if filtered is not None:
                row["llm_checked"] = True
                if any((
                    filtered.get("sponsors"),
                    filtered.get("tagged_users"),
                    filtered.get("mentions"),
                    filtered.get("coauthor_producers"),
                )):
                    row["paid_partnership"] = bool(filtered.get("paid_partnership"))
                    row["sponsors"] = filtered.get("sponsors") or None
                    row["tagged_users"] = _usernames_only(filtered.get("tagged_users"))
                    row["mentions"] = _usernames_only(filtered.get("mentions"))
                    row["coauthor_producers"] = _usernames_only(filtered.get("coauthor_producers"))

        inserted += _save_post_with_confidence(db, brand.name or "Unknown", row, sponsorship_prompt)
        time.sleep(0.3)

    if inserted == 0:
        upsert_rows(
            db,
            InstagramPost,
            [_build_profile_only_row(brand.id, handle, items)],
            ["brand_raw_id"],
            index_where=text("post_id IS NULL"),
        )
    brand.instagram_checked = True
    db.commit()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch five recent Instagram posts for qualifying brands.")
    parser.add_argument("--limit", type=int, help="Maximum number of qualifying brands to process.")
    parser.add_argument("--dry-run", action="store_true", help="List qualifying brands without scraping.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if not args.dry_run and not APIFY_TOKEN:
        parser.error("APIFY_TOKEN is required unless --dry-run is used")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = SessionLocal()
    try:
        brand_ids = [row[0] for row in db.execute(QUALIFYING_BRANDS_QUERY).all()]
        if args.limit:
            brand_ids = brand_ids[:args.limit]
        brands = db.query(BrandRaw).filter(BrandRaw.id.in_(brand_ids)).all() if brand_ids else []
        brands_by_id = {brand.id: brand for brand in brands}
        logger.info("%d qualifying brands selected", len(brands))

        for brand_id in brand_ids:
            brand = brands_by_id.get(brand_id)
            if brand is None:
                continue
            if args.dry_run:
                logger.info("DRY RUN brand_raw_id=%s brand=%s", brand.id, brand.name)
                continue
            count = _process_brand(db, brand)
            if count is None:
                continue
            logger.info("Processed brand_raw_id=%s brand=%s fetched up to %d posts; %d inserted", brand.id, brand.name, POSTS_PER_BRAND, count)
            time.sleep(1.0)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
