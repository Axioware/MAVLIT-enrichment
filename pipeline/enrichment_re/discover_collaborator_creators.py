"""
pipeline/enrichment_re/discover_collaborator_creators.py

Discover new creators for content_creator_re from the usernames stored in
instagram_users.post_collaborators (the tagged users / mentions / co-authors
/ sponsors of each scraped creator post).

For up to --limit instagram_users rows per run — each row with ALL of its
comma-separated usernames (no --limit = all remaining rows, worked through
in batches of 10 rows so progress is saved as it goes):

  1. Read usernames from instagram_users rows where post_collaborators is set
     and collaborators_checked = false. Usernames already in
     content_creator_re (case-insensitive) or repeated in this run are skipped.
  2. Check every username against Meta's Branded Content Library
     (facebook_branded_content_checker.py) for branded-content posts in its
     date window. No result (0, or the account isn't in the search dropdown)
     stops here — nothing is saved to content_creator_re, no LLM/Apify cost.
     Every check's outcome (found / not_found / not_in_dropdown / error +
     result count) is saved to facebook_branded_content_checks, one row per
     username.
  3. For accounts with >= 1 branded-content result: username-only LLM check —
     creator (a person) or brand/product/company? A brand is saved straight
     to content_creator_re as niche "Brand", is_scraped=True (no Apify).
  4. For creators: scrape 5 posts via Apify, then a second LLM call reads the
     profile + those posts to double-check creator vs brand AND pick the
     niche: Music, Beauty, Health, Fitness or Other. Saved to
     content_creator_re:
       - creator -> niche Music/Beauty/Health/Fitness/Other, is_scraped=False
         (content_creator_re.py picks it up on its next run)
       - brand (caught by the second check) -> niche "Brand", is_scraped=True
         (so content_creator_re.py never scrapes it as a creator)
  5. Mark an instagram_users row collaborators_checked = true once every
     username in it has been handled, so the next run moves on.

A username whose Branded Content check, LLM call or Apify scrape fails is left unhandled — its row stays
collaborators_checked = false and it is retried on the next run.

Only rows from confirmed partner creators are read:
  - user_type mention / tagged_user / coauthor_producer: the creator must be
    referenced (tagged_users / mentions / coauthor_producers / sponsors) on
    an instagram_posts row with sponsorship_confidence >= 95.
  - user_type contentcreatorRE: the creator must have a
    test_creator_brand_partnership_posts row with sponsorship_confidence >= 90.
Commenter rows are never read.

--niche / --gender limit the run to instagram_users rows whose creator
niche (instagram_users.niche) / gender (instagram_users.gender) match,
case-insensitive — e.g. --niche music --gender female --limit 1 processes
every comma-separated username of the first unchecked female Music creator
row. --user-type limits it to one creator user_type: contentcreatorRE,
coauthor_producer, tagged_user or mention.

Run from the project root:
    python -m pipeline.enrichment_re.discover_collaborator_creators --dry-run
    python -m pipeline.enrichment_re.discover_collaborator_creators --limit 50
    python -m pipeline.enrichment_re.discover_collaborator_creators --niche music --limit 1
    python -m pipeline.enrichment_re.discover_collaborator_creators --niche music --gender female --limit 1
    python -m pipeline.enrichment_re.discover_collaborator_creators --niche music --gender female --user-type contentcreatorRE --limit 10
    python -m pipeline.enrichment_re.discover_collaborator_creators            # all
"""

import argparse
import asyncio
import json
import logging
import time

from sqlalchemy import and_, func, or_, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from config import APIFY_TOKEN, OPENAI_KEY
from pipeline.db import (
    Base,
    ContentCreatorRE,
    FacebookBrandedContentCheck,
    InstagramPost,
    InstagramUser,
    SessionLocal,
    TestCreatorBrandPartnershipPost,
    engine,
)
from pipeline.enrichment.instagram_users import _profile_from_posts, _scrape_posts
from pipeline.enrichment_re.facebook_branded_content_checker import (
    END_DATE,
    START_DATE,
    check_branded_content,
)
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template

logger = logging.getLogger(__name__)

