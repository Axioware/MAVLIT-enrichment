# Run from the project root in PowerShell (module mode keeps the project root on sys.path):
#   .\venv\Scripts\python.exe -m gender_check.run_brand_gender_check
# Optional: add --dry-run to list qualifying brands without calling the LLM,
# or --limit 10 to process at most 10 brands.
# Classification results are saved to brands_raw and printed as JSON lines.
# python -m gender_check.run_brand_gender_check --limit 1

import argparse
import json
import logging
import math
from collections import defaultdict

from sqlalchemy import text

from pipeline.db import BrandRaw, InstagramPost, SessionLocal
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template

PROMPT = """Analyze this brand's Instagram presence and offerings. You must evaluate THREE DISTINCT audience concepts:

1. TARGET AUDIENCE GENDER
Who is the brand's marketing primarily aimed at?
This means the person the brand is trying to attract, influence, or persuade to buy, engage, or take action.

2. PRODUCT AUDIENCE GENDER
Who is the actual product or service designed for, intended for, or useful to?
This means the end user or recipient of the product/service, NOT necessarily the person who purchases it.

3. PRODUCT AUDIENCE AGE RANGE
What age range is the product or service designed for?
Return the youngest reasonable age and oldest reasonable age as separate integer fields.

IMPORTANT: TARGET AUDIENCE AND PRODUCT AUDIENCE ARE DIFFERENT.

Examples:
- A flower/gift brand may market heavily to men because men purchase flowers as gifts, while the flowers can be given to people of any gender. Target audience = male; product audience = both.
- A home-decor brand may primarily feature women and speak to women in its marketing, while home-decor products are useful for people of any gender. Target audience = female; product audience = both.
- A men's skincare brand marketed directly to men: target audience = male; product audience = male.
- A children's toy brand may market to parents, while the actual product audience is children. The target gender should be based on the marketing evidence, while product gender should be based on the actual intended users.

Do NOT assume that the person shown in an advertisement is necessarily the product's end user.
Do NOT assume the buyer and end user are the same person.
Do NOT conflate marketing representation with product purpose.

For each gender field, return exactly ONE of:
- "male"
- "female"
- "both"

"both" means there is evidence that the brand/product genuinely serves both male and female audiences. Do NOT use "both" simply because the evidence is uncertain.

For age:
- Return integer ages from 0 to 120.
- Estimate the youngest and oldest reasonable ages the product/service is designed for.
- Use evidence from the actual products/services, not the age of people appearing in posts.
- If the product/service is genuinely suitable for almost all ages, use a broad range.
- If there is not enough evidence to estimate an endpoint, return null for that endpoint.
- If the age range cannot reasonably be determined, return null for both.
- Do not invent a precise age range without evidence.

Use ALL available evidence:
- Instagram bio
- Business category
- Recent post captions
- Hashtags
- Repeated product/service references
- Product names
- Service descriptions
- Promotions and offers
- People/models shown or discussed
- Explicit statements about who products/services are for
- Any other information contained in the supplied Instagram evidence

Evidence rules:
- Do not determine gender from the brand name.
- Do not assume beauty, fashion, fitness, health, lifestyle, home, or similar categories are automatically male or female.
- Product audience must be based primarily on the actual product/service.
- Target audience must be based primarily on marketing language, positioning, promotions, creative choices, and who the brand appears to be trying to reach.
- A brand can have a female target audience while its products are for both genders.
- A brand can have a male target audience while its products are for both genders.
- A brand can market to one gender while the product is intended for another gender.
- Do not use stereotypes.
- Do not infer gender solely from models or people appearing in photographs.
- Consider the overall repeated pattern of evidence rather than one post.

CONFIDENCE:

Return a separate confidence score from 0 to 100 for:
- target audience gender
- product audience gender
- product audience age range

Confidence measures how certain the evidence supports the classification or estimate.

Confidence scale:
- 90-100 = very strong and explicit evidence
- 75-89 = strong evidence
- 60-74 = moderate evidence
- 0-59 = weak or ambiguous evidence

A high confidence score does NOT mean the brand strongly targets that gender. It means the evidence strongly supports the classification.

If the evidence is insufficient:
- Use null for the affected gender field.
- Use null for unsupported age endpoints.
- Lower the corresponding confidence score.

Return ONLY valid JSON:

{
    "target_audience_gender": "male|female|both|null",
    "target_audience_gender_confidence": 0,
    "product_audience_gender": "male|female|both|null",
    "product_audience_gender_confidence": 0,
    "product_audience_min_age": 0,
    "product_audience_max_age": 120,
    "product_audience_age_confidence": 0,
    "audience_analysis_explanation": "Short explanation distinguishing the marketing target from the actual product audience and explaining the age estimate."
}

Keep "audience_analysis_explanation" under 50 words.

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

Analyze the complete available evidence and return ONLY the JSON object.
"""

