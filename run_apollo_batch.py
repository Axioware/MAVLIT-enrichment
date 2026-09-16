"""
run_apollo_batch.py

One-off batch run: populates/refreshes initial_brand_score for every brand
discovered via content_creator_re (creators 1-208, sponsorship_confidence
>= 90, refferls=false, 2026 posts, has_official_website=true) — 327
distinct brands as of the query that produced this list.

run_brand_scoring(db, brand_id=X) bypasses ITS OWN batch-mode gate
(has_official_website/wikidata_enriched/shopify_checked/tranco_checked/
meta_ads_fetched/youtube_checked/instagram_checked/initial_brand_scored
all required in batch mode) — so this scores every brand regardless of
whether meta_ads (or anything else) has been fetched yet. It also
unconditionally sets brands_raw.initial_brand_scored=True on completion
(score_brand(), inside initial_brand_scoring.py) — no extra step needed
for that; it's automatic.

Does NOT run Apollo — that's done separately, manually, afterward against
every brand now sitting in initial_brand_score (see apollo_contacts.py,
whose batch-mode total_score >= 50 gate has been removed so it picks up
all of them regardless of score).

Makes no external API/LLM calls at all — pure DB aggregation from
whatever enrichment signals already exist for each brand — so this is
free and fast; skips any brand already initial_brand_scored=True so it's
safe to re-run.

Or in the foreground:
    python3 run_apollo_batch.py
"""

import logging

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from pipeline.db import SessionLocal, BrandRaw
from pipeline.enrichment.initial_brand_scoring import run_brand_scoring

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler("apollo_batch.log"),
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
"""


def main() -> None:
    db = SessionLocal()
    brands = [(r[0], r[1]) for r in db.execute(text(_QUERY)).all()]
    logger.info("Resolved %d brand(s) to process", len(brands))

    scored = 0
    skipped_done = 0
    for i, (brand_id, name) in enumerate(brands, start=1):
        brand = db.query(BrandRaw).filter(BrandRaw.id == brand_id).first()
        if brand.initial_brand_scored:
            skipped_done += 1
            continue

        logger.info("[%d/%d] brand_id=%s name=%s — scoring", i, len(brands), brand_id, name)
        try:
            run_brand_scoring(db, brand_id=brand_id)
            scored += 1
        except Exception:
            logger.exception("[%d/%d] brand_id=%s name=%s — scoring failed, continuing", i, len(brands), brand_id, name)

    logger.info("DONE. %d brand(s) scored this run, %d already done (skipped)", scored, skipped_done)
    db.close()


if __name__ == "__main__":
    main()
