"""
pipeline/enrichment_re/content_creator_re.py

Reverse-engineering flow: content_creator_re is a manually-seeded list
(username, niche, url) with no brand/post behind it. For each row where
is_scraped=False:

  1. Scrape the creator's top 5 posts via Apify and classify demographics
     via Mistral — same _scrape_posts/_profile_from_posts/_classify_demographics
     functions used by the main creator flow in instagram_users.py. niche is
     NOT LLM-classified here — it's copied straight from content_creator_re.niche.
  2. Store one row per post in instagram_users (user_type="contentcreatorRE"),
     with is_content_creator_re=True.
  3. For each of those 5 posts, extract mentions/tagged_users/coauthor_producers/
     paid_partnership and ask Mistral (brand_check prompt) which referenced
     accounts, if any, are real brand/company accounts — not other creators.
     Any confirmed brand gets a bare brands_raw row (instagram_handle only;
     name/niche/source are nullable for exactly this case — see pipeline/db.py)
     and is linked to this creator via brand_instagram_users. One creator_re
     row can end up linked to several different brands across its 5 posts.
  4. Collect up to 5 commenters per post (same _collect_commenter_records
     function), scrape 1 post each for profile data, store them in
     instagram_users (user_type="commenter") with is_content_creator_re=True,
     and link every commenter to every brand confirmed in step 3
     (brand_instagram_users) as well as to the creator itself
     (instagram_creator_commenters, scoped per confirmed brand).
  5. Mark content_creator_re.is_scraped=True.

Prompts (editable via /admin > Prompts):
  brand_check — this module only
"""

import logging
import time

from sqlalchemy import or_
from sqlalchemy.orm import Session

from config import APIFY_TOKEN, MISTRAL_API_KEY
from pipeline.db import BrandRaw, ContentCreatorRE, InstagramUser, Prompt, insert_brand
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.llm import call_mistral_json, fill_template
from pipeline.helpers.prompts import BRAND_CHECK_PROMPT_NAME, BRAND_CHECK_DEFAULT_PROMPT
from pipeline.helpers.social import normalize_handle
from pipeline.enrichment.instagram_posts import _usernames_only
from pipeline.enrichment.instagram_users import (
    _COMMENTER_USERNAME_INDEX_WHERE,
    _build_post_row,
    _classify_demographics,
    _collect_commenter_records,
    _link_commenter_to_creator,
    _link_to_brand,
    _profile_from_posts,
    _scrape_posts,
)

logger = logging.getLogger(__name__)


def _get_brand_check_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == BRAND_CHECK_PROMPT_NAME).first()
    return row.content if row else BRAND_CHECK_DEFAULT_PROMPT


def _check_post_for_brands(db: Session, username: str, full_name: str | None, item: dict) -> list[str]:
    """
    Extract mentions/tagged_users/coauthor_producers/paid_partnership from a
    raw post item and ask Mistral which of those referenced accounts, if
    any, are real brand/company accounts. Returns confirmed brand usernames
    — empty if none, or if the post has no such signal to check at all.
    """
    mentions  = item.get("mentions") or []
    tagged    = _usernames_only(item.get("taggedUsers")) or []
    coauthors = _usernames_only(item.get("coauthorProducers")) or []

    if not (mentions or tagged or coauthors) or not MISTRAL_API_KEY:
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
    result = call_mistral_json(prompt, context=f"brand_check @{username} post {item.get('id')}")
    if not isinstance(result, dict):
        return []
    brands = result.get("brands")
    if not isinstance(brands, list):
        return []
    return [b.strip() for b in brands if isinstance(b, str) and b.strip()]


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


def enrich_content_creator_re(db: Session, limit: int = 1) -> int:
    """
    Process up to `limit` content_creator_re rows where is_scraped=False.
    Returns number of rows processed.
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping content_creator_re enrichment")
        return 0

    rows: list[ContentCreatorRE] = (
        db.query(ContentCreatorRE)
        .filter(ContentCreatorRE.is_scraped == False)
        .limit(limit)
        .all()
    )

    if not rows:
        logger.info("Content creator RE: no pending rows")
        return 0

    logger.info("Content creator RE: processing %d row(s)", len(rows))
    processed = 0

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
        if not raw_posts:
            logger.warning("Content creator RE: no posts returned for @%s", username)
            row.is_scraped = True
            db.commit()
            processed += 1
            continue

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

        #  Check each post's collaboration signals for real brand accounts
        confirmed_brand_ids: set[int] = set()
        for item in raw_posts:
            brand_usernames = _check_post_for_brands(db, username, profile.get("fullName"), item)
            if brand_usernames:
                time.sleep(0.3)
            for brand_username in brand_usernames:
                brand_id = _get_or_create_brand_id(db, brand_username)
                if brand_id:
                    confirmed_brand_ids.add(brand_id)

        for brand_id in confirmed_brand_ids:
            _link_to_brand(db, brand_id, username)

        if confirmed_brand_ids:
            logger.info(
                "Content creator RE: @%s linked to %d brand(s)",
                username, len(confirmed_brand_ids),
            )

        #  Collect unique commenters from the 5 posts (up to 5/post)
        commenter_records = _collect_commenter_records(raw_posts, n_per_post=5)
        commenter_usernames = sorted({record["username"] for record in commenter_records})

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
                if not c_posts:
                    time.sleep(0.5)
                    continue

                c_profile = _profile_from_posts(c_posts)
                c_demo = _classify_demographics(db, commenter, c_profile)
                time.sleep(0.3)

                if c_posts[0].get("id"):
                    upsert_rows(db, InstagramUser, [
                        _build_post_row(
                            commenter, "commenter", c_profile, c_demo, c_posts[0],
                            is_content_creator_re=True,
                        )
                    ], ["username"], index_where=_COMMENTER_USERNAME_INDEX_WHERE)

                logger.info(
                    "Content creator RE:   commenter @%s stored — gender=%s country=%s",
                    commenter, c_demo["gender"], c_demo["country"],
                )
                time.sleep(1.0)

            #  Link every commenter (existing + new) to every confirmed
            #  brand, plus creator <-> commenter itself (instagram_creator_commenters)
            for brand_id in confirmed_brand_ids:
                for commenter in commenter_usernames:
                    _link_to_brand(db, brand_id, commenter)
                for record in commenter_records:
                    _link_commenter_to_creator(db, brand_id, username, record)

        row.is_scraped = True
        db.commit()
        processed += 1
        logger.info("Content creator RE: @%s done", username)

    logger.info("Content creator RE: %d row(s) processed", processed)
    return processed
