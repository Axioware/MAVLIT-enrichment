"""
pipeline/enrichment_re/discover_collaborator_creators.py

Discover new creators for content_creator_re from the usernames stored in
instagram_users.post_collaborators (the tagged users / mentions / co-authors
/ sponsors of each scraped creator post).

For up to --limit usernames per run:

  1. Read usernames from instagram_users rows where post_collaborators is set
     and collaborators_checked = false. Usernames already in
     content_creator_re (case-insensitive) or repeated in this run are skipped.
  2. Username-only LLM check: is this a creator (a person) or a
     brand/product/company? Brands stop here — nothing is saved for them.
  3. For creators only: scrape 5 posts via Apify, then a second LLM call reads
     the profile + those posts to double-check creator vs brand AND pick the
     niche: Music, Beauty, Health, Fitness or Other.
  4. Save to content_creator_re:
       - creator -> niche Music/Beauty/Health/Fitness/Other, is_scraped=False
         (content_creator_re.py picks it up on its next run)
       - brand (caught by the second check) -> niche "Brand", is_scraped=True
         (so content_creator_re.py never scrapes it as a creator)
  5. Mark an instagram_users row collaborators_checked = true once every
     username in it has been handled, so the next run moves on.

A username whose Apify scrape fails is left unhandled — its row stays
collaborators_checked = false and it is retried on the next run.

Run from the project root:
    python -m pipeline.enrichment_re.discover_collaborator_creators --dry-run
    python -m pipeline.enrichment_re.discover_collaborator_creators --limit 50
"""

import argparse
import json
import logging
import time

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from config import APIFY_TOKEN, OPENAI_KEY
from pipeline.db import ContentCreatorRE, InstagramUser, SessionLocal
from pipeline.enrichment.instagram_users import _profile_from_posts, _scrape_posts
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template

logger = logging.getLogger(__name__)

POSTS_PER_CREATOR = 5
NICHES = ("Music", "Beauty", "Health", "Fitness", "Other")
BRAND_NICHE = "Brand"
_ROW_BATCH = 200  # instagram_users rows read per query while collecting usernames

USERNAME_TYPE_PROMPT = """You are classifying an Instagram account from its username ONLY.

Username: @{username}

Decide whether this account is most likely:
- "creator": an individual person — influencer, artist, musician, athlete,
  coach, blogger, personal account
- "brand": a brand, product, company, store, organization, agency, media
  outlet, event, venue or any other non-person account

Return ONLY this JSON:
{"type": "creator"}
or
{"type": "brand"}
"""

NICHE_PROMPT = """You are reviewing an Instagram account using its profile and 5 most recent posts.

Username: @{username}
Full name: {full_name}
Bio: {bio}
Business account: {is_business}
Followers: {followers}

Recent posts (caption + hashtags):
{posts}

Step 1 - Double-check the account type:
- "creator": an individual person (influencer, artist, musician, athlete,
  coach, blogger, personal account)
- "brand": a brand, product, company, store, organization, agency, media
  outlet, event or venue

Step 2 - If it is a creator, pick the ONE niche that best fits its content:
- "Music": musicians, singers, producers, DJs, music content
- "Beauty": makeup, skincare, hair, nails, beauty content
- "Health": nutrition, wellness, food-as-health, medical, mental health
- "Fitness": workouts, training, sports performance, gym content
- "Other": anything else

Return ONLY this JSON:
{"type": "creator", "niche": "Music"}
If the account is a brand, return:
{"type": "brand", "niche": null}
"""


def _ensure_columns(db: Session) -> None:
    db.execute(text("ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS post_collaborators TEXT"))
    db.execute(text(
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS "
        "collaborators_checked BOOLEAN NOT NULL DEFAULT false"
    ))
    db.commit()


def _split_usernames(value: str | None) -> list[str]:
    return [u.strip().lstrip("@") for u in (value or "").split(",") if u.strip().lstrip("@")]


def _existing_creator_usernames(db: Session) -> set[str]:
    return {
        u.lower() for (u,) in
        db.query(func.lower(func.trim(ContentCreatorRE.username)))
        .filter(ContentCreatorRE.username.isnot(None))
        .all()
        if u
    }


def _collect_work(db: Session, limit: int) -> tuple[list[str], dict[int, list[str]]]:
    """
    Walk unchecked instagram_users rows in id order and collect up to `limit`
    new usernames. Returns (usernames_to_process, {row_id: [usernames]}) —
    every row in the map is fully covered, either by usernames being
    processed now or ones that need no work (already in content_creator_re /
    already queued this run). A row that would push past the limit is left
    out entirely, so it stays unchecked for the next run — except the first
    row, which is always taken (so a run can go over `limit` when that single
    row has more new usernames than the limit).
    """
    known = _existing_creator_usernames(db)
    queued: list[str] = []
    queued_keys: set[str] = set()
    rows: dict[int, list[str]] = {}
    last_id = 0

    while len(queued) < limit:
        batch = (
            db.query(InstagramUser.id, InstagramUser.post_collaborators)
            .filter(
                InstagramUser.collaborators_checked == False,  # noqa: E712
                InstagramUser.post_collaborators.isnot(None),
                InstagramUser.post_collaborators != "",
                InstagramUser.id > last_id,
            )
            .order_by(InstagramUser.id)
            .limit(_ROW_BATCH)
            .all()
        )
        if not batch:
            break
        for row_id, value in batch:
            last_id = row_id
            usernames = _split_usernames(value)
            new = [u for u in dict.fromkeys(usernames) if u.lower() not in known and u.lower() not in queued_keys]
            # Rows are all-or-nothing (that's what collaborators_checked tracks),
            # so stop before a row that would overshoot the limit — but always
            # take at least the first row, or a row with more usernames than
            # --limit would block every run forever.
            if new and queued and len(queued) + len(new) > limit:
                return queued, rows
            for username in new:
                queued.append(username)
                queued_keys.add(username.lower())
            rows[row_id] = usernames
            if len(queued) >= limit:
                return queued, rows
    return queued, rows


