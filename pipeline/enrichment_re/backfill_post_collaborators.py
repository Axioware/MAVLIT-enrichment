"""TEMPORARY backfill script — delete after use.

For each content_creator_re creator: scrape their latest 40 posts (Apify), then
  1. fill instagram_users.post_collaborators (tagged users, mentions,
     co-authors, sponsors) on the creator's posts that are already stored there;
  2. run the same brand check as content_creator_re.py on every scraped post
     (brand_check LLM prompt) - confirmed brands get a brands_raw row if new,
     their referral flag, a test_creator_brand_partnership_posts evidence row,
     and a brand_instagram_users link to the creator.

Unlike content_creator_re.py it does NOT:
  - scrape or store commenters
  - re-classify demographics
  - insert new instagram_users rows (posts not already stored are only counted)
  - change is_scraped

Defaults to the 22 Music creators that have a sponsorship_confidence >= 90
partnership with a refferls=false brand.

Run from the project root:
    python -m pipeline.enrichment_re.backfill_post_collaborators --dry-run
    python -m pipeline.enrichment_re.backfill_post_collaborators
    python -m pipeline.enrichment_re.backfill_post_collaborators --creator-id 4 --creator-id 5
"""

import argparse
import logging
import time

from sqlalchemy import func, text

from pipeline.db import (
    ContentCreatorRE,
    InstagramUser,
    SessionLocal,
    TestCreatorBrandPartnershipPost,
)
from pipeline.enrichment.instagram_users import (
    _link_to_brand,
    _post_collaborators_str,
    _profile_from_posts,
    _scrape_posts,
)
from pipeline.enrichment_re.content_creator_re import (
    _check_post_for_brands,
    _ensure_partnership_evidence_table,
    _get_or_create_brand_id,
    _mark_brand_referral,
    _record_llm_partnership_post,
)
from pipeline.helpers.social import normalize_handle

MUSIC_CREATOR_IDS = [
    4, 5, 25, 31, 38, 40, 66, 78, 80, 83, 84,
    85, 86, 87, 88, 90, 119, 121, 128, 179, 180, 181,
]
POSTS_PER_CREATOR = 40

logger = logging.getLogger(__name__)


def backfill_creator(db, creator: ContentCreatorRE, dry_run: bool) -> dict:
    username = normalize_handle(creator.username or "")
    stats = {"id": creator.id, "username": username, "scraped": 0, "updated": 0,
             "with_collaborators": 0, "not_stored": 0, "brands": 0, "new_brands": [],
             "status": "ok"}
    if not username:
        stats["status"] = "no username"
        return stats

    stored = (
        db.query(InstagramUser)
        .filter(
            func.lower(InstagramUser.username) == username.lower(),
            InstagramUser.user_type == "contentcreatorRE",
            InstagramUser.post_id.isnot(None),
        )
        .all()
    )
    rows_by_post_id = {row.post_id: row for row in stored}
    brands_before = _partner_brand_ids(db, creator.id)
    db.commit()  # don't hold locks during the slow Apify scrape

    if dry_run:
        stats["status"] = f"dry run ({len(rows_by_post_id)} posts stored)"
        return stats

    raw_posts = _scrape_posts(username, n=POSTS_PER_CREATOR)
    if raw_posts is None:
        stats["status"] = "scrape failed"
        return stats
    stats["scraped"] = len(raw_posts)

    for item in raw_posts:
        post_id = str(item.get("id") or "")
        row = rows_by_post_id.get(post_id)
        if row is None:
            stats["not_stored"] += 1
            continue
        collaborators = _post_collaborators_str(item, username)
        db.query(InstagramUser).filter(InstagramUser.id == row.id).update(
            {"post_collaborators": collaborators}, synchronize_session=False,
        )
        stats["updated"] += 1
        if collaborators:
            stats["with_collaborators"] += 1
    db.commit()

    # Brand check - same steps as content_creator_re.py, minus commenters.
    full_name = _profile_from_posts(raw_posts).get("fullName")
    confirmed_brand_ids: set[int] = set()
    for item in raw_posts:
        db.commit()  # release locks before the LLM call
        brand_matches = _check_post_for_brands(db, username, full_name, item)
        if brand_matches:
            time.sleep(0.3)
        for brand_match in brand_matches:
            brand_username = brand_match["username"]
            brand_id = _get_or_create_brand_id(db, brand_username)
            if not brand_id:
                continue
            _mark_brand_referral(db, brand_id, brand_match.get("has_referral_code", False))
            _record_llm_partnership_post(
                db,
                brand_id=brand_id,
                brand_username=brand_username,
                creator_row_id=creator.id,
                creator_username=username,
                creator_name=full_name,
                item=item,
            )
            confirmed_brand_ids.add(brand_id)

    for brand_id in confirmed_brand_ids:
        _link_to_brand(db, brand_id, username)
    db.commit()

    stats["brands"] = len(confirmed_brand_ids)
    stats["new_brands"] = sorted(_partner_brand_ids(db, creator.id) - brands_before)
    db.commit()
    return stats


