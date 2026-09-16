"""
run_apollo_batch.py

One-off batch run: finds Apollo contacts for every brand discovered via
content_creator_re (creators 1-208, sponsorship_confidence >= 90,
refferls=false, 2026 posts, has_official_website=true) — 327 distinct
brands as of the query that produced this list. Bypasses
run_apollo_contacts()'s batch-mode initial_brand_score gate by looping
brand_id= over the resolved brand list one at a time, same as its own
docstring describes for testing a single brand.

Logs to both stdout AND apollo_batch.log (so progress survives running
this under nohup/background and can still be tailed live).

Cost/scale (measured elsewhere this session, not guaranteed per-brand):
  - up to 5 Apollo credits/brand (email enrich, the only credit-costing
    step) -> up to ~1,635 credits for all 327
  - ~$0.017 OpenAI ranking cost/brand -> ~$5.60 total
  - ~60-100s/brand (mostly the ranking LLM call) -> likely 5-9 hours
    running straight through

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
