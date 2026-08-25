"""
pipeline/enrichment_re/content_creator_re.py

Reverse-engineering flow: content_creator_re is a manually-seeded list
(username, niche, url) with no brand/post behind it. For each row where
is_scraped=False:

  1. Scrape the creator's top 40 posts via Apify and classify demographics
     via the LLM — same _scrape_posts/_profile_from_posts/_classify_demographics
     functions used by the main creator flow in instagram_users.py. niche is
     NOT LLM-classified here — it's copied straight from content_creator_re.niche.
  2. Store one row per post in instagram_users (user_type="contentcreatorRE"),
     with is_content_creator_re=True.
  3. For each of those posts, extract mentions/tagged_users/coauthor_producers/
     paid_partnership and ask the LLM (brand_check prompt) which referenced
     accounts, if any, are real brand/company accounts — not other creators.
     Any confirmed brand gets a bare brands_raw row (instagram_handle only;
     name/niche/source are nullable for exactly this case — see pipeline/db.py)
     and is linked to this creator via brand_instagram_users. The exact
     LLM-confirmed creator/brand/post evidence is also upserted into
     test_creator_brand_partnership_posts. One creator_re row can end up
     linked to several different brands across its scraped posts.
  4. For posts where step 3 confirmed at least one brand, collect up to 5
     commenters for that post only, scrape 1 post each for profile data, store
     them in instagram_users (user_type="commenter") with
     is_content_creator_re=True, and link those commenters only to the brand(s)
     confirmed on that same source post.
  5. Mark content_creator_re.is_scraped=True.

Prompts (editable via /admin > Prompts):
  brand_check — this module only
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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config import APIFY_TOKEN, OPENAI_KEY
from pipeline.db import (
    Base,
    BrandRaw,
    ContentCreatorRE,
    InstagramUser,
    Prompt,
    SessionLocal,
    TestCreatorBrandPartnershipPost,
    engine,
    insert_brand,
)
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import BRAND_CHECK_PROMPT_NAME, BRAND_CHECK_DEFAULT_PROMPT
from pipeline.helpers.social import normalize_handle
from pipeline.enrichment.instagram_posts import _real_coauthors, _usernames_only
from pipeline.enrichment.instagram_users import (
    _build_post_row,
    _classify_demographics,
    _collect_commenter_records,
    _link_commenter_to_creator,
    _link_to_brand,
    _post_url,
    _profile_from_posts,
    _scrape_posts,
)

logger = logging.getLogger(__name__)


def _ensure_partnership_evidence_table() -> None:
    """
    Keep the scratch evidence table available for standalone script runs
    too. The FastAPI app also creates it on startup via Base.metadata.
    """
    Base.metadata.create_all(bind=engine, tables=[TestCreatorBrandPartnershipPost.__table__])
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_test_creator_brand_post
            ON test_creator_brand_partnership_posts(creator_username, brand_raw_id, post_url)
        """))
        conn.execute(text("ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS refferls BOOLEAN NOT NULL DEFAULT false"))
        conn.execute(text("ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS caption TEXT"))
        conn.execute(text("ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS paid_partnership BOOLEAN"))
        conn.execute(text("ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS mentions JSONB"))
        conn.execute(text("ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS tagged_users JSONB"))
        conn.execute(text("ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS coauthor_producers JSONB"))
        conn.execute(text("ALTER TABLE test_creator_brand_partnership_posts ADD COLUMN IF NOT EXISTS sponsorship_confidence INTEGER"))
        conn.commit()


def _get_brand_check_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == BRAND_CHECK_PROMPT_NAME).first()
    if not row:
        return BRAND_CHECK_DEFAULT_PROMPT
    if "has_referral_code" not in (row.content or ""):
        row.content = BRAND_CHECK_DEFAULT_PROMPT
        db.commit()
        logger.info("Content creator RE: refreshed brand_check prompt with referral-code fields")
    return row.content


def _boolish(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "y"}
    return bool(value)


def _parse_brand_check_result(result: dict) -> list[dict]:
    brands = result.get("brands")
    if not isinstance(brands, list):
        return []

    parsed: list[dict] = []
    for brand in brands:
        if isinstance(brand, str):
            handle = normalize_handle(brand)
            if handle:
                parsed.append({
                    "username": handle,
                    "has_referral_code": False,
                    "referral_code": None,
                })
            continue

        if not isinstance(brand, dict):
            continue

        raw_handle = brand.get("username") or brand.get("handle") or brand.get("brand")
        if not isinstance(raw_handle, str):
            continue

        handle = normalize_handle(raw_handle)
        if not handle:
            continue

        referral_code = (
            brand.get("referral_code")
            or brand.get("discount_code")
            or brand.get("creator_code")
        )
        if isinstance(referral_code, str):
            referral_code = referral_code.strip() or None
        else:
            referral_code = None

        parsed.append({
            "username": handle,
            "has_referral_code": _boolish(brand.get("has_referral_code")) or bool(referral_code),
            "referral_code": referral_code,
        })

    return parsed


