"""
pipeline/enrichment_re/backfill_partnership_post_content.py

One-time backfill: test_creator_brand_partnership_posts gained caption,
paid_partnership, mentions, tagged_users and coauthor_producers columns
after rows already existed in it (see pipeline/db.py). This re-fetches each
existing row's post directly from Apify by post_url and fills those columns
in.

Safe to re-run — only rows with caption IS NULL are picked up, so a
partially-completed run just resumes where it left off.
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

from config import APIFY_TOKEN
from pipeline.db import SessionLocal, TestCreatorBrandPartnershipPost
from pipeline.enrichment.instagram_posts import _real_coauthors, _usernames_only
from pipeline.enrichment_re.content_creator_re import _ensure_partnership_evidence_table
from pipeline.helpers.apify import run_apify_actor
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_ACTOR_ID = "shu8hvrXbJbY3Eb9W"


def _fetch_post(post_url: str) -> dict | None:
    """
    Fetch a single Instagram post's data via Apify. require_success=True so
    an actor failure (rather than a genuinely empty result) is distinguished
    in the caller's logging, though both currently skip the row the same way.
    """
    items = run_apify_actor(
        _ACTOR_ID,
        {
            "directUrls":   [post_url],
            "resultsType":  "posts",
            "resultsLimit": 1,
        },
        label=f"Backfill {post_url}",
        require_success=True,
    )
    return items[0] if items else None


def backfill_partnership_post_content(db: Session, limit: int = 500) -> tuple[int, int]:
    """
    Fill caption/paid_partnership/mentions/tagged_users/coauthor_producers on
    up to `limit` rows in test_creator_brand_partnership_posts where
    caption IS NULL (i.e. rows saved before these columns existed).

    Returns (updated, failed).
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping backfill")
        return 0, 0

    _ensure_partnership_evidence_table()

    rows: list[TestCreatorBrandPartnershipPost] = (
        db.query(TestCreatorBrandPartnershipPost)
        .filter(TestCreatorBrandPartnershipPost.caption.is_(None))
        .limit(limit)
        .all()
    )

    if not rows:
        logger.info("Backfill: no rows pending")
        return 0, 0

    logger.info("Backfill: processing %d row(s)", len(rows))
    updated = 0
    failed = 0

    for row in rows:
        item = _fetch_post(row.post_url)
        if item is None:
            logger.warning(
                "Backfill: id=%d @%s — Apify fetch failed/empty for %s",
                row.id, row.creator_username, row.post_url,
            )
            failed += 1
            time.sleep(1.0)
            continue

        handle = normalize_handle(row.brand_instagram_handle or "")
        row.caption = item.get("caption")
        row.paid_partnership = item.get("paidPartnership")
        row.mentions = item.get("mentions")
        row.tagged_users = _usernames_only(item.get("taggedUsers"))
        row.coauthor_producers = _usernames_only(_real_coauthors(item, handle))
        db.commit()
        updated += 1
        logger.info(
            "Backfill: id=%d @%s → paid=%s mentions=%d tagged=%d coauthors=%d",
            row.id, row.creator_username, row.paid_partnership,
            len(row.mentions or []), len(row.tagged_users or []), len(row.coauthor_producers or []),
        )
        time.sleep(1.0)

    logger.info("Backfill: %d updated, %d failed", updated, failed)
    return updated, failed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="One-time backfill of post content for test_creator_brand_partnership_posts."
    )
    parser.add_argument("--limit", type=int, default=500, help="Max pending rows to process this run.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        updated, failed = backfill_partnership_post_content(db, limit=args.limit)
        print(f"backfill_partnership_post_content: updated={updated} failed={failed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
