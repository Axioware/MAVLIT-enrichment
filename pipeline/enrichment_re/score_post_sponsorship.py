"""
pipeline/enrichment_re/score_post_sponsorship.py

One-time test script: for every row already in
test_creator_brand_partnership_posts, ask an LLM (hardcoded prompt, no
Prompt-table entry) to estimate a 0-100 confidence that the post is a paid
sponsorship with that specific brand, using brand_name, creator_name,
caption, paid_partnership, mentions and tagged_users. Writes the score into
sponsorship_confidence.

Safe to re-run — only rows with sponsorship_confidence IS NULL are picked
up, so a partially-completed run just resumes.
"""

import argparse
import logging
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy.orm import Session

from config import OPENAI_KEY
from pipeline.db import SessionLocal, TestCreatorBrandPartnershipPost
from pipeline.enrichment_re.content_creator_re import _ensure_partnership_evidence_table
from pipeline.helpers.gpt_llm import call_gpt_json

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """You are an Instagram sponsorship verification system.

Your task is NOT to identify whether the brand and creator have ever worked together.

Your task is ONLY to determine whether THIS SPECIFIC INSTAGRAM POST is evidence of a paid sponsorship, paid partnership, brand collaboration, ambassador promotion, affiliate promotion, gifted campaign with disclosure, or other commercial relationship between THIS creator and THIS brand.

ASSUME THE POST IS NOT SPONSORED UNLESS THERE IS POSITIVE EVIDENCE.

Do NOT increase confidence simply because:
- the brand is mentioned
- the brand is tagged
- the creator follows the brand
- the creator likes the brand
- the product appears in the caption
- the creator uses the product
- the creator reviews the product
- the creator attends an event
- the creator reposts brand content
- the creator is in the same industry
- the creator and brand have partnered in the past

A tag, mention, or product reference alone is weak evidence.

Strong evidence includes:
- Instagram paid partnership label
- explicit sponsorship disclosure (#ad, #sponsored, paid partnership, partner, advertisement)
- affiliate/referral/discount code
- language indicating a commercial relationship
- direct promotional call-to-action for the brand
- giveaway or campaign run jointly with the brand
- clear indication the creator is representing the brand
- multiple pieces of evidence pointing to the same brand

IMPORTANT:

The score must represent the likelihood that THIS POST is sponsored by THIS SPECIFIC BRAND.

If the evidence suggests sponsorship with a different brand, the score should be very low.

The creator has fewer than 1 million followers.

Be skeptical when evaluating extremely large global brands (for example Marvel, Disney, Netflix, Coca-Cola, McDonald's, Apple, Nike, Adidas, Samsung, Amazon, etc.).

A mention, tag, hashtag, product reference, fan content, review, reaction, event attendance, movie discussion, theme park visit, or general enthusiasm toward a major brand is NOT strong evidence of sponsorship.

For very large global brands, require stronger evidence than usual before assigning a high confidence score. Prefer low confidence unless the post contains clear commercial signals linking the creator and the brand.

Examples:

Brand = Nike
Caption promotes Adidas
Paid partnership = true

Result:
Low confidence because the evidence points to another brand.

Brand = Nike
Caption contains #ad and promotes Nike products
Nike tagged
Discount code provided

Result:
High confidence.

Scoring rubric:

0-10:
No evidence of sponsorship.
Ordinary mention, tag, review, lifestyle content, or unrelated brand.

11-25:
Brand appears but no evidence of a commercial relationship.

26-40:
Some weak signals but sponsorship is speculative.

41-60:
Possible sponsorship but evidence is incomplete.

61-80:
Strong evidence of a commercial relationship.

81-100:
Very strong evidence.
Explicit sponsorship disclosures, paid partnership indicators, affiliate codes, campaign language, or multiple strong signals.

Evaluate only the information provided below.

Brand: {brand_name}

Creator: {creator_name}

Post caption:
{caption}

Instagram paid partnership flag:
{paid_partnership}

Mentions:
{mentions}

Tagged users:
{tagged_users}

Respond with ONLY valid JSON:

{{
  "confidence_pct": <integer 0-100>,
  "reason": "<short explanation>"
}}
"""


def _fmt(value) -> str:
    if value is None or value == [] or value == {}:
        return "none"
    return str(value)