POSTS_PER_CREATOR = 5
NICHES = ("Music", "Beauty", "Health", "Fitness", "Other")
BRAND_NICHE = "Brand"
_ROW_BATCH = 200  # instagram_users rows read per query while collecting usernames

# Which instagram_users rows count as confirmed partner creators.
_BRAND_POST_USER_TYPES = ("mention", "tagged_user", "coauthor_producer")
_BRAND_POST_MIN_CONFIDENCE = 90   # instagram_posts.sponsorship_confidence
_RE_USER_TYPE = "contentcreatorRE"
_RE_MIN_CONFIDENCE = 90           # test_creator_brand_partnership_posts.sponsorship_confidence
_USER_TYPES = (_RE_USER_TYPE,) + _BRAND_POST_USER_TYPES   # valid --user-type values

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
    # The checks table is new (create_all skips it when it exists, and only
    # touches this table — no lock on instagram_users).
    Base.metadata.create_all(bind=engine, tables=[FacebookBrandedContentCheck.__table__])
    # ALTER TABLE ... IF NOT EXISTS still takes an exclusive lock, which queues
    # behind any open instagram_users transaction (e.g. enrich_instagram_users
    # mid-Apify scrape) and blocks every other query on the table — so only
    # run it when a column is actually missing.
    existing = {
        name for (name,) in db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'instagram_users' "
            "AND column_name IN ('post_collaborators', 'collaborators_checked')"
        ))
    }
    db.commit()
    if existing == {"post_collaborators", "collaborators_checked"}:
        return
    db.execute(text("ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS post_collaborators TEXT"))
    db.execute(text(
        "ALTER TABLE instagram_users ADD COLUMN IF NOT EXISTS "
        "collaborators_checked BOOLEAN NOT NULL DEFAULT false"
    ))
    db.commit()


def _save_branded_content_check(
    db: Session, username: str, status: str,
    instagram_id: str | None = None, result_count: int | None = None, error: str | None = None,
) -> None:
    """Upsert this username's latest Branded Content Library check outcome."""
    values = {
        "username": username, "instagram_id": instagram_id, "status": status,
        "result_count": result_count, "error": (error or "")[:2000] or None,
        "start_date": START_DATE, "end_date": END_DATE,
    }
    statement = insert(FacebookBrandedContentCheck).values(**values)
    db.execute(statement.on_conflict_do_update(
        index_elements=[FacebookBrandedContentCheck.username],
        set_={**{k: statement.excluded[k] for k in values if k != "username"}, "checked_at": func.now()},
    ))
    db.commit()


def _split_usernames(value: str | None) -> list[str]:
    return [u.strip().lstrip("@") for u in (value or "").split(",") if u.strip().lstrip("@")]


def _bare_username(entry) -> str | None:
    """instagram_posts JSON list entries are usernames (older rows may be {"username": ...})."""
    value = entry.get("username") if isinstance(entry, dict) else entry
    if not isinstance(value, str):
        return None
    value = value.strip().lstrip("@").lower()
    return value or None


def _qualifying_creator_filter(db: Session):
    """
    Filter clause for instagram_users rows from confirmed partner creators:
    brand-post creators referenced on an instagram_posts row with
    sponsorship_confidence >= _BRAND_POST_MIN_CONFIDENCE, and RE creators with
    a test_creator_brand_partnership_posts row at >= _RE_MIN_CONFIDENCE.
    """
    brand_post_creators: set[str] = set()
    posts = (
        db.query(InstagramPost.tagged_users, InstagramPost.mentions,
                 InstagramPost.coauthor_producers, InstagramPost.sponsors)
        .filter(InstagramPost.sponsorship_confidence >= _BRAND_POST_MIN_CONFIDENCE)
        .all()
    )
    for post in posts:
        for field in post:
            for entry in field if isinstance(field, list) else []:
                username = _bare_username(entry)
                if username:
                    brand_post_creators.add(username)

    re_creators = {
        u.lower() for (u,) in
        db.query(func.lower(func.trim(TestCreatorBrandPartnershipPost.creator_username)))
        .filter(
            TestCreatorBrandPartnershipPost.sponsorship_confidence >= _RE_MIN_CONFIDENCE,
            TestCreatorBrandPartnershipPost.creator_username.isnot(None),
        )
        .distinct()
        .all()
        if u
    }

    username = func.lower(func.trim(InstagramUser.username))
    return or_(
        and_(InstagramUser.user_type.in_(_BRAND_POST_USER_TYPES), username.in_(sorted(brand_post_creators) or [""])),
        and_(InstagramUser.user_type == _RE_USER_TYPE, username.in_(sorted(re_creators) or [""])),
    )


