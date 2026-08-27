"""
TEMPORARY — run once on the server, then delete this file.

For every brand already in test_creator_brand_partnership_posts whose
instagram_checked=True but that has ZERO rows in instagram_posts (the brand
scrape ran and found no post worth keeping, so nothing — not even a
follower count — ever got saved), scrape just 1 post via Apify (cheap: only
need the embedded profile snapshot, not real post content) and save a
profile-only row via instagram_posts.py's _build_profile_only_row — same
mechanism enrich_instagram_posts now uses automatically going forward.

Safe to re-run — the partial unique index on (brand_raw_id) WHERE post_id
IS NULL means a brand that already got its profile-only row is skipped on
the next INSERT (upsert_rows does ON CONFLICT DO NOTHING).
"""

import logging
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from config import APIFY_TOKEN
from pipeline.db import BrandRaw, SessionLocal, InstagramPost, TestCreatorBrandPartnershipPost
from pipeline.enrichment.instagram_posts import _build_profile_only_row
from pipeline.helpers.apify import run_apify_actor
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.social import normalize_handle

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_ACTOR_ID = "shu8hvrXbJbY3Eb9W"

if not APIFY_TOKEN:
    raise SystemExit("APIFY_TOKEN not set")

db = SessionLocal()

brands = (
    db.query(BrandRaw)
    .join(TestCreatorBrandPartnershipPost, TestCreatorBrandPartnershipPost.brand_raw_id == BrandRaw.id)
    .filter(BrandRaw.instagram_checked.is_(True))
    .filter(~db.query(InstagramPost.id).filter(InstagramPost.brand_raw_id == BrandRaw.id).exists())
    .distinct()
    .all()
)

print(f"{len(brands)} brand(s) to backfill")

saved = 0
failed = 0

for brand in brands:
    handle = normalize_handle(brand.instagram_handle or "")
    if not handle:
        print(f"id={brand.id} {brand.name}: no instagram_handle — skipping")
        continue

    print(f"--- id={brand.id} {brand.name or '(no name)'} @{handle} ---")
    items = run_apify_actor(
        _ACTOR_ID,
        {
            "addParentData": True,
            "directUrls":    [f"https://www.instagram.com/{handle}/"],
            "resultsLimit":  1,
            "resultsType":   "posts",
        },
        label=f"Profile-only backfill @{handle}",
        require_success=True,
    )

    if items is None:
        print(f"  Apify scrape failed for @{handle} — skipping (retry later)")
        failed += 1
        time.sleep(1.0)
        continue

    row = _build_profile_only_row(brand.id, handle, items)
    n = upsert_rows(db, InstagramPost, [row], ["brand_raw_id"], index_where=text("post_id IS NULL"))
    saved += n
    print(f"  followers={row.get('followers_count')} — {'saved' if n else 'already existed'}")
    time.sleep(1.0)

db.close()
print(f"done: saved={saved} failed={failed} total={len(brands)}")
