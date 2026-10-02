"""TEMPORARY test script — delete after use.

Re-runs run_brand_website_audience_check (with --force, so existing results are
overwritten) for every brand whose partner creator niche is Beauty AND whose
current target_audience_gender is "both".

A brand counts as Beauty when its best partnership row (the same DISTINCT ON
pick the main script uses: highest sponsorship confidence, then newest post)
comes from a Beauty creator. All the main script's other conditions still apply
(creators 1-208, confidence >= 90, no referrals, geo_reach 0-40 or NULL, 2026
posts, 5+ Instagram posts, website + description).

Run from the project root:
    python -m gender_check.test_rerun_beauty --dry-run     # list brands only
    python -m gender_check.test_rerun_beauty --limit 5     # first 5 brands
    python -m gender_check.test_rerun_beauty               # all Beauty brands
"""

import argparse
import sys

from sqlalchemy import text

from gender_check import run_brand_website_audience_check as website_check
from pipeline.db import SessionLocal

BEAUTY_BRAND_IDS_QUERY = text("""
    WITH best_per_brand AS (
      SELECT DISTINCT ON (tcbp.brand_raw_id)
        tcbp.brand_raw_id,
        ccr.niche,
        br.name,
        br.has_official_website,
        br.website,
        br.description,
        br.target_audience_gender
      FROM content_creator_re ccr
      JOIN test_creator_brand_partnership_posts tcbp
        ON tcbp.content_creator_re_id = ccr.id
      JOIN brands_raw br
        ON br.id = tcbp.brand_raw_id
      WHERE ccr.id BETWEEN 1 AND 208
        AND ccr.niche IN ('Beauty', 'Music', 'Fitness', 'Health')
        AND tcbp.sponsorship_confidence >= 90
        AND br.refferls = false
        AND (
          br.geo_reach_score BETWEEN 0 AND 40
          OR br.geo_reach_score IS NULL
        )
        AND tcbp.post_timestamp >= '2026-01-01'
        AND tcbp.post_timestamp < '2027-01-01'
        AND (
          SELECT COUNT(*)
          FROM instagram_posts ip
          WHERE ip.brand_raw_id = br.id
        ) >= 5
      ORDER BY
        tcbp.brand_raw_id,
        tcbp.sponsorship_confidence DESC NULLS LAST,
        tcbp.post_timestamp DESC NULLS LAST
    )
    SELECT brand_raw_id, name
    FROM best_per_brand
    WHERE niche = 'Beauty'
      AND has_official_website = true
      AND website IS NOT NULL
      AND description IS NOT NULL
      AND lower(target_audience_gender) = 'both'
    ORDER BY name
""")


def main() -> int:
    parser = argparse.ArgumentParser(description="TEMP: re-run the website audience check for Beauty-niche brands.")
    parser.add_argument("--dry-run", action="store_true", help="List the Beauty brands without scraping or calling the LLM.")
    parser.add_argument("--limit", type=int, help="Only process the first N Beauty brands.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = db.execute(BEAUTY_BRAND_IDS_QUERY).mappings().all()
    finally:
        db.close()

    if args.limit is not None:
        rows = rows[:args.limit]
    if not rows:
        print("No Beauty-niche brands with target audience \"both\" matched.")
        return 0

    print(f"{len(rows)} Beauty-niche brand(s) with target audience \"both\":")
    for row in rows:
        print(f"  {row['brand_raw_id']:>5}  {row['name']}")

    # Hand off to the real script with --force so already-analysed brands are re-run.
    forwarded = ["--force"]
    for row in rows:
        forwarded += ["--brand-id", str(row["brand_raw_id"])]
    if args.dry_run:
        forwarded.append("--dry-run")
    sys.argv = [website_check.__file__, *forwarded]
    return website_check.main()


if __name__ == "__main__":
    raise SystemExit(main())
