# Run from the project root in PowerShell (module mode keeps the project root on sys.path):
#   .\venv\Scripts\python.exe -m gender_check.run_brand_gender_check
# Optional: add --dry-run to list qualifying brands without calling the LLM,
# --limit 10 to process at most 10 brands, or --beach-brands to process only
# brands with beach_brands = true.
# Classification results are saved to brands_raw and printed as JSON lines.
# python -m gender_check.run_brand_gender_check --brand-id 2151

import argparse
import json
import logging
import math
from collections import defaultdict

from sqlalchemy import text

from pipeline.db import (
    BrandRaw,
    InstagramPost,
    Prompt,
    BatchSessionLocal,
    TestCreatorBrandPartnershipPost,
)
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.owner_guard import require_owner_role
from pipeline.helpers.prompts import (
    BRAND_AUDIENCE_ANALYSIS_DEFAULT_PROMPT,
    BRAND_AUDIENCE_ANALYSIS_PROMPT_NAME,
)

MIN_INSTAGRAM_POSTS = 10
LLM_MODEL = "gpt-6-luna"

# Candidate brands: refferls = false, geo_reach_score 0-40, no audience
# results yet, and partnership evidence from EITHER source:
#   - a test_creator_brand_partnership_posts row with sponsorship_confidence >= 90
#   - an instagram_posts row with sponsorship_confidence >= 90
# This script handles the brands WITHOUT a usable website (no website, or a
# website with no description); run_brand_website_audience_check.py handles
# the rest. --beach-brands narrows the set to brands_raw.beach_brands = true.
_BRAND_QUERY_SQL = """
    SELECT
      br.id AS brand_raw_id,
      COALESCE(br.name, (
        SELECT tcbp.brand_name
        FROM test_creator_brand_partnership_posts tcbp
        WHERE tcbp.brand_raw_id = br.id AND tcbp.brand_name IS NOT NULL
        LIMIT 1
      )) AS brand_name,
      br.has_official_website,
      br.description,
      CASE
        WHEN br.has_official_website = false THEN 'no_website'
        WHEN br.has_official_website = true AND br.description IS NULL
          THEN 'website_but_description_null'
      END AS reason
    FROM brands_raw br
    WHERE br.refferls = false
      AND br.geo_reach_score BETWEEN 0 AND 40
      AND (
        EXISTS (
          SELECT 1 FROM test_creator_brand_partnership_posts tcbp
          WHERE tcbp.brand_raw_id = br.id AND tcbp.sponsorship_confidence >= 90
        )
        OR EXISTS (
          SELECT 1 FROM instagram_posts ip
          WHERE ip.brand_raw_id = br.id AND ip.sponsorship_confidence >= 90
        )
      )
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
        br.has_official_website = false
        OR (br.has_official_website = true AND br.description IS NULL)
      )
      AND (:beach_only = false OR br.beach_brands = true)
    ORDER BY reason, brand_name
"""

BRAND_QUERY = text(_BRAND_QUERY_SQL)   # parameters: beach_only (True narrows the set to brands_raw.beach_brands = true)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _post_evidence(post: InstagramPost) -> dict:
    return {
        "caption": post.caption,
        "hashtags": post.hashtags,
        "coauthor_producers": post.coauthor_producers or [],
        "mentions": post.mentions or [],
        "sponsors": post.sponsors or [],
        "tagged_users": post.tagged_users or [],
    }


def _reverse_engineering_creators(db, brand_raw_id: int) -> list[dict]:
    """
    Distinct creators linked to the brand through reverse-engineering
    partnership posts (test_creator_brand_partnership_posts) with
    sponsorship_confidence >= 90.
    """
    rows = (
        db.query(
            TestCreatorBrandPartnershipPost.creator_username,
            TestCreatorBrandPartnershipPost.creator_name,
        )
        .filter(
            TestCreatorBrandPartnershipPost.brand_raw_id == brand_raw_id,
            TestCreatorBrandPartnershipPost.sponsorship_confidence >= 90,
        )
        .all()
    )
    creators: dict[str, dict] = {}
    for username, name in rows:
        if not username or not username.strip():
            continue
        key = username.strip().lower()
        if key not in creators or (name and not creators[key]["creator_name"]):
            creators[key] = {"creator_username": username.strip(), "creator_name": name}
    return list(creators.values())


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


UNSURE_TARGET_GENDER_CONFIDENCE = 50


def _default_target_gender(metrics: dict) -> dict:
    """target_audience_gender is never saved NULL: when the LLM still gives
    none, fall back to "both" at low confidence (the prompt's "not sure" rule)."""
    if metrics.get("target_audience_gender") is None:
        metrics["target_audience_gender"] = "both"
        metrics["target_audience_gender_confidence"] = UNSURE_TARGET_GENDER_CONFIDENCE
    return metrics


def _ensure_gender_columns(db) -> None:
    require_owner_role(db, "run_brand_gender_check (brands_raw columns)")
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
    parser.add_argument(
        "--brand-id",
        type=int,
        action="append",
        dest="brand_ids",
        help="Process one brand_raw ID. Repeat this option to process multiple IDs.",
    )
    parser.add_argument(
        "--beach-brands",
        action="store_true",
        help="Only process brands with beach_brands = true.",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    db = BatchSessionLocal()
    try:
        _ensure_gender_columns(db)
        prompt_template = _get_brand_audience_prompt(db)
        brands = db.execute(BRAND_QUERY, {"beach_only": bool(args.beach_brands)}).mappings().all()
        if args.brand_ids:
            requested_ids = set(args.brand_ids)
            brands = [brand for brand in brands if brand["brand_raw_id"] in requested_ids]
            missing_ids = requested_ids - {brand["brand_raw_id"] for brand in brands}
            if missing_ids:
                logger.warning(
                    "Requested brand_raw_id(s) did not match the script's candidate conditions: %s",
                    sorted(missing_ids),
                )
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
        # End the read transaction now: brands_raw/instagram_posts locks held
        # across the slow scraping/LLM work below block every ALTER TABLE
        # (API startup migrations, other scripts) and everything queued behind
        # them. Loaded objects stay usable (expire_on_commit=False).
        db.commit()

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
            re_creators = _reverse_engineering_creators(db, brand["brand_raw_id"])
            db.commit()  # don't hold locks during the slow scraping/LLM calls below
            prompt = fill_template(
                prompt_template,
                brand_name=brand["brand_name"] or "Unknown",
                bio=bio,
                business_category_name=business_category_name,
                posts=json.dumps(post_evidence, ensure_ascii=True, default=str),
                re_creators=json.dumps(re_creators, ensure_ascii=True) if re_creators else "None",
            )
            result = call_gpt_json(
                prompt,
                context=f"brand gender check brand_raw_id={brand['brand_raw_id']}",
                model=LLM_MODEL,
            )
            gender_metrics = _default_target_gender(_gender_metrics_result(result))
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