MIN_INSTAGRAM_POSTS = 10

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
        AND br.target_audience_gender IS NULL
        AND br.target_audience_gender_confidence IS NULL
        AND br.product_audience_gender IS NULL
        AND br.product_audience_gender_confidence IS NULL
        AND br.product_audience_min_age IS NULL
        AND br.product_audience_max_age IS NULL
        AND br.product_audience_age_confidence IS NULL
        AND br.audience_analysis_explanation IS NULL
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


def _gender_category(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in {"male", "female", "both"} else None


def _age(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 120 or not number.is_integer():
        return None
    return int(number)


def _gender_metrics_result(result: dict) -> dict:
    target_gender = _gender_category(result.get("target_audience_gender"))
    product_gender = _gender_category(result.get("product_audience_gender"))
    min_age = _age(result.get("product_audience_min_age"))
    max_age = _age(result.get("product_audience_max_age"))
    if min_age is not None and max_age is not None and min_age > max_age:
        min_age = None
        max_age = None

    target_confidence = _bounded_number(result.get("target_audience_gender_confidence"))
    product_confidence = _bounded_number(result.get("product_audience_gender_confidence"))
    age_confidence = _bounded_number(result.get("product_audience_age_confidence"))
    has_estimate = any(value is not None for value in (target_gender, product_gender, min_age, max_age))

    return {
        "target_audience_gender": target_gender,
        "target_audience_gender_confidence": round(target_confidence) if target_gender and target_confidence is not None else 0,
        "product_audience_gender": product_gender,
        "product_audience_gender_confidence": round(product_confidence) if product_gender and product_confidence is not None else 0,
        "product_audience_min_age": min_age,
        "product_audience_max_age": max_age,
        "product_audience_age_confidence": round(age_confidence) if min_age is not None and max_age is not None and age_confidence is not None else 0,
        "audience_analysis_explanation": _explanation(
            result.get("audience_analysis_explanation"),
            "No valid audience estimates were returned." if not has_estimate else "No explanation provided.",
        ),
    }


def _ensure_gender_columns(db) -> None:
    db.execute(text("""
        ALTER TABLE brands_raw
            DROP COLUMN IF EXISTS male_pct,
            DROP COLUMN IF EXISTS male_confidence,
            DROP COLUMN IF EXISTS female_pct,
            DROP COLUMN IF EXISTS female_confidence,
            DROP COLUMN IF EXISTS gender_explanation,
            ADD COLUMN IF NOT EXISTS target_audience_gender TEXT,
            ADD COLUMN IF NOT EXISTS target_audience_gender_confidence INTEGER,
            ADD COLUMN IF NOT EXISTS product_audience_gender TEXT,
            ADD COLUMN IF NOT EXISTS product_audience_gender_confidence INTEGER,
            ADD COLUMN IF NOT EXISTS product_audience_min_age INTEGER,
            ADD COLUMN IF NOT EXISTS product_audience_max_age INTEGER,
            ADD COLUMN IF NOT EXISTS product_audience_age_confidence INTEGER,
            ADD COLUMN IF NOT EXISTS audience_analysis_explanation TEXT
    """))
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
                        **gender_metrics,
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