def _existing_creator_usernames(db: Session) -> set[str]:
    return {
        u.lower() for (u,) in
        db.query(func.lower(func.trim(ContentCreatorRE.username)))
        .filter(ContentCreatorRE.username.isnot(None))
        .all()
        if u
    }


def _collect_work(
    db: Session, limit: int, skip_row_ids: set[int] | None = None,
    niche: str | None = None, gender: str | None = None, user_type: str | None = None,
) -> tuple[list[str], dict[int, list[str]]]:
    """
    Walk unchecked instagram_users rows in id order and collect up to `limit`
    rows that have at least one new username, each with ALL of its new
    usernames. Returns (usernames_to_process, {row_id: [usernames]}) — every
    row in the map is fully covered, either by usernames being processed now
    or ones that need no work (already in content_creator_re / already queued
    this run). Rows with no new usernames are included (so they get marked
    checked) but don't count toward the limit. Rows in skip_row_ids (ones
    that already failed earlier in this run) are ignored. niche / gender
    restrict to rows whose instagram_users.niche / gender match
    (case-insensitive); user_type restricts to one instagram_users.user_type.
    """
    known = _existing_creator_usernames(db)
    qualifying_creator = _qualifying_creator_filter(db)
    queued: list[str] = []
    queued_keys: set[str] = set()
    rows: dict[int, list[str]] = {}
    rows_with_work = 0
    last_id = 0

    while rows_with_work < limit:
        query = db.query(InstagramUser.id, InstagramUser.post_collaborators).filter(
            InstagramUser.collaborators_checked == False,  # noqa: E712
            InstagramUser.post_collaborators.isnot(None),
            InstagramUser.post_collaborators != "",
            InstagramUser.id > last_id,
            qualifying_creator,
        )
        if niche:
            query = query.filter(func.lower(func.trim(InstagramUser.niche)) == niche.strip().lower())
        if gender:
            query = query.filter(func.lower(func.trim(InstagramUser.gender)) == gender.strip().lower())
        if user_type:
            query = query.filter(InstagramUser.user_type == user_type)
        batch = query.order_by(InstagramUser.id).limit(_ROW_BATCH).all()
        if not batch:
            break
        for row_id, value in batch:
            last_id = row_id
            if skip_row_ids and row_id in skip_row_ids:
                continue
            usernames = _split_usernames(value)
            new = [u for u in dict.fromkeys(usernames) if u.lower() not in known and u.lower() not in queued_keys]
            for username in new:
                queued.append(username)
                queued_keys.add(username.lower())
            rows[row_id] = usernames
            if new:
                rows_with_work += 1
                if rows_with_work >= limit:
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
            # fill_template only accepts strings — str() every value.
            full_name=str(profile.get("fullName") or "unknown"),
            bio=str(profile.get("biography") or "none")[:600],
            is_business=str(profile.get("isBusinessAccount")),
            followers=str(profile.get("followersCount") if profile.get("followersCount") is not None else "unknown"),
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


