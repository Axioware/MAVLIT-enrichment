"""
run_end_to_end_pipeline.py

End-to-end enrichment pipeline for every existing brands_raw row.

This runner intentionally does NOT run the reverse-engineering-only steps:
  - pipeline.enrichment_re.content_creator_re
  - pipeline.enrichment_re.brand_wikidata_lookup

Instead, it takes the brand IDs already present in brands_raw and runs the
standard enrichment stack in this order:

  1. shopify_detect.py
  2. wikidata_socials.py
  3. tranco.py
  4. youtube_sponsorship.py
  5. meta_ads.py
  6. instagram_posts.py
  7. instagram_users.py
  8. initial_brand_scoring.py
  9. apollo_contacts.py
 10. brand_signals.py

Each step is called without brand_id so the module's normal "already done"
filters are respected. For example, shopify_detect only processes rows where
shopify_checked=False, Instagram users only processes posts where
is_users_scraped=False, and Apollo only processes brands with no contact rows.

Run with:
    python3 run_end_to_end_pipeline.py
"""

import logging

from dotenv import load_dotenv
load_dotenv()

from pipeline.db import BrandProfile, BrandRaw, InitialBrandScore, SessionLocal
from pipeline.enrichment.shopify_detect import enrich_shopify
from pipeline.enrichment.wikidata_socials import enrich_wikidata_socials
from pipeline.enrichment.tranco import enrich_tranco
# Imported as a module (not "from ... import enrich_youtube_sponsorships")
# because drain_youtube_sponsorships() below also reads
# youtube_sponsorship.quota_fully_exhausted, a flag that mutates at runtime
# — a `from...import` of just the function wouldn't see later changes to
# that module attribute.
from pipeline.enrichment import youtube_sponsorship
from pipeline.enrichment.youtube_sponsorship import enrich_youtube_sponsorships
from pipeline.enrichment.meta_ads import enrich_meta_ads
from pipeline.enrichment.instagram_posts import enrich_instagram_posts
from pipeline.enrichment.instagram_users import enrich_instagram_users
from pipeline.enrichment.initial_brand_scoring import run_brand_scoring
from pipeline.enrichment.apollo_contacts import run_apollo_contacts
from pipeline.enrichment.brand_signals import run_brand_signals


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

_BANNER = "=" * 78


def _step_start(label: str) -> None:
    logger.info(_BANNER)
    logger.info("STEP %s — starting", label)
    logger.info(_BANNER)


def _step_end(label: str, summary: str) -> None:
    logger.info("STEP %s — finished: %s", label, summary)
    logger.info(_BANNER)


def load_brand_ids(db) -> list[int]:
    """Return every existing brands_raw.id in stable order."""
    return [row.id for row in db.query(BrandRaw.id).order_by(BrandRaw.id).all()]


def drain_pending_step(label: str, fn, db, batch_limit: int, **kwargs) -> None:
    """Call fn(db, limit=batch_limit, **kwargs) until its normal pending set is empty."""
    _step_start(label)
    total_processed = 0
    batch_num = 0

    while True:
        batch_num += 1
        logger.info("[%s] batch %d — starting, limit=%d", label, batch_num, batch_limit)
        try:
            processed = fn(db, limit=batch_limit, **kwargs)
        except Exception:
            logger.exception("[%s] batch %d — failed, stopping this step", label, batch_num)
            break

        if not processed:
            logger.info("[%s] batch %d — no pending rows", label, batch_num)
            break

        total_processed += processed
        logger.info("[%s] batch %d — processed %d row(s), %d total", label, batch_num, processed, total_processed)

    _step_end(label, f"{total_processed} row(s) processed")


def drain_instagram_users(label: str, db, batch_limit: int = 5) -> None:
    """Drain pending instagram_posts rows using instagram_users.py's normal filter."""
    drain_pending_step(label, enrich_instagram_users, db, batch_limit=batch_limit)


def pending_brand_signal_ids(db, limit: int) -> list[int]:
    """
    brand_signals.py has no *_checked flag. Treat an existing brand_match_profile
    row as "already done" for this end-to-end runner.
    """
    return [
        row.id
        for row in (
            db.query(BrandRaw.id)
            .join(InitialBrandScore, InitialBrandScore.brand_raw_id == BrandRaw.id)
            .outerjoin(BrandProfile, BrandProfile.brand_raw_id == BrandRaw.id)
            .filter(InitialBrandScore.total_score >= 50)
            .filter(BrandProfile.brand_raw_id.is_(None))
            .order_by(BrandRaw.id)
            .limit(limit)
            .all()
        )
    ]