def _partner_brand_ids(db, creator_id: int) -> set[int]:
    """brands_raw ids this creator already has partnership evidence for."""
    return {
        brand_id for (brand_id,) in
        db.query(TestCreatorBrandPartnershipPost.brand_raw_id)
        .filter(TestCreatorBrandPartnershipPost.content_creator_re_id == creator_id)
        .distinct()
        .all()
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description="TEMP: backfill instagram_users.post_collaborators for creators.")
    parser.add_argument(
        "--creator-id", type=int, action="append", dest="creator_ids",
        help="content_creator_re id to backfill (repeatable). Defaults to the 22 Music creators.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only show how many posts each creator has stored.")
    args = parser.parse_args()
    creator_ids = args.creator_ids or MUSIC_CREATOR_IDS

    _ensure_partnership_evidence_table()
    db = SessionLocal()
    try:
        db.execute(text("ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS post_collaborators TEXT"))
        db.commit()

        creators = (
            db.query(ContentCreatorRE)
            .filter(ContentCreatorRE.id.in_(creator_ids))
            .order_by(ContentCreatorRE.id)
            .all()
        )
        db.commit()
        missing = sorted(set(creator_ids) - {c.id for c in creators})
        if missing:
            logger.warning("content_creator_re id(s) not found: %s", missing)
        logger.info("Backfilling post_collaborators for %d creator(s)", len(creators))

        results = []
        for index, creator in enumerate(creators, start=1):
            try:
                stats = backfill_creator(db, creator, args.dry_run)
            except Exception:
                logger.exception("Creator id=%s failed", creator.id)
                db.rollback()
                stats = {"id": creator.id, "username": creator.username, "scraped": 0, "updated": 0,
                         "with_collaborators": 0, "not_stored": 0, "brands": 0, "new_brands": [],
                         "status": "error"}
            results.append(stats)
            logger.info(
                "[%d/%d] id=%s @%s: scraped=%d updated=%d with_collaborators=%d not_stored=%d "
                "brands=%d new_brands=%s (%s)",
                index, len(creators), stats["id"], stats["username"], stats["scraped"],
                stats["updated"], stats["with_collaborators"], stats["not_stored"],
                stats["brands"], stats["new_brands"], stats["status"],
            )
            if not args.dry_run:
                time.sleep(1)

        print("\n===== POST_COLLABORATORS BACKFILL =====")
        for s in results:
            print(f"id={s['id']:>4} @{s['username']}: updated {s['updated']} post(s), "
                  f"{s['with_collaborators']} with collaborators, {s['not_stored']} not stored, "
                  f"{s['brands']} brand(s) confirmed, new brand ids {s['new_brands']} ({s['status']})")
        all_new = sorted({b for s in results for b in s["new_brands"]})
        print(f"TOTAL updated: {sum(s['updated'] for s in results)} post(s)")
        print(f"TOTAL new partner brands: {len(all_new)} {all_new}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
