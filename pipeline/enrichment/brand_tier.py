"""
pipeline/enrichment/brand_tier.py

Classify each brand in brands_raw into a market tier based on likely scale,
brand power, and premium positioning.

Values:
- lower-range
- midlower-range
- midhigher-range
- higher-range
- extreme

extreme = Fortune 500 / household-name global brands (Amazon, Nike, Apple,
Coca-Cola, ...) that mostly partner with celebrities and very large creators.
higher-range = large, well-known companies that are hard but not impossible
for an independent creator to reach.

When a brand has no name or no description, the tier is guessed from its
Instagram username (e.g. "shoptemu" -> Temu). A brand the LLM does not
recognize at all is treated as small and gets lower-range — the prompt never
asks for null; an empty tier only means the OpenAI call failed or returned
nothing usable (retried on the next run).

Safe to re-run — rows with brand_tier IS NULL are processed, and rows with an
existing value are skipped. Brands with refferls = true are never processed. When the LLM gives no confirmed tier (unsure, or
the OpenAI call failed) brand_tier is left NULL — never the text 'null' — so
the row is retried on the next run.
"""

import argparse
import logging
import re

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
- extreme

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
- Large corporations, multinational companies, major entertainment companies, major record labels, major sports organizations, major retailers, public companies, major consumer brands, major technology companies, major media companies, government organizations, universities, major nonprofits, or widely recognized brands that are NOT household names at Fortune 500 scale.
- Usually have dedicated marketing, PR, sponsorship, talent, or agency teams.
- Partnerships are typically managed through formal processes, agencies, or existing networks.
- Most independent creators cannot easily secure partnerships through cold outreach.

extreme:
- Fortune 500 companies and their flagship consumer brands, and other extremely well-known household-name global brands (recognized by almost everyone, in almost every country).
- Their influencer marketing mostly goes to celebrities, athletes, musicians with huge followings, and mega creators (millions of followers), usually through agencies and large campaigns.
- An independent creator with 10K–1M followers realistically has almost no chance of a direct sponsorship.

Important rules:

- Be highly skeptical of assigning anything below higher-range to globally recognized brands.
- Brands such as Amazon, Apple, Google, Microsoft, Sony, Disney, Netflix, Adidas, Nike, Coca-Cola, Pepsi, McDonald's, Samsung, BMW, Mercedes-Benz, Visa, Mastercard, Walmart, Target, Spotify, TikTok, Meta, and YouTube are extreme.
- Use extreme ONLY for Fortune 500 / household-name brands that mostly partner with celebrities and very large creators. Large but less famous companies, major subsidiaries, and well-known niche leaders are higher-range, not extreme.
- Large parent companies, major subsidiaries, and major corporate brands should generally be higher-range (or extreme if they are Fortune 500 household names).
- Government agencies, universities, major hospitals, museums, sports leagues, and major public institutions should generally be higher-range.
- If information is limited, estimate based on brand name, website, description, Instagram username, and likely commercial scale.
- If you do not recognize the brand at all — you know nothing about it from its name, description, website or Instagram username — that is itself evidence that it is small. Household names and large companies are recognized by almost everyone, so a brand you have never heard of is very unlikely to be higher-range or extreme. Classify an unrecognized brand as lower-range (use midlower-range only if the description or website clearly shows an established regional or growing business).
- Always return one of the five tiers. Do not return null.

Brand name: {brand_name}
Brand niche: {niche}
Brand description: {description}
Website/domain: {website}
Instagram handle: {instagram_handle}
Instagram username: {username}
{identification_note}

Respond with ONLY valid JSON, with exactly one of the five tier values:

{"brand_tier":"<tier>"}
"""

# Added to the prompt when the brand has no name or no description: the
# Instagram username is then the main evidence, and the "unsure -> null" rule
# is relaxed so a recognizable username still gets a tier.
IDENTIFY_FROM_USERNAME_NOTE = """
NOTE: this brand has no {missing}. Work out which brand or business the Instagram username "@{username}" belongs to (for example "shoptemu" is Temu, "espn" is ESPN, "homedepot" is The Home Depot) and classify THAT brand using the definitions above. If you cannot tell which business the username refers to, or you have never heard of it, it is almost certainly a small brand: classify it as lower-range.
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
    "extreme": "extreme",
    "extreme-range": "extreme",
}


def _normalize_brand_tier(value: object) -> str | None:
    """A valid tier, or None when there is no confirmed tier (stored as NULL)."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower().replace("_", "-")
    if cleaned in {"", "null", "none", "n/a", "na", "unknown", "unsure"}:
        return None
    return _NORMALIZED_VALUES.get(cleaned)


def _username(handle: str | None) -> str:
    """Instagram username from a stored handle / profile URL ("https://www.instagram.com/espn/" -> "espn")."""
    value = (handle or "").strip()
    value = re.sub(r"^(?:https?://)?(?:www\.)?instagram\.com/", "", value, flags=re.I)
    return re.split(r"[/?#]", value, maxsplit=1)[0].lstrip("@").strip()


def _classify_brand_tier(db: Session, brand: BrandRaw) -> str | None:
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping brand tier classification for brand_id=%s", brand.id)
        return None

    username = _username(brand.instagram_handle)
    name = (brand.name or "").strip()
    description = (brand.description or "").strip()
    missing = " or ".join(label for label, value in (("name", name), ("description", description)) if not value)
    note = ""
    if missing and username:
        note = IDENTIFY_FROM_USERNAME_NOTE.replace("{missing}", missing).replace("{username}", username)
        logger.info("Brand tier scoring: id=%s has no %s — guessing from @%s", brand.id, missing, username)

    prompt = fill_template(
        BRAND_TIER_PROMPT,
        brand_name=name or "none provided",
        niche=brand.niche or "unknown",
        description=description[:800] or "none provided",
        website=brand.website or brand.domain or "none provided",
        instagram_handle=brand.instagram_handle or "none provided",
        username=username or "none provided",
        identification_note=note,
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
    """Set brand_tier for pending brands in brands_raw (brand_tier IS NULL, refferls not true)."""
    # refferls=True brands are skipped; NULL / False are processed.
    query = db.query(BrandRaw).filter(
        BrandRaw.brand_tier.is_(None),
        BrandRaw.refferls.isnot(True),
    )
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
            logger.info(
                "Brand tier scoring: id=%s name=%s — no confirmed tier; leaving brand_tier empty (retried next run)",
                row.id,
                row.name,
            )
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