def _process_username(db: Session, username: str, summary: dict, tag: str) -> bool:
    """
    Branded Content check first, then classify and save. Returns False when
    the username should be retried next run.
    """
    # Step 1 — Branded Content Library gate, for every username: only accounts
    # with at least one branded-content post in the checker's date window go on.
    logger.info("%s: checking Branded Content Library", tag)
    try:
        branded = asyncio.run(check_branded_content(username))
    except Exception as exc:  # CheckError or any browser failure — record it, retry next run
        _save_branded_content_check(db, username, "error", error=f"{type(exc).__name__}: {exc}")
        logger.warning("%s: Branded Content check failed (%s) — will retry next run", tag, exc)
        return False
    _save_branded_content_check(
        db, username,
        "found" if branded.result_count >= 1 else ("not_found" if branded.instagram_id else "not_in_dropdown"),
        instagram_id=branded.instagram_id, result_count=branded.result_count,
    )
    if branded.result_count < 1:
        summary["no_branded_content"] += 1
        logger.info(
            "%s: no branded content (Instagram ID %s) — skipped",
            tag, branded.instagram_id or "not in search dropdown",
        )
        return True
    logger.info("%s: %d branded-content result(s)", tag, branded.result_count)

    # Step 2 — cheap username-only check. Brands are saved straight away
    # (niche Brand, is_scraped=True) — no Apify.
    kind = _classify_username(username)
    if kind is None:
        logger.warning("%s: username check failed — will retry next run", tag)
        return False
    if kind == "brand":
        _save_creator(db, username, BRAND_NICHE)
        summary["brand_by_username"] += 1
        summary["saved"][BRAND_NICHE] = summary["saved"].get(BRAND_NICHE, 0) + 1
        logger.info("%s: brand (username check) — saved as %s", tag, BRAND_NICHE)
        return True

    # Step 3 — creators: scrape 5 posts, then the profile check confirms
    # creator vs brand and picks the niche. Both outcomes are saved.
    logger.info("%s: creator (username check) — scraping %d posts", tag, POSTS_PER_CREATOR)
    posts = _scrape_posts(username, n=POSTS_PER_CREATOR)
    if posts is None:
        logger.warning("%s: Apify scrape failed — will retry next run", tag)
        return False
    if not posts:
        logger.info("%s: no posts (private/deleted) — skipped", tag)
        return True

    kind, niche = _classify_niche(username, posts)
    if kind is None:
        logger.warning("%s: niche check failed — will retry next run", tag)
        return False
    if kind == "brand":
        niche = BRAND_NICHE
        summary["brand_by_profile"] += 1

    _save_creator(db, username, niche)
    summary["saved"][niche] = summary["saved"].get(niche, 0) + 1
    logger.info("%s: saved as %s", tag, niche)
    time.sleep(0.5)
    return True


def run(
    db: Session, limit: int, dry_run: bool = False, skip_row_ids: set[int] | None = None,
    niche: str | None = None, gender: str | None = None, user_type: str | None = None,
) -> dict:
    _ensure_columns(db)
    usernames, rows = _collect_work(db, limit, skip_row_ids, niche, gender, user_type)
    db.commit()  # end the read transaction before slow LLM/Apify work
    queued_keys = {u.lower() for u in usernames}
    work_rows = sum(1 for row_usernames in rows.values() if any(u.lower() in queued_keys for u in row_usernames))
    logger.info(
        "Collected %d distinct new username(s) from %d instagram_users row(s) "
        "(+%d row(s) with no new usernames, just marked checked)",
        len(usernames), work_rows, len(rows) - work_rows,
    )

    summary = {"usernames": len(usernames), "brand_by_username": 0, "no_branded_content": 0,
               "brand_by_profile": 0, "saved": {}, "failed": 0, "rows_checked": 0, "failed_row_ids": set()}
    if dry_run:
        for username in usernames:
            logger.info("DRY RUN would classify @%s", username)
        return summary
    if not OPENAI_KEY or not APIFY_TOKEN:
        logger.error("OPENAI_KEY and APIFY_TOKEN must both be set — nothing processed")
        return summary

    failed: set[str] = set()
    for index, username in enumerate(usernames, start=1):
        tag = f"[{index}/{len(usernames)}] @{username}"
        try:
            ok = _process_username(db, username, summary, tag)
        except Exception:
            logger.exception("%s: unexpected error — will retry next run", tag)
            db.rollback()
            ok = False
        if not ok:
            failed.add(username.lower())

    # A row is done only if none of its usernames failed this run.
    done_rows = [
        row_id for row_id, row_usernames in rows.items()
        if not any(u.lower() in failed for u in row_usernames)
    ]
    _mark_rows_checked(db, done_rows)
    summary["failed"] = len(failed)
    summary["rows_checked"] = len(done_rows)
    summary["failed_row_ids"] = set(rows) - set(done_rows)
    return summary


