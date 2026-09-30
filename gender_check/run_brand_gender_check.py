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

from pipeline.db import BrandRaw, InstagramPost, Prompt, SessionLocal
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import (
    BRAND_AUDIENCE_ANALYSIS_DEFAULT_PROMPT,
    BRAND_AUDIENCE_ANALYSIS_PROMPT_NAME,
)

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
        AND ccr.niche IN ('Beauty', 'Music', 'Fitness', 'Health')
        AND tcbp.sponsorship_confidence >= 90
        AND br.refferls = false
        AND br.target_audience_gender IS NULL
        AND br.target_audience_gender_confidence IS NULL
        AND br.target_audience_min_age IS NULL
        AND br.target_audience_max_age IS NULL
        AND br.target_audience_age_confidence IS NULL
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


def _get_brand_audience_prompt(db) -> str:
    prompt_row = db.query(Prompt).filter(
        Prompt.name == BRAND_AUDIENCE_ANALYSIS_PROMPT_NAME
    ).first()
    return (
        prompt_row.content
        if prompt_row and prompt_row.content
        else BRAND_AUDIENCE_ANALYSIS_DEFAULT_PROMPT
    )


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
    target_min_age = _age(result.get("target_audience_min_age"))
    target_max_age = _age(result.get("target_audience_max_age"))
    min_age = _age(result.get("product_audience_min_age"))
    max_age = _age(result.get("product_audience_max_age"))
    if target_min_age is not None and target_max_age is not None and target_min_age > target_max_age:
        target_min_age = None
        target_max_age = None
    if min_age is not None and max_age is not None and min_age > max_age:
        min_age = None
        max_age = None

    target_confidence = _bounded_number(result.get("target_audience_gender_confidence"))
    product_confidence = _bounded_number(result.get("product_audience_gender_confidence"))
    target_age_confidence = _bounded_number(result.get("target_audience_age_confidence"))
    age_confidence = _bounded_number(result.get("product_audience_age_confidence"))
    has_estimate = any(value is not None for value in (
        target_gender, product_gender, target_min_age, target_max_age, min_age, max_age,
    ))

    return {
        "target_audience_gender": target_gender,
        "target_audience_gender_confidence": round(target_confidence) if target_gender and target_confidence is not None else 0,
        "target_audience_min_age": target_min_age,
        "target_audience_max_age": target_max_age,
        "target_audience_age_confidence": round(target_age_confidence) if target_min_age is not None and target_max_age is not None and target_age_confidence is not None else 0,
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
            ADD COLUMN IF NOT EXISTS target_audience_min_age INTEGER,
            ADD COLUMN IF NOT EXISTS target_audience_max_age INTEGER,
            ADD COLUMN IF NOT EXISTS target_audience_age_confidence INTEGER,
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
        prompt_template = _get_brand_audience_prompt(db)
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
                prompt_template,
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
