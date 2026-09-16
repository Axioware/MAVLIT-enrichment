"""
One-time backfill for brands_raw.geo_reach_country_codes.

Uses the existing geo reach result fields as input:
  - geo_reach_score
  - geo_reach_label
  - geo_reach_locations
  - geo_reach_pages_scraped

Default behavior updates checked rows whose geo_reach_country_codes is NULL.
Pass --overwrite to regenerate codes for all checked rows.
"""

import argparse
import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import OPENAI_KEY
from pipeline.db import BrandRaw, SessionLocal
from pipeline.enrichment.geo_reach.geo_reach import _normalize_country_codes
from pipeline.helpers.gpt_llm import call_gpt_json

logger = logging.getLogger(__name__)

PROMPT = """
You are backfilling a compact country-code column for a brand's geographic reach.

Use ONLY the geo reach fields below. They were produced by a previous website crawl.

Brand:
{brand_context}

Rules:
- Return country codes only, not city names, state names, prose, or objects.
- Use uppercase ISO alpha-2 country codes: "US" for United States, "CA" for Canada, "GB" for United Kingdom, etc.
- If a city/state/province is mentioned, infer the country and return that country code. Example: "Austin, Texas" -> "US".
- If multiple countries are found, return all country codes once.
- If geo_reach_score is 0 or geo_reach_label is "global", return ["GLOBAL"].
- If there is no usable reach evidence, return [].
- Do not treat manufacturing origin, "made in X", registered address, legal address, or headquarters-only evidence as market reach.

Respond with ONLY valid JSON:
{"country_codes": []}
"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)


def _ensure_column(db: Session) -> None:
    db.execute(text("ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS geo_reach_country_codes JSONB"))
    db.commit()


def _country_codes_for_brand(brand: BrandRaw) -> list[str]:
    context = {
        "id": brand.id,
        "name": brand.name,
        "geo_reach_score": brand.geo_reach_score,
        "geo_reach_label": brand.geo_reach_label,
        "geo_reach_locations": brand.geo_reach_locations,
        "geo_reach_pages_scraped": brand.geo_reach_pages_scraped,
    }
    prompt = PROMPT.replace("{brand_context}", _json(context))
    result = call_gpt_json(prompt, context=f"geo country-code backfill brand_id={brand.id}")
    return _normalize_country_codes(result.get("country_codes"), brand.geo_reach_score)


def backfill_geo_reach_country_codes(
    db: Session,
    limit: int | None = None,
    brand_id: int | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> int:
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping geo reach country-code backfill")
        return 0

    _ensure_column(db)

    query = db.query(BrandRaw).filter(BrandRaw.geo_reach_checked.is_(True))
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    if not overwrite:
        query = query.filter(BrandRaw.geo_reach_country_codes.is_(None))
    query = query.order_by(BrandRaw.id.asc())
    if limit is not None:
        query = query.limit(limit)

    rows = query.all()
    if not rows:
        logger.info("Geo country-code backfill: no matching rows")
        return 0

    updated = 0
    for brand in rows:
        codes = _country_codes_for_brand(brand)
        logger.info(
            "geo country-code backfill: id=%s name=%s score=%s label=%s codes=%s",
            brand.id, brand.name, brand.geo_reach_score, brand.geo_reach_label, codes,
        )
        if dry_run:
            continue

        brand.geo_reach_country_codes = codes
        db.commit()
        updated += 1

    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="One-time backfill for brands_raw.geo_reach_country_codes.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to process.")
    parser.add_argument("--brand-id", type=int, default=None, dest="brand_id", help="Backfill one brands_raw.id.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate rows that already have country codes.")
    parser.add_argument("--dry-run", action="store_true", help="Call the LLM and log codes without updating the DB.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        updated = backfill_geo_reach_country_codes(
            db,
            limit=args.limit,
            brand_id=args.brand_id,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
        print(f"geo_reach_country_codes_backfill: updated={updated}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