def run_all(
    db: Session, batch_size: int = 10,
    niche: str | None = None, gender: str | None = None, user_type: str | None = None,
) -> dict:
    """
    Process every remaining row, batch_size rows at a time, marking rows
    checked after each batch so a crash mid-way keeps the finished batches.
    Rows that fail are skipped for the rest of this run (so the loop can't
    spin on them) and stay unchecked for the next run.
    """
    total = {"usernames": 0, "brand_by_username": 0, "no_branded_content": 0, "brand_by_profile": 0,
             "saved": {}, "failed": 0, "rows_checked": 0}
    skip_row_ids: set[int] = set()
    batch_no = 0
    while True:
        batch_no += 1
        logger.info("===== batch %d =====", batch_no)
        summary = run(db, batch_size, skip_row_ids=skip_row_ids, niche=niche, gender=gender, user_type=user_type)
        if not summary["usernames"]:
            break
        skip_row_ids |= summary["failed_row_ids"]
        for key in ("usernames", "brand_by_username", "no_branded_content", "brand_by_profile", "failed", "rows_checked"):
            total[key] += summary[key]
        for saved_niche, count in summary["saved"].items():
            total["saved"][saved_niche] = total["saved"].get(saved_niche, 0) + count
        if not OPENAI_KEY or not APIFY_TOKEN:
            break
    return total


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        description="Discover new content_creator_re creators from instagram_users.post_collaborators."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max instagram_users rows to process this run, each with ALL its usernames. Omit to process ALL remaining rows (in batches of 10).",
    )
    parser.add_argument(
        "--niche", type=str, default=None,
        help="Only rows whose creator niche (instagram_users.niche) matches, case-insensitive — e.g. music.",
    )
    parser.add_argument(
        "--gender", type=str, default=None,
        help="Only rows whose creator gender (instagram_users.gender) matches, case-insensitive — e.g. female.",
    )
    parser.add_argument(
        "--user-type", type=str, default=None, dest="user_type",
        help="Only rows of one creator user_type: " + ", ".join(_USER_TYPES) + " (case/underscore-insensitive, e.g. content_creator_re).",
    )
    parser.add_argument("--dry-run", action="store_true", help="List the usernames that would be processed; no LLM/Apify/writes.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.user_type is not None:
        # Accept any casing/underscores, e.g. content_creator_re -> contentcreatorRE.
        by_key = {t.replace("_", "").lower(): t for t in _USER_TYPES}
        user_type = by_key.get(args.user_type.replace("_", "").replace("-", "").lower())
        if user_type is None:
            parser.error("--user-type must be one of: " + ", ".join(_USER_TYPES))
        args.user_type = user_type

    db = SessionLocal()
    try:
        if args.limit is not None:
            summary = run(db, args.limit, dry_run=args.dry_run,
                          niche=args.niche, gender=args.gender, user_type=args.user_type)
        elif args.dry_run:  # list everything, write nothing
            summary = run(db, 10**9, dry_run=True, niche=args.niche, gender=args.gender, user_type=args.user_type)
        else:
            summary = run_all(db, niche=args.niche, gender=args.gender, user_type=args.user_type)
    finally:
        db.close()

    print("\n===== COLLABORATOR CREATOR DISCOVERY =====")
    print(f"usernames read:            {summary['usernames']}")
    if not args.dry_run:
        print(f"no branded content (skipped): {summary['no_branded_content']}")
        print(f"brand (username check):    {summary['brand_by_username']}  (saved with niche Brand)")
        print(f"brand (profile check):     {summary['brand_by_profile']}  (saved with niche Brand)")
        print(f"saved to content_creator_re: {sum(summary['saved'].values())}  {summary['saved']}")
        print(f"failed (retry next run):   {summary['failed']}")
        print(f"instagram_users rows marked collaborators_checked: {summary['rows_checked']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
