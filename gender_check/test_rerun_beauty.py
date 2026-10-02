"""TEMPORARY test script — delete after use.

Re-runs run_brand_website_audience_check (with --force, so existing results are
overwritten) for every brand whose partner creator niche is Beauty, Music,
Health or Fitness AND whose current target_audience_gender is "both".

A brand's niche is the niche of its best partnership row (the same DISTINCT ON
pick the main script uses: highest sponsorship confidence, then newest post).
All the main script's other conditions still apply
(creators 1-208, confidence >= 90, no referrals, geo_reach 0-40 or NULL, 2026
posts, 5+ Instagram posts, website + description).

Run from the project root:
    python -m gender_check.test_rerun_beauty --dry-run     # list brands only
    python -m gender_check.test_rerun_beauty --limit 5     # first 5 brands
    python -m gender_check.test_rerun_beauty               # all matching brands
"""

import argparse
import logging
import sys

from sqlalchemy import text

from gender_check import run_brand_website_audience_check as website_check
from pipeline.db import SessionLocal

BRAND_IDS_QUERY = text("""
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
    SELECT brand_raw_id, name, niche
    FROM best_per_brand
    WHERE niche IN ('Beauty', 'Music', 'Health', 'Fitness')
      AND has_official_website = true
      AND website IS NOT NULL
      AND description IS NOT NULL
      AND lower(target_audience_gender) = 'both'
    ORDER BY niche, name
""")

TARGET_AUDIENCE_QUERY = text("""
    SELECT
      target_audience_gender AS gender,
      target_audience_gender_confidence AS confidence,
      audience_analysis_explanation AS explanation
    FROM brands_raw
    WHERE id = :brand_id
""")

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TEMP: re-run the website audience check for Beauty/Music/Health/Fitness brands with target 'both'."
    )
    parser.add_argument("--dry-run", action="store_true", help="List the brands without scraping or calling the LLM.")
    parser.add_argument("--limit", type=int, help="Only process the first N brands.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = db.execute(BRAND_IDS_QUERY).mappings().all()
    finally:
        db.close()

    if args.limit is not None:
        rows = rows[:args.limit]
    if not rows:
        print("No Beauty/Music/Health/Fitness brands with target audience \"both\" matched.")
        return 0

    by_niche: dict[str, int] = {}
    for row in rows:
        by_niche[row["niche"]] = by_niche.get(row["niche"], 0) + 1
    print(
        f"{len(rows)} brand(s) with target audience \"both\" ("
        + ", ".join(f"{niche}: {count}" for niche, count in sorted(by_niche.items())) + "):"
    )
    for row in rows:
        print(f"  {row['brand_raw_id']:>5}  {row['niche']:8}  {row['name']}")

    if args.dry_run:
        sys.argv = [website_check.__file__, "--force", "--dry-run"]
        for row in rows:
            sys.argv += ["--brand-id", str(row["brand_raw_id"])]
        return website_check.main()

    # One brand at a time so the previous -> now line is logged as soon as
    # each brand finishes. --force re-runs brands that already have results.
    summary = []
    for index, row in enumerate(rows, start=1):
        brand_id = row["brand_raw_id"]
        before = _target_audience(brand_id)
        sys.argv = [website_check.__file__, "--force", "--brand-id", str(brand_id)]
        website_check.main()
        after = _target_audience(brand_id)

        line = _compare_line(f"[{row['niche']}] {row['name']}", brand_id, before, after)
        summary.append(line)
        logger.info("[%d/%d] %s", index, len(rows), line)

    print("\n===== TARGET AUDIENCE: PREVIOUS -> NOW =====")
    for line in summary:
        print(line)
    return 0


def _target_audience(brand_id: int) -> dict:
    db = SessionLocal()
    try:
        return dict(db.execute(TARGET_AUDIENCE_QUERY, {"brand_id": brand_id}).mappings().one())
    finally:
        db.close()


def _compare_line(name: str, brand_id: int, before: dict, after: dict) -> str:
    previous = f"{before['gender'] or 'none'} ({before['confidence'] or 0})"
    # Unchanged explanation means the brand was skipped (e.g. its website did not load).
    if after["explanation"] == before["explanation"]:
        return f"{name} ({brand_id}): previous target audience {previous}, now NOT RE-ANALYSED (skipped)"
    now = f"{after['gender'] or 'none'} ({after['confidence'] or 0})"
    changed = "CHANGED" if (before["gender"] or "") != (after["gender"] or "") else "same"
    return f"{name} ({brand_id}): previous target audience {previous}, now {now}  [{changed}]"


if __name__ == "__main__":
    raise SystemExit(main())
