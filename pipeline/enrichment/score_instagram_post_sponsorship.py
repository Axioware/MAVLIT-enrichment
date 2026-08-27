"""
pipeline/enrichment/score_instagram_post_sponsorship.py

For every row in instagram_posts (brand-side posts, scraped from the
brand's own Instagram account — see instagram_posts.py) that references at
least one account via sponsors/tagged_users/mentions/coauthor_producers,
ask an LLM (hardcoded prompt, no Prompt-table entry) to estimate a 0-100
confidence that this specific post is a paid sponsorship/collaboration
between the brand and whichever creator(s) it references. Writes the score
into instagram_posts.sponsorship_confidence.

This is the reverse direction of pipeline/enrichment_re/score_post_sponsorship.py,
which scores a CREATOR's own post against one specific brand. Here the post
belongs to the BRAND and may reference multiple creators at once, so the
score reflects the post as a whole rather than one specific creator/brand pair.

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

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from config import OPENAI_KEY
from pipeline.db import Base, InstagramPost, SessionLocal, engine
from pipeline.helpers.gpt_llm import call_gpt_json

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """You are an Instagram sponsorship verification system.

Your task is NOT to identify whether the referenced accounts are creators.

Your task is ONLY to determine whether THIS SPECIFIC INSTAGRAM POST, posted by the BRAND'S own account, is evidence of a paid sponsorship, paid partnership, brand collaboration, ambassador promotion, affiliate promotion, gifted campaign with disclosure, or other commercial relationship between THIS brand and the creator(s) referenced below.

ASSUME THE POST IS NOT A PAID COLLABORATION UNLESS THERE IS POSITIVE EVIDENCE.

Do NOT increase confidence simply because:
- an account is tagged
- an account is mentioned
- an account is listed as a coauthor
- an account is listed as a sponsor field with no other context
- the referenced account is a well-known creator or influencer
- the post is a repost, shoutout, or event photo

A tag, mention, or product reference alone is weak evidence.

Strong evidence includes:
- Instagram paid partnership label
- explicit sponsorship disclosure (#ad, #sponsored, paid partnership, partner, advertisement)
- affiliate/referral/discount code attributed to the creator
- language indicating a commercial relationship (e.g. "our ambassador", "working with", "campaign with")
- direct promotional call-to-action featuring the creator
- giveaway or campaign run jointly with the creator
- clear indication the creator is representing or endorsing the brand

IMPORTANT:

The score must represent the likelihood that THIS POST is a paid collaboration between the BRAND and the creator(s) it references (sponsors / tagged_users / mentions / coauthor_producers combined).

Be skeptical when the referenced accounts look like ordinary customers, employees, event attendees, or other brand/company accounts rather than independent creators.

Scoring rubric:

0-10:
No evidence of a paid collaboration. Ordinary tag, mention, repost, or unrelated account.

11-25:
An account is referenced but there's no evidence of a commercial relationship.

26-40:
Some weak signals, but a paid collaboration is speculative.

41-60:
Possible collaboration, evidence is incomplete.

61-80:
Strong evidence of a genuine commercial relationship with the referenced creator(s).

81-100:
Very strong evidence — explicit paid-partnership marker plus disclosure language, affiliate/discount codes, or multiple strong signals together.

Evaluate only the information provided below.

Brand: {brand_name}

Post caption:
{caption}

Instagram paid partnership flag:
{paid_partnership}

Sponsors:
{sponsors}

Tagged users:
{tagged_users}

Mentions:
{mentions}

Coauthor producers:
{coauthor_producers}

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


def _ensure_column() -> None:
    """Keep this column available for standalone script runs too."""
    Base.metadata.create_all(bind=engine, tables=[InstagramPost.__table__])
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE instagram_posts ADD COLUMN IF NOT EXISTS sponsorship_confidence INTEGER"
        ))
        conn.commit()


def _score_post(
    brand_name: str,
    caption: str | None,
    paid_partnership: bool | None,
    sponsors,
    tagged_users,
    mentions,
    coauthor_producers,
) -> int | None:
    prompt = _PROMPT_TEMPLATE.format(
        brand_name=brand_name or "unknown",
        caption=(caption or "")[:600] or "none",
        paid_partnership=str(bool(paid_partnership)).lower(),
        sponsors=_fmt(sponsors),
        tagged_users=_fmt(tagged_users),
        mentions=_fmt(mentions),
        coauthor_producers=_fmt(coauthor_producers),
    )
    result = call_gpt_json(prompt, context=f"instagram post sponsorship score {brand_name}")
    score = result.get("confidence_pct") if isinstance(result, dict) else None
    if not isinstance(score, (int, float)):
        return None
    return max(0, min(100, round(score)))


def score_instagram_post_sponsorship(db: Session, limit: int = 500) -> tuple[int, int]:
    """
    Fill sponsorship_confidence on up to `limit` rows in instagram_posts
    that reference at least one account (sponsors/tagged_users/mentions/
    coauthor_producers) and where it's still NULL.

    Returns (updated, failed).
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping instagram post sponsorship scoring")
        return 0, 0

    _ensure_column()

    rows: list[InstagramPost] = (
        db.query(InstagramPost)
        .filter(InstagramPost.sponsorship_confidence.is_(None))
        .filter(
            or_(
                InstagramPost.sponsors.isnot(None),
                InstagramPost.tagged_users.isnot(None),
                InstagramPost.mentions.isnot(None),
                InstagramPost.coauthor_producers.isnot(None),
            )
        )
        .limit(limit)
        .all()
    )

    if not rows:
        logger.info("Instagram post sponsorship scoring: no rows pending")
        return 0, 0

    logger.info("Instagram post sponsorship scoring: processing %d row(s)", len(rows))
    updated = 0
    failed = 0

    for row in rows:
        brand_name = row.brand_raw.name if row.brand_raw else None
        score = _score_post(
            brand_name or row.instagram_handle, row.caption, row.paid_partnership,
            row.sponsors, row.tagged_users, row.mentions, row.coauthor_producers,
        )
        if score is None:
            logger.warning(
                "Instagram post sponsorship scoring: id=%d @%s — LLM call failed",
                row.id, row.instagram_handle,
            )
            failed += 1
            time.sleep(0.5)
            continue

        row.sponsorship_confidence = score
        db.commit()
        updated += 1
        logger.info(
            "Instagram post sponsorship scoring: id=%d @%s → %d%%",
            row.id, row.instagram_handle, score,
        )
        time.sleep(0.5)

    logger.info("Instagram post sponsorship scoring: %d updated, %d failed", updated, failed)
    return updated, failed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="One-time LLM sponsorship-confidence scoring for instagram_posts (brand → creator direction)."
    )
    parser.add_argument("--limit", type=int, default=500, help="Max pending rows to process this run.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        updated, failed = score_instagram_post_sponsorship(db, limit=args.limit)
        print(f"score_instagram_post_sponsorship: updated={updated} failed={failed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
