# Run from the project root in PowerShell (module mode keeps the project root on sys.path):
#   .\venv\Scripts\python.exe -m gender_check.run_brand_gender_check
# Optional: add --dry-run to list qualifying brands without calling the LLM,
# or --limit 10 to process at most 10 brands.
# Classification results are printed as JSON lines in the terminal; this script
# reads from the database but does not save the results to a database table.
# python -m gender_check.run_brand_gender_check --limit 1

import argparse
import json
import logging
import math
from collections import defaultdict

from sqlalchemy import text

from pipeline.db import BrandRaw, InstagramPost, SessionLocal
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template

PROMPT = """You are analyzing an Instagram brand to independently estimate how strongly its products or services are marketed toward male and female audiences.

Estimate both independent percentages:
- male_pct: how strongly the brand targets a male audience, from 0 to 100
- female_pct: how strongly the brand targets a female audience, from 0 to 100

These are independent targeting estimates, not shares of one audience. Do not make them add to 100, do not derive one from the other, and do not lower one just because the other is high. Both may be high when the brand clearly targets both audiences. Use 0 when there is evidence that a gender is not a target; use lower confidence when evidence is limited or ambiguous.

Use all available evidence:
- Instagram bio
- Business category
- Recent post captions
- Hashtags
- Repeated product/service references
- People/models shown or discussed in the content when that information is available

Important:
- Do not assume gender based only on the brand name.
- Do not assume that a beauty, fashion, fitness, health, or lifestyle brand is automatically Female or Male.
- Look for explicit evidence of who the products/services are intended for.
- Do not force a Male or Female classification when the evidence supports a mixed audience.
- A brand can be Female even if some posts feature men, and vice versa. Consider the overall pattern.
- Do not use stereotypes.
- Give male_confidence and female_confidence separately, each from 0 to 100, based on the strength and consistency of evidence for that estimate.
- Give one short, evidence-based explanation that covers the evidence for both percentages.

Return ONLY valid JSON:

{
    "male_pct": 85,
    "male_confidence": 80,
    "female_pct": 90,
    "female_confidence": 80,
    "explanation": "The brand explicitly markets products to both men and women."
}

Confidence scale for each estimate:
- 90-100 = very strong and explicit evidence
- 75-89 = strong evidence
- 60-74 = moderate evidence
- 0-59 = weak or ambiguous evidence

Keep the explanation under 30 words.

Brand name:
{brand_name}

Instagram bio:
{bio}

Business category:
{business_category_name}

Instagram posts:
{posts}

Each post may contain:
- caption
- hashtags

Analyze the complete available evidence and return only the JSON object.
"""

MIN_INSTAGRAM_POSTS = 20

BRAND_QUERY = text("""
    WITH best_per_brand AS (
      SELECT DISTINCT ON (tcbp.brand_raw_id)
        ccr.username AS creator_username,
        ccr.niche,
        tcbp.brand_raw_id,
        tcbp.brand_name,
        tcbp.post_url,
        tcbp.post_timestamp,
        tcbp.sponsorship_confidence,
        br.has_official_website,
        br.description
      FROM content_creator_re ccr
      JOIN test_creator_brand_partnership_posts tcbp
        ON tcbp.content_creator_re_id = ccr.id
      JOIN brands_raw br
        ON br.id = tcbp.brand_raw_id
      WHERE ccr.id BETWEEN 1 AND 208
        AND ccr.niche = 'Beauty'
        AND tcbp.sponsorship_confidence >= 90
        AND br.refferls = false
        AND br.male_pct IS NULL
        AND br.male_confidence IS NULL
        AND br.female_pct IS NULL
        AND br.female_confidence IS NULL
        AND br.gender_explanation IS NULL
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
      brand_raw_id,
      brand_name,
      creator_username,
      post_url,
      post_timestamp,
      sponsorship_confidence,
      has_official_website,
      description,
      CASE
        WHEN has_official_website = false THEN 'no_website'
        WHEN has_official_website = true AND description IS NULL
          THEN 'website_but_description_null'
      END AS reason
    FROM best_per_brand
    WHERE has_official_website = false
       OR (has_official_website = true AND description IS NULL)
    ORDER BY reason, brand_name
""")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _post_evidence(post: InstagramPost) -> dict:
    return {
        "caption": post.caption,
        "hashtags": post.hashtags,
    }


def _bounded_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 100:
        return None
    return number


