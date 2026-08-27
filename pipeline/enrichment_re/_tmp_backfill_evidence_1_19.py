"""
TEMPORARY — run once, then delete this file.

Backfills test_creator_brand_partnership_posts for content_creator_re rows
id 1-19, which were originally scraped before this evidence table existed.
Their brands are already in brands_raw, so this just re-scrapes each
creator's posts and re-runs the brand_check LLM per post to (re)confirm
those brands and upsert the evidence row (including sponsorship_confidence).

Deliberately skips: creator/demographics storage in instagram_users (already
stored from the original run) and ALL commenter collection/scraping/linking
(step 4 of enrich_content_creator_re) — not needed for this backfill.

Does not touch is_scraped — queries by id range directly instead of the
is_scraped flag, so nothing needs resetting first.
"""

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import time

from dotenv import load_dotenv
load_dotenv()

from config import APIFY_TOKEN
from pipeline.db import SessionLocal, ContentCreatorRE
from pipeline.enrichment.instagram_users import _profile_from_posts, _scrape_posts
from pipeline.enrichment_re.content_creator_re import (
    _check_post_for_brands,
    _ensure_partnership_evidence_table,
    _get_or_create_brand_id,
    _mark_brand_referral,
    _record_llm_partnership_post,
)
from pipeline.helpers.social import normalize_handle

if not APIFY_TOKEN:
    raise SystemExit("APIFY_TOKEN not set")

db = SessionLocal()
_ensure_partnership_evidence_table()

rows = (
    db.query(ContentCreatorRE)
    .filter(ContentCreatorRE.id.between(1, 19))
    .order_by(ContentCreatorRE.id)
    .all()
)

for row in rows:
    username = normalize_handle(row.username or "")
    if not username:
        print(f"id={row.id}: no username — skipping")
        continue

    print(f"--- id={row.id} @{username} ---")
    raw_posts = _scrape_posts(username, n=40)
    if raw_posts is None:
        print(f"  Apify scrape failed for @{username} — skipping")
        continue
    if not raw_posts:
        print(f"  no posts returned for @{username}")
        continue

    profile = _profile_from_posts(raw_posts)
    confirmed = 0

    for item in raw_posts:
        brand_matches = _check_post_for_brands(db, username, profile.get("fullName"), item)
        if brand_matches:
            time.sleep(0.3)
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
                    sponsorship_confidence=brand_match.get("confidence_pct"),
                )
                confirmed += 1

    print(f"  @{username}: {confirmed} brand/post evidence row(s) upserted")
    time.sleep(1.0)

db.close()
print("done")
