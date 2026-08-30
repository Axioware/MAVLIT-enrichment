"""
test_active_sponsorships.py

Test/temporary script — builds one scratch table (also viewable in /admin,
see TestBrandsWithInstagramPosts in pipeline/db.py and its ModelView in
api/app.py): test_brands_with_instagram_posts.

Lists every brand that:
  - has at least one row in instagram_posts, AND
  - in brands_raw has wikidata_enriched, shopify_checked, tranco_checked,
    youtube_checked, and instagram_checked all = True.

Plus, per brand, a post count and an "active sponsoring creator" count —
a creator (instagram_users, user_type != 'commenter', so a real
coauthor/tagged/mentioned/contentcreatorRE account, not just a commenter)
linked to the brand via brand_instagram_users, whose source post (matched
by post_id) carries a real sponsorship signal (paid_partnership=True, or
sponsors/coauthor_producers present), and who has a real profile snapshot
(followers_count not null).

Run with:
    python3 test_active_sponsorships.py
"""

import logging

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pipeline.db import (
    Base, BrandInstagramUser, BrandRaw, InstagramPost, InstagramUser,
    SessionLocal, TestBrandsWithInstagramPosts, engine,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)


def _brands_with_instagram_posts(db):
    """Brands with all 5 checks True that also have at least one instagram_posts row."""
    return (
        db.query(BrandRaw)
        .join(InstagramPost, InstagramPost.brand_raw_id == BrandRaw.id)
        .filter(
            BrandRaw.wikidata_enriched == True,
            BrandRaw.shopify_checked   == True,
            BrandRaw.tranco_checked    == True,
            BrandRaw.youtube_checked   == True,
            BrandRaw.instagram_checked == True,
        )
        .distinct()
        .all()
    )


def _active_sponsoring_creator_count(db, brand_id: int) -> int:
    """Creators (not commenters) linked to this brand whose source post has a real sponsorship signal."""
    return (
        db.query(InstagramUser)
        .join(BrandInstagramUser, BrandInstagramUser.instagram_user_id == InstagramUser.id)
        .join(InstagramPost, InstagramPost.post_id == InstagramUser.post_id)
        .filter(
            BrandInstagramUser.brand_raw_id == brand_id,
            InstagramUser.user_type != "commenter",
            InstagramUser.followers_count.isnot(None),
            (
                (InstagramPost.paid_partnership.is_(True))
                | (InstagramPost.sponsors.isnot(None))
                | (InstagramPost.coauthor_producers.isnot(None))
            ),
        )
        .count()
    )


def build_table() -> int:
    db = SessionLocal()
    total_rows = 0
    try:
        brands = _brands_with_instagram_posts(db)
        logger.info("Eligible brands (5 checks True, present in instagram_posts): %d", len(brands))

        if not brands:
            logger.info("Nothing to insert into test_brands_with_instagram_posts.")
            return 0

        rows_to_insert = []
        for brand in brands:
            post_count = db.query(InstagramPost).filter(InstagramPost.brand_raw_id == brand.id).count()
            active_creator_count = _active_sponsoring_creator_count(db, brand.id)
            rows_to_insert.append({
                "brand_raw_id":           brand.id,
                "brand_name":             brand.name,
                "brand_niche":            brand.niche,
                "brand_website":          brand.website,
                "brand_instagram_handle": brand.instagram_handle,
                "instagram_post_count":   post_count,
                "active_creator_count":   active_creator_count,
            })

        stmt = pg_insert(TestBrandsWithInstagramPosts).values(rows_to_insert)
        stmt = stmt.on_conflict_do_update(
            index_elements=["brand_raw_id"],
            set_={
                "instagram_post_count": stmt.excluded.instagram_post_count,
                "active_creator_count": stmt.excluded.active_creator_count,
                "generated_at":         text("now()"),
            },
        )
        result = db.execute(stmt)
        db.commit()
        total_rows = result.rowcount

        logger.info("Upserted %d row(s) into test_brands_with_instagram_posts", total_rows)
        return total_rows
    finally:
        db.close()


if __name__ == "__main__":
    # ORM-mapped (pipeline/db.py) so this is a harmless no-op once it
    # already exists — same create_all() the main app runs on startup,
    # kept here too so this script also works standalone.
    Base.metadata.create_all(bind=engine, tables=[TestBrandsWithInstagramPosts.__table__])

    n = build_table()
    print(f"\ntest_brands_with_instagram_posts: {n} row(s) upserted this run.")
    print("Query it directly, e.g.:\n  SELECT brand_name, instagram_post_count, active_creator_count FROM test_brands_with_instagram_posts ORDER BY instagram_post_count DESC;")