def _check_post_for_brands(db: Session, username: str, full_name: str | None, item: dict) -> list[dict]:
    """
    Extract mentions/tagged_users/coauthor_producers/paid_partnership from a
    raw post item and ask the LLM which of those referenced accounts, if
    any, are real brand/company accounts. Returns confirmed brand usernames
    — empty if none, or if the post has no such signal to check at all.
    """
    mentions  = item.get("mentions") or []
    tagged    = _usernames_only(item.get("taggedUsers")) or []
    coauthors = _usernames_only(item.get("coauthorProducers")) or []

    if not (mentions or tagged or coauthors) or not OPENAI_KEY:
        return []

    prompt = fill_template(
        _get_brand_check_prompt(db),
        creator_username=username,
        creator_full_name=full_name or "",
        caption=(item.get("caption") or "")[:600],
        paid_partnership=str(bool(item.get("paidPartnership"))).lower(),
        mentions=", ".join(mentions) if mentions else "none",
        tagged_users=", ".join(tagged) if tagged else "none",
        coauthor_producers=", ".join(coauthors) if coauthors else "none",
    )
    result = call_gpt_json(prompt, context=f"brand_check @{username} post {item.get('id')}")
    return _parse_brand_check_result(result) if isinstance(result, dict) else []


def _get_or_create_brand_id(db: Session, brand_username: str) -> int | None:
    """
    Look up an existing brands_raw row by instagram_handle first — this
    covers both properly-seeded brands (name set) and bare rows created by
    this same flow, neither of which the partial unique index alone can
    dedupe against (it only applies to name IS NULL rows). Match against
    the bare username, the full-URL form (what ~99% of seeded brands
    store, e.g. "https://www.instagram.com/nike"), and trailing-slash
    variants, since existing data isn't consistently formatted. Only
    create a new bare row (instagram_handle as a full URL, matching the
    existing convention; name/niche/source left NULL) when no row exists
    for this handle at all.
    """
    existing = db.query(BrandRaw.id).filter(
        or_(
            BrandRaw.instagram_handle == brand_username,
            BrandRaw.instagram_handle.ilike(f"%/{brand_username}"),
            BrandRaw.instagram_handle.ilike(f"%/{brand_username}/"),
        )
    ).first()
    if existing:
        return existing.id

    handle_url = f"https://www.instagram.com/{brand_username}"
    insert_brand(db, {"instagram_handle": handle_url})
    row = db.query(BrandRaw.id).filter(BrandRaw.instagram_handle == handle_url).first()
    return row.id if row else None


def _mark_brand_referral(db: Session, brand_id: int, has_referral_code: bool) -> None:
    if not has_referral_code:
        return
    db.query(BrandRaw).filter(BrandRaw.id == brand_id).update({"refferls": True})
    db.commit()


def _brand_snapshot(db: Session, brand_id: int, brand_username: str) -> tuple[str, str | None]:
    brand = db.query(BrandRaw).filter(BrandRaw.id == brand_id).first()
    if not brand:
        return brand_username, None
    return (
        brand.name or brand_username,
        brand.instagram_handle or f"https://www.instagram.com/{brand_username}",
    )