def _classify_username(username: str) -> str | None:
    result = call_gpt_json(
        fill_template(USERNAME_TYPE_PROMPT, username=username),
        context=f"collaborator type @{username}",
    )
    kind = str(result.get("type") or "").strip().lower()
    return kind if kind in ("creator", "brand") else None


def _classify_niche(username: str, posts: list[dict]) -> tuple[str | None, str | None]:
    profile = _profile_from_posts(posts)
    evidence = [
        {"caption": (p.get("caption") or "")[:600], "hashtags": p.get("hashtags") or []}
        for p in posts
    ]
    result = call_gpt_json(
        fill_template(
            NICHE_PROMPT,
            username=username,
            full_name=profile.get("fullName") or "unknown",
            bio=(profile.get("biography") or "none")[:600],
            is_business=profile.get("isBusinessAccount"),
            followers=profile.get("followersCount"),
            posts=json.dumps(evidence, ensure_ascii=True),
        ),
        context=f"collaborator niche @{username}",
    )
    kind = str(result.get("type") or "").strip().lower()
    if kind == "brand":
        return "brand", None
    if kind != "creator":
        return None, None
    niche = str(result.get("niche") or "").strip().capitalize()
    return "creator", niche if niche in NICHES else "Other"


def _save_creator(db: Session, username: str, niche: str) -> None:
    db.add(ContentCreatorRE(
        username=username,
        niche=niche,
        url=f"https://www.instagram.com/{username}/",
        is_scraped=(niche == BRAND_NICHE),
    ))
    db.commit()


def _mark_rows_checked(db: Session, row_ids: list[int]) -> None:
    if row_ids:
        db.query(InstagramUser).filter(InstagramUser.id.in_(row_ids)).update(
            {"collaborators_checked": True}, synchronize_session=False,
        )
        db.commit()


def run(db: Session, limit: int, dry_run: bool = False) -> dict:
    _ensure_columns(db)
    usernames, rows = _collect_work(db, limit)
    db.commit()  # end the read transaction before slow LLM/Apify work
    logger.info("Collected %d new username(s) from %d instagram_users row(s)", len(usernames), len(rows))

    summary = {"usernames": len(usernames), "brand_by_username": 0, "brand_by_profile": 0,
               "saved": {}, "failed": 0, "rows_checked": 0}
    if dry_run:
        for username in usernames:
            logger.info("DRY RUN would classify @%s", username)
        return summary
    if not OPENAI_KEY or not APIFY_TOKEN:
        logger.error("OPENAI_KEY and APIFY_TOKEN must both be set — nothing processed")
        return summary

    failed: set[str] = set()
    for index, username in enumerate(usernames, start=1):
        kind = _classify_username(username)
        if kind is None:
            logger.warning("[%d/%d] @%s: username check failed — will retry next run", index, len(usernames), username)
            failed.add(username.lower())
            continue
        if kind == "brand":
            summary["brand_by_username"] += 1
            logger.info("[%d/%d] @%s: brand (username check) — skipped", index, len(usernames), username)
            continue

        posts = _scrape_posts(username, n=POSTS_PER_CREATOR)
        if posts is None:
            logger.warning("[%d/%d] @%s: Apify scrape failed — will retry next run", index, len(usernames), username)
            failed.add(username.lower())
            continue
        if not posts:
            logger.info("[%d/%d] @%s: no posts (private/deleted) — skipped", index, len(usernames), username)
            continue

        kind, niche = _classify_niche(username, posts)
        if kind is None:
            logger.warning("[%d/%d] @%s: niche check failed — will retry next run", index, len(usernames), username)
            failed.add(username.lower())
            continue
        if kind == "brand":
            niche = BRAND_NICHE
            summary["brand_by_profile"] += 1

        _save_creator(db, username, niche)
        summary["saved"][niche] = summary["saved"].get(niche, 0) + 1
        logger.info("[%d/%d] @%s: saved as %s", index, len(usernames), username, niche)
        time.sleep(0.5)

    # A row is done only if none of its usernames failed this run.
    done_rows = [
        row_id for row_id, row_usernames in rows.items()
        if not any(u.lower() in failed for u in row_usernames)
    ]
    _mark_rows_checked(db, done_rows)
    summary["failed"] = len(failed)
    summary["rows_checked"] = len(done_rows)
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        description="Discover new content_creator_re creators from instagram_users.post_collaborators."
    )
    parser.add_argument("--limit", type=int, default=50, help="Max new usernames to process this run (default 50).")
    parser.add_argument("--dry-run", action="store_true", help="List the usernames that would be processed; no LLM/Apify/writes.")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")

    db = SessionLocal()
    try:
        summary = run(db, args.limit, dry_run=args.dry_run)
    finally:
        db.close()

    print("\n===== COLLABORATOR CREATOR DISCOVERY =====")
    print(f"usernames read:            {summary['usernames']}")
    if not args.dry_run:
        print(f"brand (username check):    {summary['brand_by_username']}")
        print(f"brand (profile check):     {summary['brand_by_profile']}  (saved with niche Brand)")
        print(f"saved to content_creator_re: {sum(summary['saved'].values())}  {summary['saved']}")
        print(f"failed (retry next run):   {summary['failed']}")
        print(f"instagram_users rows marked collaborators_checked: {summary['rows_checked']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