def _explanation(value: object, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _gender_metrics_result(result: dict) -> dict:
    male_pct = _bounded_number(result.get("male_pct"))
    female_pct = _bounded_number(result.get("female_pct"))
    male_confidence = _bounded_number(result.get("male_confidence"))
    female_confidence = _bounded_number(result.get("female_confidence"))

    return {
        "male_pct": round(male_pct) if male_pct is not None else None,
        "male_confidence": round(male_confidence) if male_pct is not None and male_confidence is not None else 0,
        "female_pct": round(female_pct) if female_pct is not None else None,
        "female_confidence": round(female_confidence) if female_pct is not None and female_confidence is not None else 0,
        "explanation": _explanation(
            result.get("explanation"),
            "No valid gender estimates were returned."
            if male_pct is None and female_pct is None
            else "No explanation provided.",
        ),
    }


def _ensure_gender_columns(db) -> None:
    for column, sql_type in (
        ("male_pct", "FLOAT"),
        ("male_confidence", "INTEGER"),
        ("female_pct", "FLOAT"),
        ("female_confidence", "INTEGER"),
        ("gender_explanation", "TEXT"),
    ):
        db.execute(text(f"ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS {column} {sql_type}"))
    db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Classify target-audience gender for selected brands."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List qualifying brands without calling the LLM.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of qualifying brands to send to the LLM.",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    db = SessionLocal()
    try:
        _ensure_gender_columns(db)
        brands = db.execute(BRAND_QUERY).mappings().all()
        if not brands:
            logger.info("The brand query returned no rows.")
            return 0

        brand_ids = [brand["brand_raw_id"] for brand in brands]
        posts = (
            db.query(InstagramPost)
            .filter(InstagramPost.brand_raw_id.in_(brand_ids))
            .order_by(
                InstagramPost.brand_raw_id, InstagramPost.timestamp.desc().nullslast()
            )
            .all()
        )
        posts_by_brand = defaultdict(list)
        for post in posts:
            posts_by_brand[post.brand_raw_id].append(post)

        eligible = [
            (brand, posts_by_brand[brand["brand_raw_id"]])
            for brand in brands
            if len(posts_by_brand[brand["brand_raw_id"]]) > MIN_INSTAGRAM_POSTS
        ]
        if args.limit is not None:
            eligible = eligible[: args.limit]

        logger.info(
            "%d candidate brand(s); %d have more than %d Instagram post rows.",
            len(brands),
            len(eligible),
            MIN_INSTAGRAM_POSTS,
        )

        for brand, brand_posts in eligible:
            if args.dry_run:
                logger.info(
                    "DRY RUN brand_raw_id=%s brand=%s instagram_posts=%d reason=%s",
                    brand["brand_raw_id"],
                    brand["brand_name"],
                    len(brand_posts),
                    brand["reason"],
                )
                continue

            bio = next(
                (post.biography for post in brand_posts if post.biography),
                "Not available",
            )
            business_category_name = next(
                (
                    post.business_category_name
                    for post in brand_posts
                    if post.business_category_name
                ),
                "Not available",
            )
            post_evidence = [_post_evidence(post) for post in brand_posts]
            prompt = fill_template(
                PROMPT,
                brand_name=brand["brand_name"] or "Unknown",
                bio=bio,
                business_category_name=business_category_name,
                posts=json.dumps(post_evidence, ensure_ascii=True, default=str),
            )
            result = call_gpt_json(
                prompt,
                context=f"brand gender check brand_raw_id={brand['brand_raw_id']}",
            )
            gender_metrics = _gender_metrics_result(result)
            updated = (
                db.query(BrandRaw)
                .filter(BrandRaw.id == brand["brand_raw_id"])
                .update(
                    {
                        "male_pct": gender_metrics["male_pct"],
                        "male_confidence": gender_metrics["male_confidence"],
                        "female_pct": gender_metrics["female_pct"],
                        "female_confidence": gender_metrics["female_confidence"],
                        "gender_explanation": gender_metrics["explanation"],
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if not updated:
                logger.error("Could not save gender metrics for brand_raw_id=%s", brand["brand_raw_id"])
            print(
                json.dumps(
                    {
                        "brand_raw_id": brand["brand_raw_id"],
                        "brand_name": brand["brand_name"],
                        "post_count": len(brand_posts),
                        **gender_metrics,
                    },
                    ensure_ascii=True,
                    default=str,
                )
            )

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
