"""
run_youtube_batch.py

Finds YouTube sponsorship signals for the same brand list as
run_apollo_batch.py — every brand discovered via content_creator_re
(creators 1-208, sponsorship_confidence >= 90, refferls=false, 2026
posts, has_official_website=true) — with one extra requirement:
name IS NOT NULL, since enrich_youtube_sponsorships() requires a name
unconditionally, even in brand_id mode (its whole methodology embeds the
brand name in every search query — a bare brand has nothing to search
with).

Quota, not money, is the hard constraint here: 1,400 YouTube Data API
units per brand, rotating across all 13 configured keys (YOUTUBE_API_KEY +
_1.._12) = ~130,000 units/day = ~92 brands/day MAX even with every key at
full quota. 327 brands realistically needs several daily runs. Follows
the exact pattern run_end_to_end_pipeline.py already uses for this
(drain_youtube_sponsorships/pending_youtube_ids): after every brand,
checks pipeline.enrichment.youtube_sponsorship.quota_fully_exhausted and
stops immediately once every key is exhausted today, rather than looping
through the rest of the list for ~13 doomed calls each.

Safe to re-run as many times as needed (e.g. once a day until done) — it
re-checks youtube_checked itself before each brand_id= call (that mode
bypasses the filter enrich_youtube_sponsorships() would otherwise apply),
so an already-done brand is never re-processed and quota is never wasted
on it twice.

Logs to both stdout AND youtube_batch.log.

Run:
    nohup python3 run_youtube_batch.py > youtube_batch_output.log 2>&1 &
    tail -f youtube_batch.log
"""

import logging

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from pipeline.db import SessionLocal, BrandRaw
# Imported as a module (not "from ... import enrich_youtube_sponsorships")
# because quota_fully_exhausted mutates at runtime — a plain `from`-import
# of the name would freeze the value at import time and never see updates.
from pipeline.enrichment import youtube_sponsorship

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler("youtube_batch.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

_QUERY = """
    SELECT DISTINCT tcbp.brand_raw_id, br.name
    FROM content_creator_re ccr
    JOIN test_creator_brand_partnership_posts tcbp ON tcbp.content_creator_re_id = ccr.id
    JOIN brands_raw br ON br.id = tcbp.brand_raw_id
    WHERE ccr.id BETWEEN 1 AND 208
      AND tcbp.sponsorship_confidence >= 90
      AND br.refferls = false
      AND tcbp.post_timestamp >= '2026-01-01'
      AND tcbp.post_timestamp < '2027-01-01'
      AND br.has_official_website = true
      AND br.name IS NOT NULL
"""


def main() -> None:
    db = SessionLocal()
    brands = [(r[0], r[1]) for r in db.execute(text(_QUERY)).all()]
    logger.info("Resolved %d brand(s) to process", len(brands))

    processed = 0
    skipped_done = 0
    for i, (brand_id, name) in enumerate(brands, start=1):
        brand = db.query(BrandRaw).filter(BrandRaw.id == brand_id).first()
        if brand.youtube_checked:
            skipped_done += 1
            continue

        logger.info("[%d/%d] brand_id=%s name=%s — starting", i, len(brands), brand_id, name)
        try:
            processed += youtube_sponsorship.enrich_youtube_sponsorships(db, brand_id=brand_id)
        except Exception:
            logger.exception("[%d/%d] brand_id=%s name=%s — unexpected error, continuing", i, len(brands), brand_id, name)
            continue

        if youtube_sponsorship.quota_fully_exhausted:
            logger.warning(
                "YouTube quota fully exhausted for today — stopping this run. "
                "Re-run this same script tomorrow (after quotas reset) to continue; "
                "already-checked brands are skipped automatically."
            )
            break

    remaining = len(brands) - processed - skipped_done
    logger.info(
        "DONE. %d brand(s) processed this run, %d already done (skipped), %d still remaining",
        processed, skipped_done, remaining,
    )
    db.close()


if __name__ == "__main__":
    main()
