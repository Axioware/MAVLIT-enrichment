"""
pipeline/enrichment_re/refresh_content_creator_re.py

Re-scrape already-scraped content_creator_re creators to catch NEW brand
partnerships they have posted since their last scrape.

A creator is eligible when ALL of:
  - content_creator_re.is_scraped = true
  - content_creator_re.currenttime is older than REFRESH_AFTER_DAYS days
    (currenttime is set when the row is added, then reset to now() after
    every successful refresh — so it means "last refreshed / added")
  - it has at least one test_creator_brand_partnership_posts row with
    sponsorship_confidence >= MIN_PARTNERSHIP_CONFIDENCE (i.e. it is a
    creator that actually does brand deals, worth re-checking)

Oldest currenttime first. Each eligible creator is run through the same
flow as content_creator_re.py (enrich_content_creator_re), with
skip_seen_posts=True: the latest 40 posts are scraped and upserted, but
only posts not already stored for that creator go to the brand_check LLM,
and commenters are collected only for newly confirmed partnership posts.

After a creator is processed successfully its currenttime is set to now().
If its Apify scrape fails, currenttime is left unchanged so the creator is
picked up again on the next run.

New partnership rows get sponsorship_confidence = NULL — run
    python -m pipeline.enrichment_re.score_post_sponsorship
afterwards to score them.

Run with:
    python -m pipeline.enrichment_re.refresh_content_creator_re
    python -m pipeline.enrichment_re.refresh_content_creator_re --days 15 --limit 20 --niche music
    python -m pipeline.enrichment_re.refresh_content_creator_re --dry-run      # list eligible creators only
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

if __package__ in (None, ""):
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import exists, func
from sqlalchemy.orm import Session

from pipeline.db import ContentCreatorRE, SessionLocal, TestCreatorBrandPartnershipPost
from pipeline.enrichment_re.content_creator_re import enrich_content_creator_re

logger = logging.getLogger(__name__)

# Re-scrape creators whose last scrape/refresh is older than this many days.
REFRESH_AFTER_DAYS = 30
# Only creators with at least one partnership at or above this confidence.
MIN_PARTNERSHIP_CONFIDENCE = 90


def eligible_creators(
    db: Session, days: int = REFRESH_AFTER_DAYS, niche: str | None = None, limit: int | None = None,
) -> list[ContentCreatorRE]:
    """Scraped creators due for a refresh, oldest currenttime first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    has_partnership = exists().where(
        TestCreatorBrandPartnershipPost.content_creator_re_id == ContentCreatorRE.id,
        TestCreatorBrandPartnershipPost.sponsorship_confidence >= MIN_PARTNERSHIP_CONFIDENCE,
    )
    query = db.query(ContentCreatorRE).filter(
        ContentCreatorRE.is_scraped.is_(True),
        ContentCreatorRE.currenttime < cutoff,
        has_partnership,
    )
    if niche:
        query = query.filter(ContentCreatorRE.niche.ilike(niche.strip()))
    query = query.order_by(ContentCreatorRE.currenttime, ContentCreatorRE.id)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def refresh_content_creator_re(
    db: Session, days: int = REFRESH_AFTER_DAYS, limit: int | None = None, niche: str | None = None,
) -> tuple[int, set[int]]:
    """
    Refresh every eligible creator (see module docstring). Returns
    (creators_refreshed, brand_raw_ids) — the brands confirmed on new posts,
    for callers that want to chain further enrichment onto them.
    """
    creators = eligible_creators(db, days=days, niche=niche, limit=limit)
    # End the read transaction before scraping: enrich_content_creator_re()
    # runs ALTER TABLE on test_creator_brand_partnership_posts (and others)
    # over its own connection, which waits forever on any lock this session
    # still holds from the eligibility query. Loaded rows stay usable
    # (expire_on_commit=False).
    db.commit()
    if not creators:
        logger.info("Refresh content creator RE: no creators older than %d day(s) to refresh", days)
        return 0, set()

    logger.info("Refresh content creator RE: %d creator(s) older than %d day(s) to refresh", len(creators), days)
    refreshed = 0
    all_brand_ids: set[int] = set()

    for i, creator in enumerate(creators, start=1):
        creator_id, username = creator.id, creator.username
        logger.info("Refresh content creator RE: (%d/%d) @%s (id=%d)", i, len(creators), username, creator_id)

        processed, brand_ids = enrich_content_creator_re(db, creator_ids=[creator_id], skip_seen_posts=True)
        if not processed:
            logger.warning(
                "Refresh content creator RE: @%s (id=%d) not completed (Apify scrape failed) — "
                "currenttime left unchanged so it is retried next run",
                username, creator_id,
            )
            continue

        db.query(ContentCreatorRE).filter(ContentCreatorRE.id == creator_id).update(
            {ContentCreatorRE.currenttime: func.now()}, synchronize_session=False,
        )
        db.commit()
        refreshed += 1
        all_brand_ids |= brand_ids
        logger.info(
            "Refresh content creator RE: @%s done — %d brand(s) confirmed on new posts, currenttime updated",
            username, len(brand_ids),
        )

    logger.info(
        "Refresh content creator RE: %d/%d creator(s) refreshed, %d brand(s) confirmed on new posts",
        refreshed, len(creators), len(all_brand_ids),
    )
    return refreshed, all_brand_ids


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="Re-scrape already-scraped content_creator_re creators for new partnerships.")
    parser.add_argument("--days", type=int, default=REFRESH_AFTER_DAYS,
                        help=f"Refresh creators whose currenttime is older than this many days (default {REFRESH_AFTER_DAYS}).")
    parser.add_argument("--limit", type=int, default=None, help="Max creators to refresh this run (default: all eligible).")
    parser.add_argument("--niche", help="Only refresh creators of this niche (case-insensitive).")
    parser.add_argument("--dry-run", action="store_true", help="Only list the eligible creators; scrape nothing.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.dry_run:
            creators = eligible_creators(db, days=args.days, niche=args.niche, limit=args.limit)
            print(f"{len(creators)} creator(s) eligible (older than {args.days} day(s)):")
            for c in creators:
                print(f"  id={c.id:<5} @{c.username:<28} {c.niche or '':<10} currenttime={c.currenttime:%Y-%m-%d %H:%M}")
            return
        refreshed, brand_ids = refresh_content_creator_re(db, days=args.days, limit=args.limit, niche=args.niche)
        print(f"refresh_content_creator_re refreshed={refreshed}; new_brand_ids={sorted(brand_ids)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
