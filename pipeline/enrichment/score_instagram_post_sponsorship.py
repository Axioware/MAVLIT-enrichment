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

_PROMPT_TEMPLATE = """You are an Instagram brand-collaboration verification system.

Your task is NOT to identify whether referenced accounts are creators.

Your task is ONLY to determine whether THIS SPECIFIC INSTAGRAM POST, published by the brand's own Instagram account, is evidence of a commercial collaboration between the brand and one or more referenced creator accounts.

Commercial collaborations include:
- paid sponsorships
- paid partnerships
- influencer campaigns
- ambassador relationships
- affiliate relationships
- gifted collaborations with disclosure
- creator marketing campaigns
- brand-funded promotions

ASSUME THERE IS NO COMMERCIAL COLLABORATION UNLESS THERE IS POSITIVE EVIDENCE.

The goal is to determine whether the brand is actively collaborating with a creator in THIS SPECIFIC POST.

Do NOT increase confidence simply because:
- an account is tagged
- an account is mentioned
- an account is a coauthor
- an account appears in the sponsor field
- the account is famous
- the account has many followers
- the account appears in a photo
- the account attended an event
- the account purchased a product
- the account won a contest
- the account is a customer
- the account is an employee
- the account is another business
- the account is another brand
- the account is a retailer or distributor
- the account is a photographer, videographer, venue, agency, or service provider

A tag, mention, coauthor relationship, or sponsor field alone is NOT sufficient evidence of a commercial creator partnership.

Strong evidence includes:
- Instagram paid partnership label
- explicit sponsorship disclosure
- #ad, #sponsored, #paidpartnership
- affiliate, referral, promo, creator, ambassador, or discount codes
- campaign language involving the referenced creator
- language such as:
  - "partnering with"
  - "working with"
  - "collaboration with"
  - "ambassador"
  - "creator partner"
  - "sponsored by"
  - "in partnership with"
- giveaway or promotion run jointly with the creator
- explicit creator-feature campaign
- multiple pieces of evidence pointing to the same creator relationship

IMPORTANT:

The score must represent the likelihood that THIS POST is evidence of a commercial collaboration between the brand and one or more referenced creator accounts.

The score should NOT represent:
- whether the referenced account is famous
- whether the referenced account is a creator
- whether the referenced account has worked with the brand in the past

Only evaluate the evidence present in THIS POST.

If referenced accounts appear to be:
- customers
- employees
- event attendees
- vendors
- photographers
- agencies
- retail stores
- distributors
- partner businesses
- media outlets
- other brands

then assign a low score unless there is explicit evidence of a creator-brand commercial relationship.

Before assigning a score, answer these questions internally:

1. Is there evidence of a commercial collaboration in this post?
2. Which referenced account(s) appear to be part of that collaboration?
3. Does the post clearly indicate a creator-marketing relationship rather than a customer, employee, vendor, or business relationship?

The confidence score should be high only when all three questions support a creator-brand commercial partnership.

Scoring rubric:

0-10:
No evidence of collaboration.

11-20:
Accounts are referenced but there is no commercial evidence.

21-40:
Weak signals. Collaboration is speculative.

41-60:
Possible collaboration but evidence is incomplete.

61-80:
Strong evidence of a creator-brand commercial relationship.

81-100:
Explicit creator partnership, sponsorship disclosure, paid partnership indicator, creator code, ambassador program, or multiple strong signals directly linking the brand and creator.

Evaluate only the information below.

Brand:
{brand_name}

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


def score_instagram_post_sponsorship(
    db: Session, limit: int | None = None, brand_raw_id: int | None = None,
) -> int:
    """
    Fill sponsorship_confidence on rows in instagram_posts that reference at
    least one account (sponsors/tagged_users/mentions/coauthor_producers)
    and where it's still NULL. Processes every pending row by default; pass
    limit to cap how many this call processes.

    Pass brand_raw_id to scope this call to one brand's instagram_posts rows
    instead — still filtered to sponsorship_confidence IS NULL, so repeated
    calls (e.g. limit=5 in a loop) advance through that brand's pending
    posts one batch at a time, matching enrich_instagram_users's
    brand_raw_id= pattern (this operates on instagram_posts ROWS, same as
    that step, not on brands directly).

    Returns the number of rows updated (failures are logged, not returned —
    matching every other enrichment step's fn(db, limit=...) -> int
    convention, since drain_pending_step's loop stops on a falsy return).
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping instagram post sponsorship scoring")
        return 0

    _ensure_column()

    query = (
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
    )
    if brand_raw_id is not None:
        query = query.filter(InstagramPost.brand_raw_id == brand_raw_id)
    if limit is not None:
        query = query.limit(limit)
    rows: list[InstagramPost] = query.all()

    if not rows:
        logger.info("Instagram post sponsorship scoring: no rows pending")
        return 0

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
    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="One-time LLM sponsorship-confidence scoring for instagram_posts (brand → creator direction)."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max pending rows to process this run. Omit to process every pending row.",
    )
    parser.add_argument(
        "--brand-id", type=int, default=None, dest="brand_raw_id",
        help="Scope this run to one brand's instagram_posts rows (brands_raw.id).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        updated = score_instagram_post_sponsorship(db, limit=args.limit, brand_raw_id=args.brand_raw_id)
        print(f"score_instagram_post_sponsorship: updated={updated}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
