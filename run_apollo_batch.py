"""
run_apollo_batch.py

One-off batch run, two steps per brand, for every brand discovered via
content_creator_re (creators 1-208, sponsorship_confidence >= 90,
refferls=false, 2026 posts, has_official_website=true) — 327 distinct
brands as of the query that produced this list:

  1. run_brand_scoring(db, brand_id=X) — populates/refreshes this brand's
     initial_brand_score row. brand_id mode bypasses ITS OWN batch-mode
     gate too (has_official_website/wikidata_enriched/shopify_checked/
     tranco_checked/meta_ads_fetched/youtube_checked/instagram_checked/
     initial_brand_scored all required in batch mode) — so this runs
     regardless of whether meta_ads (or anything else) has been fetched
     yet, exactly as asked. Makes no external API/LLM calls at all — pure
     DB aggregation from whatever enrichment signals already exist for
     the brand — so this step adds no meaningful extra cost or time.
  2. run_apollo_contacts(db, brand_id=X) — same brand_id bypass on ITS OWN
     gate (initial_brand_score.total_score >= 50 / has_official_website /
     not-already-attempted), so it runs next regardless of what score
     step 1 just produced.

Logs to both stdout AND apollo_batch.log (so progress survives running
this under nohup/background and can still be tailed live).

Cost/scale (measured elsewhere this session, not guaranteed per-brand):
  - run_brand_scoring: free (no external calls), negligible time
  - run_apollo_contacts: up to 5 Apollo credits/brand (email enrich, the
    only credit-costing step) -> up to ~1,635 credits for all 327; plus
    ~$0.017 OpenAI ranking cost/brand -> ~$5.60 total; plus ~60-100s/brand
    (mostly the ranking LLM call) -> likely 5-9 hours running straight
    through

Run in the background with logs:
    nohup python3 run_apollo_batch.py > apollo_batch_output.log 2>&1 &
    tail -f apollo_batch.log

Or in the foreground (blocks this terminal for the full run):
    python3 run_apollo_batch.py
"""

import logging

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from pipeline.db import SessionLocal
from pipeline.enrichment.apollo_contacts import run_apollo_contacts, _ApolloAuthError
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

    processed = 0
    for i, (brand_id, name) in enumerate(brands, start=1):
        logger.info("[%d/%d] brand_id=%s name=%s — starting", i, len(brands), brand_id, name)
        try:
            run_brand_scoring(db, brand_id=brand_id)
        except Exception:
            logger.exception("[%d/%d] brand_id=%s name=%s — scoring failed, continuing to Apollo anyway", i, len(brands), brand_id, name)

        try:
            n = run_apollo_contacts(db, brand_id=brand_id)
            processed += n
            logger.info("[%d/%d] brand_id=%s name=%s — done", i, len(brands), brand_id, name)
        except _ApolloAuthError as exc:
            logger.error("Apollo auth error — aborting entire run: %s", exc)
            break
        except Exception:
            logger.exception("[%d/%d] brand_id=%s name=%s — unexpected error, skipping", i, len(brands), brand_id, name)

    logger.info("DONE. %d/%d brand(s) processed successfully", processed, len(brands))
    db.close()


if __name__ == "__main__":
    main()