def _record_llm_partnership_post(
    db: Session,
    *,
    brand_id: int,
    brand_username: str,
    creator_row_id: int,
    creator_username: str,
    creator_name: str | None,
    item: dict,
) -> None:
    post_url = _post_url(item)
    if not post_url:
        logger.warning(
            "Content creator RE: @%s brand @%s LLM-confirmed but post has no URL — evidence row skipped",
            creator_username,
            brand_username,
        )
        return

    brand_name, brand_handle = _brand_snapshot(db, brand_id, brand_username)
    handle = normalize_handle(brand_handle or brand_username)
    stmt = pg_insert(TestCreatorBrandPartnershipPost).values({
        "brand_raw_id": brand_id,
        "brand_name": brand_name,
        "brand_instagram_handle": brand_handle,
        "creator_username": creator_username,
        "creator_name": creator_name or creator_username,
        "content_creator_re_id": creator_row_id,
        "post_id": str(item.get("id") or ""),
        "post_url": post_url,
        "post_timestamp": item.get("timestamp"),
        "llm_partnership": True,
        "caption": item.get("caption"),
        "paid_partnership": item.get("paidPartnership"),
        "mentions": item.get("mentions"),
        "tagged_users": _usernames_only(item.get("taggedUsers")),
        "coauthor_producers": _usernames_only(_real_coauthors(item, handle)),
    })
    stmt = stmt.on_conflict_do_update(
        index_elements=["creator_username", "brand_raw_id", "post_url"],
        set_={
            "brand_name": stmt.excluded.brand_name,
            "brand_instagram_handle": stmt.excluded.brand_instagram_handle,
            "creator_name": stmt.excluded.creator_name,
            "content_creator_re_id": stmt.excluded.content_creator_re_id,
            "post_id": stmt.excluded.post_id,
            "post_timestamp": stmt.excluded.post_timestamp,
            "llm_partnership": True,
            "caption": stmt.excluded.caption,
            "paid_partnership": stmt.excluded.paid_partnership,
            "mentions": stmt.excluded.mentions,
            "tagged_users": stmt.excluded.tagged_users,
            "coauthor_producers": stmt.excluded.coauthor_producers,
            "detected_at": text("now()"),
        },
    )
    db.execute(stmt)
    db.commit()


