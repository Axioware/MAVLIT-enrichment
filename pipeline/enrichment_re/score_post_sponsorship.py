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

_PROMPT_TEMPLATE = """You evaluate whether an Instagram post is a PAID sponsorship / brand \
partnership between a creator and one specific brand.

Brand: {brand_name}
Creator: {creator_name}
Post caption: {caption}
Instagram-flagged paid partnership: {paid_partnership}
Mentions in post: {mentions}
Tagged users in post: {tagged_users}

Based only on this evidence, estimate how likely it is that this post is a paid \
sponsorship specifically with "{brand_name}" (as opposed to an unpaid mention, a \
tag with no commercial relationship, or a different brand entirely).

Respond with ONLY a JSON object in this exact shape, nothing else:
{{"confidence_pct": <integer from 0 to 100>}}
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


def score_post_sponsorship(db: Session, limit: int = 500) -> tuple[int, int]:
    """
    Fill sponsorship_confidence on up to `limit` rows in
    test_creator_brand_partnership_posts where it's still NULL.
    Returns (updated, failed).
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping sponsorship scoring")
        return 0, 0

    _ensure_partnership_evidence_table()

    rows: list[TestCreatorBrandPartnershipPost] = (
        db.query(TestCreatorBrandPartnershipPost)
        .filter(TestCreatorBrandPartnershipPost.sponsorship_confidence.is_(None))
        .limit(limit)
        .all()
    )

    if not rows:
        logger.info("Sponsorship scoring: no rows pending")
        return 0, 0

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

        row.sponsorship_confidence = score
        db.commit()
        updated += 1
        logger.info(
            "Sponsorship scoring: id=%d @%s/%s → %d%%",
            row.id, row.creator_username, row.brand_name, score,
        )
        time.sleep(0.5)

    logger.info("Sponsorship scoring: %d updated, %d failed", updated, failed)
    return updated, failed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="One-time LLM sponsorship-confidence scoring for test_creator_brand_partnership_posts."
    )
    parser.add_argument("--limit", type=int, default=500, help="Max pending rows to process this run.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        updated, failed = score_post_sponsorship(db, limit=args.limit)
        print(f"score_post_sponsorship: updated={updated} failed={failed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