def pending_youtube_ids(db, limit: int) -> list[int]:
    """YouTube is expensive, so be explicit: only unchecked website brands."""
    return [
        row.id
        for row in (
            db.query(BrandRaw.id)
            .filter(
                BrandRaw.name.isnot(None),
                BrandRaw.website.isnot(None),
                BrandRaw.youtube_checked == False,
            )
            .order_by(BrandRaw.id)
            .limit(limit)
            .all()
        )
    ]


def drain_youtube_sponsorships(label: str, db, batch_limit: int = 50) -> None:
    _step_start(label)
    total_processed = 0
    batch_num = 0

    while True:
        batch_num += 1
        brand_ids = pending_youtube_ids(db, batch_limit)
        if not brand_ids:
            logger.info("[%s] batch %d — no unchecked website brands", label, batch_num)
            break

        logger.info("[%s] batch %d — processing %d brand(s): %s", label, batch_num, len(brand_ids), brand_ids)
        for bid in brand_ids:
            try:
                total_processed += enrich_youtube_sponsorships(db, brand_id=bid)
            except Exception:
                logger.exception("[%s] brand_id=%d — failed, continuing", label, bid)

            # Once every YOUTUBE_API_KEY* is exhausted, every remaining brand
            # would fail the exact same way — stop this whole step now and
            # move on, instead of looping through the rest of pending_youtube_ids
            # (each costing ~2 doomed calls to rediscover the same thing).
            if youtube_sponsorship.quota_fully_exhausted:
                logger.warning(
                    "[%s] YouTube quota fully exhausted — stopping this step early and "
                    "moving to the next one. Remaining brands stay youtube_checked=False "
                    "and will be picked up on the next run.",
                    label,
                )
                _step_end(label, f"{total_processed} brand(s) processed (stopped early — quota exhausted)")
                return

    _step_end(label, f"{total_processed} brand(s) processed")


def drain_brand_signals(label: str, db, batch_limit: int = 500) -> None:
    _step_start(label)
    total_processed = 0
    batch_num = 0

    while True:
        batch_num += 1
        brand_ids = pending_brand_signal_ids(db, batch_limit)
        if not brand_ids:
            logger.info("[%s] batch %d — no pending brands without brand_match_profile", label, batch_num)
            break

        logger.info("[%s] batch %d — processing %d brand(s): %s", label, batch_num, len(brand_ids), brand_ids)
        for bid in brand_ids:
            try:
                total_processed += run_brand_signals(db, brand_id=bid)
            except Exception:
                logger.exception("[%s] brand_id=%d — failed, continuing", label, bid)

    _step_end(label, f"{total_processed} brand(s) processed")


def main() -> None:
    db = SessionLocal()
    try:
        brand_ids = load_brand_ids(db)
        if not brand_ids:
            logger.info("No rows found in brands_raw — nothing to run.")
            return

        logger.info("Loaded %d brands_raw row(s): %s", len(brand_ids), brand_ids)

        drain_pending_step("1/10 shopify_detect",        enrich_shopify,              db, batch_limit=300)
        drain_pending_step("2/10 wikidata_socials",      enrich_wikidata_socials,     db, batch_limit=500)
        drain_pending_step("3/10 tranco",                enrich_tranco,               db, batch_limit=500)
        drain_youtube_sponsorships("4/10 youtube_sponsorship", db, batch_limit=50)
        drain_pending_step("5/10 meta_ads",              enrich_meta_ads,             db, batch_limit=200)
        drain_pending_step("6/10 instagram_posts",       enrich_instagram_posts,      db, batch_limit=50)
        drain_instagram_users("7/10 instagram_users", db, batch_limit=5)
        drain_pending_step("8/10 initial_brand_scoring", run_brand_scoring,           db, batch_limit=500)
        drain_pending_step("9/10 apollo_contacts",       run_apollo_contacts,         db, batch_limit=20)
        drain_brand_signals("10/10 brand_signals", db, batch_limit=500)
    finally:
        db.close()

    logger.info(_BANNER)
    logger.info("End-to-end brands_raw pipeline complete.")
    logger.info(_BANNER)


if __name__ == "__main__":
    main()