def enrich_content_creator_re(db: Session, limit: int = 1) -> tuple[int, set[int]]:
    """
    Process up to `limit` content_creator_re rows where is_scraped=False.
    Returns (rows_processed, brand_raw_ids) — the second element is the
    union of every brands_raw.id confirmed/linked across all processed rows
    in this call (new bare rows this discovered, or existing brands a
    confirmed collab post pointed at), for callers that want to chain
    further enrichment onto exactly the brands this call touched rather
    than sweeping the whole brands_raw table.
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping content_creator_re enrichment")
        return 0, set()

    _ensure_partnership_evidence_table()

    rows: list[ContentCreatorRE] = (
        db.query(ContentCreatorRE)
        .filter(ContentCreatorRE.is_scraped == False)
        .limit(limit)
        .all()
    )

    if not rows:
        logger.info("Content creator RE: no pending rows")
        return 0, set()

    logger.info("Content creator RE: processing %d row(s)", len(rows))
    processed = 0
    all_confirmed_brand_ids: set[int] = set()

    for row in rows:
        username = normalize_handle(row.username or "")
        niche = row.niche

        if not username:
            logger.warning("Content creator RE: row id=%d has no username — skipping", row.id)
            row.is_scraped = True
            db.commit()
            processed += 1
            continue

        logger.info("Content creator RE: scraping @%s", username)

        #  Scrape top 5 posts (addParentData gets profile info too)
        raw_posts = _scrape_posts(username, n=40)
        if raw_posts is None:
            logger.warning(
                "Content creator RE: Apify scrape failed for @%s (actor limit reached, "
                "network error, etc.) — leaving is_scraped=False so this row is retried",
                username,
            )
            time.sleep(0.5)
            continue
        if not raw_posts:
            logger.warning("Content creator RE: no posts returned for @%s", username)
            row.is_scraped = True
            db.commit()
            processed += 1
            continue

        # Set once any commenter _scrape_posts() call below fails (actor
        # limit reached, network error, etc.) — gates is_scraped at the end
        # so a failed Apify run isn't silently treated as fully processed.
        scrape_failed = False

        #  Extract profile data + classify demographics via LLM
        profile = _profile_from_posts(raw_posts)
        demo = _classify_demographics(db, username, profile)
        time.sleep(0.3)

        #  Store creator — one row per post, niche from content_creator_re.niche
        creator_rows = [
            _build_post_row(
                username, "contentcreatorRE", profile, demo, item,
                niche=niche, is_content_creator_re=True,
            )
            for item in raw_posts
            if item.get("id")
        ]
        upsert_rows(db, InstagramUser, creator_rows, ["post_id"])

        logger.info(
            "Content creator RE: @%s stored — gender=%s country=%s age=%s niche=%s followers=%s",
            username, demo["gender"], demo["country"], demo["age_group"], niche,
            profile.get("followersCount"),
        )

        #  Check each post's collaboration signals for real brand accounts.
        #  Keep post-specific brand IDs so commenter scraping/linking only
        #  runs for posts where the LLM confirmed a brand partnership.
        confirmed_brand_ids: set[int] = set()
        branded_posts: list[tuple[dict, set[int]]] = []
        for item in raw_posts:
            brand_matches = _check_post_for_brands(db, username, profile.get("fullName"), item)
            if brand_matches:
                time.sleep(0.3)
            post_brand_ids: set[int] = set()
            for brand_match in brand_matches:
                brand_username = brand_match["username"]
                brand_id = _get_or_create_brand_id(db, brand_username)
                if brand_id:
                    _mark_brand_referral(db, brand_id, brand_match.get("has_referral_code", False))
                    _record_llm_partnership_post(
                        db,
                        brand_id=brand_id,
                        brand_username=brand_username,
                        creator_row_id=row.id,
                        creator_username=username,
                        creator_name=profile.get("fullName"),
                        item=item,
                    )
                    post_brand_ids.add(brand_id)
                    confirmed_brand_ids.add(brand_id)
            if post_brand_ids:
                branded_posts.append((item, post_brand_ids))

        for brand_id in confirmed_brand_ids:
            _link_to_brand(db, brand_id, username)

        if confirmed_brand_ids:
            logger.info(
                "Content creator RE: @%s linked to %d brand(s)",
                username, len(confirmed_brand_ids),
            )

        #  Collect unique commenters only from posts with confirmed brands
        #  (up to 5/post). Each record carries the brand IDs confirmed on
        #  that same post so links stay scoped to the evidence source.
        commenter_links: list[tuple[dict, set[int]]] = []
        for item, post_brand_ids in branded_posts:
            for record in _collect_commenter_records([item], n_per_post=5):
                commenter_links.append((record, post_brand_ids))

        commenter_usernames = sorted({record["username"] for record, _ in commenter_links})

        if commenter_usernames:
            existing_c = {
                r.username for r in
                db.query(InstagramUser.username)
                .filter(InstagramUser.username.in_(commenter_usernames))
                .all()
            }
            new_commenters = [u for u in commenter_usernames if u not in existing_c]

            logger.info(
                "Content creator RE: @%s has %d unique commenter(s) (%d new)",
                username, len(commenter_usernames), len(new_commenters),
            )

            #  Scrape 1 post per new commenter for profile data
            for commenter in new_commenters:
                logger.info("Content creator RE:   commenter @%s", commenter)

                c_posts = _scrape_posts(commenter, n=1)
                if c_posts is None:
                    logger.warning(
                        "Content creator RE: Apify scrape failed for commenter @%s — will retry",
                        commenter,
                    )
                    scrape_failed = True
                    time.sleep(0.5)
                    continue
                if not c_posts:
                    time.sleep(0.5)
                    continue

                c_profile = _profile_from_posts(c_posts)
                c_demo = _classify_demographics(db, commenter, c_profile)
                time.sleep(0.3)

                if c_posts[0].get("id"):
                    # No conflict target: this row could violate EITHER the
                    # partial username-where-commenter index OR the global
                    # post_id unique constraint (this commenter's own most
                    # recent post may already be stored under a different
                    # username's creator row) — see upsert_rows' docstring.
                    upsert_rows(db, InstagramUser, [
                        _build_post_row(
                            commenter, "commenter", c_profile, c_demo, c_posts[0],
                            is_content_creator_re=True,
                        )
                    ], None)

                logger.info(
                    "Content creator RE:   commenter @%s stored — gender=%s country=%s",
                    commenter, c_demo["gender"], c_demo["country"],
                )
                time.sleep(1.0)

            #  Link every commenter (existing + new) to every confirmed
            #  brand on the same post, plus creator <-> commenter itself
            #  (instagram_creator_commenters) scoped per confirmed brand.
            for record, post_brand_ids in commenter_links:
                commenter = record.get("username")
                if not commenter:
                    continue
                for brand_id in post_brand_ids:
                    _link_to_brand(db, brand_id, commenter)
                    _link_commenter_to_creator(db, brand_id, username, record)

        all_confirmed_brand_ids |= confirmed_brand_ids

        if scrape_failed:
            db.commit()  # keep whatever rows/links were already stored above
            logger.warning(
                "Content creator RE: @%s — at least one commenter Apify scrape failed — "
                "leaving is_scraped=False so this row is retried instead of being "
                "treated as fully processed",
                username,
            )
            continue

        row.is_scraped = True
        db.commit()
        processed += 1
        logger.info("Content creator RE: @%s done", username)

    logger.info(
        "Content creator RE: %d row(s) processed, %d brand(s) confirmed",
        processed, len(all_confirmed_brand_ids),
    )
    return processed, all_confirmed_brand_ids


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="Run content_creator_re reverse-engineering enrichment.")
    parser.add_argument("--limit", type=int, default=1, help="Pending content_creator_re rows to process this run.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        processed, brand_ids = enrich_content_creator_re(db, limit=args.limit)
        print(
            f"content_creator_re processed={processed}; "
            f"confirmed_brand_ids={sorted(brand_ids)}; "
            "evidence_table=test_creator_brand_partnership_posts"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
