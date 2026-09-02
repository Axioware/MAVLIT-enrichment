"""
pipeline/enrichment/brand_tier.py

Classify each brand in brands_raw into a market tier based on likely scale,
brand power, and premium positioning.

Values:
- lower-range
- midlower-range
- midhigher-range
- higher-range

Examples of higher-range brands include major global brands such as Amazon,
Sony Music, or other large premium-market businesses.

Safe to re-run — rows with brand_tier IS NULL are processed, and rows with an
existing value are skipped.
"""

import argparse
import logging

from sqlalchemy.orm import Session

from config import OPENAI_KEY
from pipeline.db import BrandRaw, SessionLocal
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template

logger = logging.getLogger(__name__)

BRAND_TIER_PROMPT = """
You are classifying a brand's sponsorship accessibility tier for influencer marketing.

Your goal is NOT to measure product quality, popularity, or reputation.

Your goal is to estimate how realistic it would be for an independent Instagram content creator (10K–1M followers) to directly approach this brand and secure a sponsorship or partnership.

Choose exactly one value:

- lower-range
- midlower-range
- midhigher-range
- higher-range

Definitions:

lower-range:
- Small businesses, startups, local brands, niche ecommerce stores, independent creators' businesses, small agencies, small SaaS companies, boutique brands, small online shops.
- Limited budget and limited market presence.
- Often actively seeking creators.
- A creator can realistically contact decision-makers directly.

midlower-range:
- Established small-to-medium businesses.
- Regional brands or growing online brands.
- May have marketing staff but limited influencer budgets.
- Creator outreach is still realistic.

midhigher-range:
- Well-established national brands.
- Significant marketing budgets.
- Recognized within their industry.
- May work with influencers regularly.
- Creator outreach is possible but increasingly competitive.

higher-range:
- Large corporations, multinational companies, major entertainment companies, major record labels, major sports organizations, major retailers, public companies, Fortune 500 companies, major consumer brands, major technology companies, major media companies, government organizations, universities, major nonprofits, or globally recognized brands.
- Usually have dedicated marketing, PR, sponsorship, talent, or agency teams.
- Partnerships are typically managed through formal processes, agencies, or existing networks.
- Most independent creators cannot easily secure partnerships through cold outreach.

Important rules:

- Be highly skeptical of assigning anything except higher-range to globally recognized brands.
- Brands such as Amazon, Apple, Google, Microsoft, Sony, Sony Music, Disney, Marvel, Netflix, Adidas, Nike, Coca-Cola, Samsung, Universal Music, Warner Music, BMW, Mercedes-Benz, Spotify, TikTok, Meta, YouTube, and similar organizations are always higher-range.
- Large parent companies, major subsidiaries, and major corporate brands should generally be higher-range.
- Government agencies, universities, major hospitals, museums, sports leagues, and major public institutions should generally be higher-range.
- If information is limited, estimate based on brand name, website, description, and likely commercial scale.
- If you are genuinely unsure, do not force a tier. Set "brand_tier" to null instead of guessing.
- Only return a tier when the evidence is strong enough to support it.

Brand name: {brand_name}
Brand niche: {niche}
Brand description: {description}
Website/domain: {website}
Instagram handle: {instagram_handle}

Respond with ONLY valid JSON:

{"brand_tier":null}
"""

_NORMALIZED_VALUES = {
    "lower-range": "lower-range",
    "lower range": "lower-range",
    "midlower-range": "midlower-range",
    "mid-lower-range": "midlower-range",
    "midhigher-range": "midhigher-range",
    "mid-higher-range": "midhigher-range",
    "higher-range": "higher-range",
    "higher range": "higher-range",
    "higherrange": "higher-range",
}


def _normalize_brand_tier(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower().replace("_", "-")
    if cleaned in {"", "null", "none", "n/a", "na", "unknown", "unsure"}:
        return None
    return _NORMALIZED_VALUES.get(cleaned)


def _classify_brand_tier(db: Session, brand: BrandRaw) -> str | None:
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping brand tier classification for brand_id=%s", brand.id)
        return None

    prompt = fill_template(
        BRAND_TIER_PROMPT,
        brand_name=brand.name or "unknown",
        niche=brand.niche or "unknown",
        description=(brand.description or "")[:800] or "none provided",
        website=brand.website or brand.domain or "none provided",
        instagram_handle=brand.instagram_handle or "none provided",
    )
    result = call_gpt_json(prompt, context=f"brand tier for brand_id={brand.id}")
    if not isinstance(result, dict):
        return None

    inferred = result.get("brand_tier")
    if inferred is None:
        inferred = result.get("tier")
    if inferred is None:
        inferred = result.get("brandTier")
    return _normalize_brand_tier(inferred)


def score_brand_tier(db: Session, limit: int | None = None, brand_raw_id: int | None = None) -> int:
    """Set brand_tier for pending brands in brands_raw."""
    query = db.query(BrandRaw).filter(BrandRaw.brand_tier.is_(None))
    if brand_raw_id is not None:
        query = query.filter(BrandRaw.id == brand_raw_id)
    if limit is not None:
        query = query.limit(limit)

    rows = query.all()
    if not rows:
        logger.info("Brand tier scoring: no rows pending")
        return 0

    logger.info("Brand tier scoring: processing %d row(s)", len(rows))
    updated = 0

    for row in rows:
        tier = _classify_brand_tier(db, row)
        if tier is None:
            logger.warning("Brand tier scoring: id=%s name=%s — LLM call failed or returned invalid result", row.id, row.name)
            continue

        row.brand_tier = tier
        db.commit()
        updated += 1
        logger.info("Brand tier scoring: id=%s name=%s -> %s", row.id, row.name, tier)

    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="Classify brands_raw rows into a brand market tier.")
    parser.add_argument("--limit", type=int, default=None, help="Max number of brands to score in this run.")
    parser.add_argument("--brand-id", type=int, default=None, dest="brand_raw_id", help="Limit to one brands_raw.id.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        updated = score_brand_tier(db, limit=args.limit, brand_raw_id=args.brand_raw_id)
        print(f"brand_tier: updated={updated}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
