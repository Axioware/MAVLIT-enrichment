"""
pipeline/enrichment_re/promote_brand_post_creators.py

Promote confirmed brand-post creators into content_creator_re.

Sibling of discover_collaborator_creators.py, but instead of reading the
usernames in post_collaborators it promotes the instagram_users creators
themselves — rows with user_type mention / tagged_user / coauthor_producer
(never commenter or contentcreatorRE) whose username is referenced
(tagged_users / mentions / coauthor_producers / sponsors) on an
instagram_posts row with sponsorship_confidence >= 96.

For up to --limit distinct usernames per run (no --limit = all remaining,
in batches of 50 so progress is saved as it goes):

  1. Pick qualifying usernames not already in content_creator_re
     (case-insensitive), optionally filtered by --niche / --gender /
     --user-type on the instagram_users row.
  2. One LLM call reads the profile + up to 5 posts ALREADY stored in
     instagram_users (no Apify scrape) to double-check creator vs brand AND
     pick the niche: Music, Beauty, Health, Fitness or Other — the same
     check discover_collaborator_creators.py runs after scraping.
  3. Save to content_creator_re:
       - creator -> niche Music/Beauty/Health/Fitness/Other, is_scraped=False
         (content_creator_re.py picks it up on its next run)
       - brand -> niche "Brand", is_scraped=True (never scraped as a creator)

A saved username is in content_creator_re, so later runs skip it. A username
whose LLM call fails is not saved and is retried on the next run.

Run from the project root:
    python -m pipeline.enrichment_re.promote_brand_post_creators --dry-run
    python -m pipeline.enrichment_re.promote_brand_post_creators --niche music --gender female --user-type coauthor_producer --limit 10
    python -m pipeline.enrichment_re.promote_brand_post_creators            # all
"""

import argparse
import json
import logging
import time
from collections import defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

from config import OPENAI_KEY
from pipeline.db import InstagramPost, InstagramUser, SessionLocal
from pipeline.enrichment_re.discover_collaborator_creators import (
    BRAND_NICHE,
    NICHE_PROMPT,
    NICHES,
    POSTS_PER_CREATOR,
    _bare_username,
    _existing_creator_usernames,
    _save_creator,
)
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template

logger = logging.getLogger(__name__)

USER_TYPES = ("coauthor_producer", "tagged_user", "mention")
MIN_POST_CONFIDENCE = 100   # instagram_posts.sponsorship_confidence


def _confirmed_usernames(db: Session) -> set[str]:
    """Lower-cased usernames referenced on an instagram_posts row scored >= MIN_POST_CONFIDENCE."""
    usernames: set[str] = set()
    posts = (
        db.query(InstagramPost.tagged_users, InstagramPost.mentions,
                 InstagramPost.coauthor_producers, InstagramPost.sponsors)
        .filter(InstagramPost.sponsorship_confidence >= MIN_POST_CONFIDENCE)
        .all()
    )
    for post in posts:
        for field in post:
            for entry in field if isinstance(field, list) else []:
                username = _bare_username(entry)
                if username:
                    usernames.add(username)
    return usernames


def _collect_work(
    db: Session, limit: int,
    niche: str | None = None, gender: str | None = None, user_type: str | None = None,
    skip: set[str] | None = None,
) -> dict[str, list[InstagramUser]]:
    """
    Up to `limit` distinct qualifying usernames -> their instagram_users rows
    (one row per stored post), in first-seen (id) order. Usernames in `skip`
    (failed earlier this run) are left out.
    """
    confirmed = _confirmed_usernames(db) - _existing_creator_usernames(db) - (skip or set())
    if not confirmed:
        return {}

    username = func.lower(func.trim(InstagramUser.username))
    query = db.query(InstagramUser).filter(
        InstagramUser.user_type.in_([user_type] if user_type else USER_TYPES),
        username.in_(sorted(confirmed)),
    )
    if niche:
        query = query.filter(func.lower(func.trim(InstagramUser.niche)) == niche.strip().lower())
    if gender:
        query = query.filter(func.lower(func.trim(InstagramUser.gender)) == gender.strip().lower())

    work: dict[str, list[InstagramUser]] = defaultdict(list)
    for row in query.order_by(InstagramUser.id).all():
        key = row.username.strip().lstrip("@").lower()
        if key not in work and len(work) >= limit:
            continue
        work[key].append(row)
    return dict(work)


