"""Scrape the latest Instagram posts for brands selected by the Beauty/health query.

Run from the project root:
    python -m gender_check.run_query_brands_instagram_posts

Use --dry-run to list the matching brands without changing flags or calling Apify.
Existing instagram_posts rows are ignored on insert because post_id is unique.
"""

import argparse
import logging

from sqlalchemy import text

from pipeline.db import BrandRaw, SessionLocal
from pipeline.enrichment.instagram_posts import enrich_instagram_posts


BRAND_QUERY = text("""
    WITH best_per_brand AS (
      SELECT DISTINCT ON (tcbp.brand_raw_id)
        ccr.niche,
        tcbp.brand_raw_id,
        tcbp.brand_name,
        br.has_official_website,
        br.description
      FROM content_creator_re ccr
      JOIN test_creator_brand_partnership_posts tcbp
        ON tcbp.content_creator_re_id = ccr.id
      JOIN brands_raw br
        ON br.id = tcbp.brand_raw_id
      WHERE ccr.id BETWEEN 1 AND 208
        AND tcbp.sponsorship_confidence >= 90
        AND br.refferls = false
        AND (
          br.geo_reach_score BETWEEN 0 AND 40
          OR br.geo_reach_score IS NULL
        )
        AND tcbp.post_timestamp >= '2026-01-01'
        AND tcbp.post_timestamp < '2027-01-01'
      ORDER BY
        tcbp.brand_raw_id,
        tcbp.sponsorship_confidence DESC NULLS LAST,
        tcbp.post_timestamp DESC NULLS LAST
    )
    SELECT
      niche,
      brand_raw_id,
      brand_name,
      has_official_website,
      CASE
        WHEN has_official_website = false THEN 'no_website'
        WHEN has_official_website = true AND description IS NULL
          THEN 'website_but_description_null'
      END AS reason
    FROM best_per_brand
    WHERE niche IN ('Music', 'Beauty', 'Fitness', 'Health')
      AND (
        has_official_website = false
        OR (has_official_website = true AND description IS NULL)
      )
    ORDER BY niche, reason, brand_name
""")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Instagram post enrichment for brands returned by BRAND_QUERY."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List query-matched brands without resetting flags or calling Apify.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of query-matched brands to process.",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    db = SessionLocal()
    try:
        brands = db.execute(BRAND_QUERY).mappings().all()
        if args.limit is not None:
            brands = brands[:args.limit]

        logger.info("Query matched %d brand(s).", len(brands))
        processed = 0

        for result in brands:
            brand_id = result["brand_raw_id"]
            brand = db.query(BrandRaw).filter(BrandRaw.id == brand_id).first()
            if brand is None or not brand.instagram_handle:
                logger.info(
                    "Skipping brand_id=%s (%s): Instagram handle is missing.",
                    brand_id,
                    result["brand_name"],
                )
                continue

            if args.dry_run:
                logger.info(
                    "DRY RUN brand_id=%s niche=%s name=%s reason=%s",
                    brand_id,
                    result["niche"],
                    result["brand_name"],
                    result["reason"],
                )
                continue

            if brand.instagram_checked:
                brand.instagram_checked = False
                db.commit()

            logger.info(
                "Scraping up to 20 Instagram posts for brand_id=%s (%s).",
                brand_id,
                result["brand_name"],
            )
            try:
                processed += enrich_instagram_posts(
                    db,
                    limit=1,
                    posts_limit=20,
                    brand_id=brand_id,
                )
            except Exception:
                db.rollback()
                logger.exception(
                    "Instagram post enrichment failed for brand_id=%s; continuing.",
                    brand_id,
                )

        logger.info("Finished query-targeted run; %d brand(s) checked.", processed)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())