def _score_post(brand_name: str, creator_name: str, caption: str | None,
                 paid_partnership: bool | None, mentions, tagged_users) -> int | None:
    prompt = _PROMPT_TEMPLATE.format(
        brand_name=brand_name or "unknown",
        creator_name=creator_name or "unknown",
        caption=(caption or "")[:600] or "none",
        paid_partnership=str(bool(paid_partnership)).lower(),
        mentions=_fmt(mentions),
        tagged_users=_fmt(tagged_users),
    )
    result = call_gpt_json(prompt, context=f"sponsorship score {creator_name}/{brand_name}")
    score = result.get("confidence_pct") if isinstance(result, dict) else None
    if not isinstance(score, (int, float)):
        return None
    return max(0, min(100, round(score)))


def score_post_sponsorship(db: Session, limit: int = 500, brand_raw_id: int | None = None) -> int:
    """
    Fill sponsorship_confidence on up to `limit` rows in
    test_creator_brand_partnership_posts where it's still NULL.

    Pass brand_raw_id to scope this call to one brand's rows instead — still
    filtered to sponsorship_confidence IS NULL, so repeated calls (e.g.
    limit=500 in a loop) advance through that brand's pending rows one
    batch at a time, matching enrich_instagram_users'/
    score_instagram_post_sponsorship's brand_raw_id= pattern (this operates
    on test_creator_brand_partnership_posts ROWS, not on brands directly).

    Returns the number of rows updated (failures are logged, not returned —
    matching every other enrichment step's fn(db, limit=...) -> int
    convention, since a drain loop stops on a falsy return).
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping sponsorship scoring")
        return 0

    _ensure_partnership_evidence_table()

    query = db.query(TestCreatorBrandPartnershipPost).filter(
        TestCreatorBrandPartnershipPost.sponsorship_confidence.is_(None)
    )
    if brand_raw_id is not None:
        query = query.filter(TestCreatorBrandPartnershipPost.brand_raw_id == brand_raw_id)
    rows: list[TestCreatorBrandPartnershipPost] = query.limit(limit).all()

    if not rows:
        logger.info("Sponsorship scoring: no rows pending")
        return 0

    logger.info("Sponsorship scoring: processing %d row(s)", len(rows))
    updated = 0
    failed = 0

    for row in rows:
        score = _score_post(
            row.brand_name, row.creator_name, row.caption,
            row.paid_partnership, row.mentions, row.tagged_users,
        )
        if score is None:
            logger.warning(
                "Sponsorship scoring: id=%d @%s/%s — LLM call failed",
                row.id, row.creator_username, row.brand_name,
            )
            failed += 1
            time.sleep(0.5)
            continue

        # Captured before commit — db.commit() expires this row's attributes,
        # and re-reading them afterward (e.g. for this log line) would force
        # SQLAlchemy to silently reload the row, which also re-triggers its
        # lazy="selectin" relationships (brand_raw/content_creator_re) and
        # opens a NEW, never-committed transaction that just sits there
        # holding a lock on brands_raw — this is exactly what caused a
        # production hang/lock pileup (score_post_sponsorship stuck right
        # after scoring a row, blocking /admin/brand-raw/list with a 502).
        row_id, row_creator_username, row_brand_name = row.id, row.creator_username, row.brand_name
        row.sponsorship_confidence = score
        db.commit()
        updated += 1
        logger.info(
            "Sponsorship scoring: id=%d @%s/%s → %d%%",
            row_id, row_creator_username, row_brand_name, score,
        )
        time.sleep(0.5)

    logger.info("Sponsorship scoring: %d updated, %d failed", updated, failed)
    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="One-time LLM sponsorship-confidence scoring for test_creator_brand_partnership_posts."
    )
    parser.add_argument("--limit", type=int, default=500, help="Max pending rows to process this run.")
    parser.add_argument(
        "--brand-id", type=int, default=None, dest="brand_raw_id",
        help="Scope this run to one brand's test_creator_brand_partnership_posts rows (brands_raw.id).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        updated = score_post_sponsorship(db, limit=args.limit, brand_raw_id=args.brand_raw_id)
        print(f"score_post_sponsorship: updated={updated}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