def _classify(username: str, rows: list[InstagramUser]) -> tuple[str | None, str | None]:
    """creator/brand + niche from the profile and posts already stored in instagram_users."""
    profile = rows[0]
    posts = [{"caption": (r.caption or "")[:600]} for r in rows[:POSTS_PER_CREATOR] if r.caption]
    result = call_gpt_json(
        fill_template(
            NICHE_PROMPT,
            username=username,
            # fill_template only accepts strings — str() every value.
            full_name=str(profile.full_name or "unknown"),
            bio=str(profile.bio or "none")[:600],
            is_business=str(profile.is_business_account),
            followers=str(profile.followers_count if profile.followers_count is not None else "unknown"),
            posts=json.dumps(posts, ensure_ascii=True),
        ),
        context=f"brand-post creator niche @{username}",
    )
    kind = str(result.get("type") or "").strip().lower()
    if kind == "brand":
        return "brand", None
    if kind != "creator":
        return None, None
    niche = str(result.get("niche") or "").strip().capitalize()
    return "creator", niche if niche in NICHES else "Other"


def run(
    db: Session, limit: int, dry_run: bool = False,
    niche: str | None = None, gender: str | None = None, user_type: str | None = None,
    skip: set[str] | None = None,
) -> dict:
    work = _collect_work(db, limit, niche, gender, user_type, skip)
    db.commit()  # end the read transaction before slow LLM work
    logger.info("Collected %d distinct username(s) to promote", len(work))

    summary = {"usernames": len(work), "brand": 0, "saved": {}, "failed": set()}
    if dry_run:
        for key, rows in work.items():
            logger.info("DRY RUN would classify @%s (%s, %s, %s)",
                        rows[0].username, rows[0].user_type, rows[0].niche, rows[0].gender)
        return summary
    if not OPENAI_KEY:
        logger.error("OPENAI_KEY must be set — nothing processed")
        return summary

    for index, (key, rows) in enumerate(work.items(), start=1):
        username = rows[0].username.strip().lstrip("@")
        tag = f"[{index}/{len(work)}] @{username}"
        try:
            kind, saved_niche = _classify(username, rows)
            if kind is None:
                logger.warning("%s: niche check failed — will retry next run", tag)
                summary["failed"].add(key)
                continue
            if kind == "brand":
                saved_niche = BRAND_NICHE
                summary["brand"] += 1
            _save_creator(db, username, saved_niche)
            summary["saved"][saved_niche] = summary["saved"].get(saved_niche, 0) + 1
            logger.info("%s: saved as %s", tag, saved_niche)
        except Exception:
            logger.exception("%s: unexpected error — will retry next run", tag)
            db.rollback()
            summary["failed"].add(key)
        time.sleep(0.5)
    return summary


def run_all(
    db: Session, batch_size: int = 50,
    niche: str | None = None, gender: str | None = None, user_type: str | None = None,
) -> dict:
    """Process every remaining username, batch_size at a time. Failures are skipped for the rest of the run."""
    total = {"usernames": 0, "brand": 0, "saved": {}, "failed": set()}
    while True:
        summary = run(db, batch_size, niche=niche, gender=gender, user_type=user_type, skip=total["failed"])
        if not summary["usernames"] or not OPENAI_KEY:
            break
        total["usernames"] += summary["usernames"]
        total["brand"] += summary["brand"]
        total["failed"] |= summary["failed"]
        for saved_niche, count in summary["saved"].items():
            total["saved"][saved_niche] = total["saved"].get(saved_niche, 0) + count
    return total


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        description="Promote brand-post creators (instagram_posts confidence >= 96) into content_creator_re."
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Max distinct usernames this run. Omit to process ALL remaining (in batches of 50).")
    parser.add_argument("--niche", type=str, default=None,
                        help="Only creators whose instagram_users.niche matches, case-insensitive — e.g. music.")
    parser.add_argument("--gender", type=str, default=None,
                        help="Only creators whose instagram_users.gender matches, case-insensitive — e.g. female.")
    parser.add_argument("--user-type", type=str, default=None, dest="user_type",
                        help="Only one user_type: " + ", ".join(USER_TYPES) + " (case/underscore-insensitive).")
    parser.add_argument("--dry-run", action="store_true", help="List the usernames that would be processed; no LLM/writes.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.user_type is not None:
        by_key = {t.replace("_", "").lower(): t for t in USER_TYPES}
        user_type = by_key.get(args.user_type.replace("_", "").replace("-", "").lower())
        if user_type is None:
            parser.error("--user-type must be one of: " + ", ".join(USER_TYPES))
        args.user_type = user_type

    filters = {"niche": args.niche, "gender": args.gender, "user_type": args.user_type}
    db = SessionLocal()
    try:
        if args.limit is not None:
            summary = run(db, args.limit, dry_run=args.dry_run, **filters)
        elif args.dry_run:  # list everything, write nothing
            summary = run(db, 10**9, dry_run=True, **filters)
        else:
            summary = run_all(db, **filters)
    finally:
        db.close()

    print("\n===== BRAND-POST CREATOR PROMOTION =====")
    print(f"usernames read:              {summary['usernames']}")
    if not args.dry_run:
        print(f"brand (saved with niche Brand): {summary['brand']}")
        print(f"saved to content_creator_re: {sum(summary['saved'].values())}  {summary['saved']}")
        print(f"failed (retry next run):     {len(summary['failed'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
