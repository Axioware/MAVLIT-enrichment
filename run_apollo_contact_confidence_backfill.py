"""
One-time backfill of LLM sponsorship-contact confidence scores.

Scores every brand_contacts row whose sponsorship confidence is NULL with
OpenAI and updates only brand_contacts.sponsorship_contact_confidence. No
Apollo calls are made and no other contact fields are changed.

Run from the repository root:
    python3 run_apollo_contact_confidence_backfill.py

Optional:
    python3 run_apollo_contact_confidence_backfill.py --batch-size 25
    python3 run_apollo_contact_confidence_backfill.py --dry-run
"""

import argparse
import json
import logging

from dotenv import load_dotenv
from sqlalchemy import update

load_dotenv()

from config import OPENAI_KEY
from pipeline.db import BrandContact, BrandRaw, SessionLocal
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import APOLLO_RANK_DEFAULT_PROMPT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 25


def _score_batch(rows: list[tuple[BrandContact, str | None]]) -> dict[int, int]:
    candidates = [
        {
            "id": str(contact.id),
            "brand": brand_name or "unknown",
            "title": contact.title or "",
            "departments": contact.departments or "",
            "subdepartments": contact.subdepartments or "",
            "functions": contact.functions or "",
            "seniority": contact.seniority or "",
        }
        for contact, brand_name in rows
    ]
    prompt = fill_template(
        APOLLO_RANK_DEFAULT_PROMPT,
        intro=(
            "MAVLIT is backfilling sponsorship-contact confidence scores for existing "
            "company contacts. Score each contact independently for how likely they "
            "are to personally own, manage, approve, negotiate, coordinate, or respond "
            "to creator sponsorship opportunities."
        ),
        title_hint=(
            "Use the confidence scale and conservative 90+ scoring rules below. "
            "This is a score-only backfill; the confidence_score is the important "
            "output for each existing contact."
        ),
        candidates=json.dumps(candidates, indent=2),
    )
    result = call_gpt_json(
        prompt,
        context=f"Apollo contact confidence backfill ({len(rows)} candidates)",
        timeout=180.0,
    )
    raw_scores = result.get("picks", []) if isinstance(result, dict) else []
    if not isinstance(raw_scores, list):
        return {}

    valid_ids = {str(contact.id) for contact, _brand_name in rows}
    scores: dict[int, int] = {}
    for item in raw_scores:
        if not isinstance(item, dict):
            continue
        contact_id = str(item.get("id", ""))
        if contact_id not in valid_ids or contact_id in {str(key) for key in scores}:
            continue
        try:
            score = max(0, min(100, int(item["confidence_score"])))
        except (KeyError, TypeError, ValueError):
            continue
        scores[int(contact_id)] = score
    return scores


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true", help="Call the LLM but do not write scores")
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if not OPENAI_KEY:
        raise SystemExit("OPENAI_KEY is not set")

    db = SessionLocal()
    try:
        rows = (
            db.query(BrandContact, BrandRaw.name)
            .outerjoin(BrandRaw, BrandRaw.id == BrandContact.brand_raw_id)
            .filter(BrandContact.sponsorship_contact_confidence.is_(None))
            .order_by(BrandContact.id)
            .all()
        )
        total = len(rows)
        logger.info("Found %d brand contact row(s) to score", total)

        scored = 0
        failed_batches = 0
        for start in range(0, total, args.batch_size):
            batch = rows[start:start + args.batch_size]
            logger.info("Scoring batch %d-%d of %d", start + 1, start + len(batch), total)
            scores = _score_batch(batch)
            if not scores:
                failed_batches += 1
                logger.error("No valid scores returned for batch starting at row %d", start + 1)
                continue

            if not args.dry_run:
                for contact_id, score in scores.items():
                    db.execute(
                        update(BrandContact)
                        .where(BrandContact.id == contact_id)
                        .values(sponsorship_contact_confidence=score)
                    )
                db.commit()

            scored += len(scores)
            logger.info(
                "Batch complete: %d score(s) accepted%s",
                len(scores),
                " (dry run)" if args.dry_run else " and saved",
            )

        logger.info(
            "DONE: %d/%d contact(s) scored, %d batch(es) without valid LLM scores%s",
            scored,
            total,
            failed_batches,
            " (dry run; no database changes)" if args.dry_run else "",
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
