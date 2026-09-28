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
from collections import defaultdict

from sqlalchemy import text

from pipeline.db import InstagramPost, SessionLocal
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template

PROMPT = """You are analyzing an Instagram brand to determine the primary gender audience the brand's products or services are marketed toward.

Classify the brand into exactly ONE of:
- Male
- Female
- Both

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
- "Both" should be used when the brand clearly serves both genders or when the available evidence does not strongly support Male or Female.
- A brand can be Female even if some posts feature men, and vice versa. Consider the overall pattern.
- Do not use stereotypes.
- Confidence must reflect the strength and consistency of the evidence.

Return ONLY valid JSON:

{
    "gender": "Male|Female|Both",
    "confidence": 0,
    "explanation": "Short explanation based on the strongest evidence."
}

Confidence:
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

MIN_INSTAGRAM_POSTS = 5

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
            print(
                json.dumps(
                    {
                        "brand_raw_id": brand["brand_raw_id"],
                        "brand_name": brand["brand_name"],
                        "post_count": len(brand_posts),
                        "gender": result.get("gender"),
                        "confidence": result.get("confidence"),
                        "explanation": result.get("explanation"),
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
