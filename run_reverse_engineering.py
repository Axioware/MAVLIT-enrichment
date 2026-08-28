"""
run_reverse_engineering.py

End-to-end reverse-engineering pipeline:

  1. Run content_creator_re.py COMPLETELY — drains every pending RE creator
     (content_creator_re.is_scraped=False) and collects the full set of
     brands_raw.id's it discovers/confirms along the way (new bare brand
     rows, or existing brands a confirmed collab post pointed at).

  2. Only once step 1 is fully done, run every other step — in order —
     targeting EXACTLY that discovered brand_id set, one brand at a time,
     via each function's own brand_id (or brand_raw_id) parameter:

       2. score_post_sponsorship.py (per-row, not per-brand — see below;
          scores the creator/brand/post evidence rows step 1 just wrote
          into test_creator_brand_partnership_posts)
       3. brand_wikidata_lookup.py
       4. brand_instagram_profile.py (only picks up what step 3 left bare —
          see its own docstring for the Instagram bio/linktree/Google-search
          resolution order)
       5. wikidata_socials.py
       6. shopify_detect.py
       7. tranco.py
       8. youtube_sponsorship.py
       9. meta_ads.py
      10. instagram_posts.py
      11. score_instagram_post_sponsorship.py  (per-post, not per-brand — see below)
      12. instagram_users.py                   (per-post, not per-brand — see below)
      13. initial_brand_scoring.py

If step 1 discovers zero brands, nothing further runs — there's nothing to
enrich. Each per-brand call bypasses that step's own *_checked filter (the
same brand_id= bypass pattern every enrichment module already supports —
shopify_detect.py and tranco.py didn't have it before this script needed
it, so a brand_id parameter was added to both, matching every other step).
A brand that isn't applicable to a given step (e.g. no website yet, so
shopify_detect has nothing to fetch) just no-ops for that one call.

Steps 2 (score_post_sponsorship), 11 (score_instagram_post_sponsorship) and
12 (instagram_users) are the exception to "one call per brand" — all three
operate on ROWS (test_creator_brand_partnership_posts for step 2,
instagram_posts for steps 11-12), not brands directly, so for each
discovered brand they loop their respective fn(brand_raw_id=...) call
until that brand's rows are fully drained, before moving to the next
brand. Step 11 runs before step 12 so a post's sponsorship_confidence is
on file before instagram_users.py scrapes its referenced accounts'
profiles.

One brand failing at one step is logged and skipped — it does not stop
the rest of that step's brands, or any later step.

Run with:
    python3 run_reverse_engineering.py
"""

import logging

from dotenv import load_dotenv
load_dotenv()

from pipeline.db import BrandRaw, SessionLocal
from pipeline.enrichment_re.content_creator_re import enrich_content_creator_re
from pipeline.enrichment_re.score_post_sponsorship import score_post_sponsorship
from pipeline.enrichment_re.brand_wikidata_lookup import enrich_brand_wikidata_lookup
from pipeline.enrichment_re.brand_instagram_profile import enrich_brand_instagram_profile
from pipeline.enrichment.wikidata_socials import enrich_wikidata_socials
from pipeline.enrichment.shopify_detect import enrich_shopify
from pipeline.enrichment.tranco import enrich_tranco
from pipeline.enrichment.youtube_sponsorship import enrich_youtube_sponsorships
from pipeline.enrichment.meta_ads import enrich_meta_ads
from pipeline.enrichment.instagram_posts import enrich_instagram_posts
from pipeline.enrichment.score_instagram_post_sponsorship import score_instagram_post_sponsorship
from pipeline.enrichment.instagram_users import enrich_instagram_users
from pipeline.enrichment.initial_brand_scoring import run_brand_scoring

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

_BANNER = "=" * 78


def _step_start(label: str) -> None:
    """Loud, easy-to-scroll-to marker for where a new step's logs begin —
    everything logged between this and the matching _step_end() banner
    (including each underlying enrich_*() call's own INFO/WARNING/ERROR
    lines, which already flow through the same root logger) belongs to
    this step."""
    logger.info(_BANNER)
    logger.info("STEP %s — starting", label)
    logger.info(_BANNER)


def _step_end(label: str, summary: str) -> None:
    logger.info("STEP %s — finished: %s", label, summary)
    logger.info(_BANNER)


def run_content_creator_re_fully(db, batch_limit: int = 1) -> set[int]:
    """
    Drains every pending content_creator_re row (is_scraped=False), one
    small batch at a time. Returns the union of every brands_raw.id
    confirmed/discovered across the whole run.
    """
    label = "1/13 content_creator_re"
    _step_start(label)
    all_brand_ids: set[int] = set()
    batch_num = 0
    while True:
        batch_num += 1
        logger.info("[%s] batch %d — calling enrich_content_creator_re(limit=%d)", label, batch_num, batch_limit)
        try:
            processed, brand_ids = enrich_content_creator_re(db, limit=batch_limit)
        except Exception:
            logger.exception("[%s] batch %d failed — stopping this step", label, batch_num)
            break
        if not processed:
            logger.info("[%s] batch %d — nothing pending, step complete", label, batch_num)
            break
        all_brand_ids |= brand_ids
        logger.info(
            "[%s] batch %d — processed %d row(s), brand(s) this batch=%s, %d discovered so far",
            label, batch_num, processed, sorted(brand_ids), len(all_brand_ids),
        )
    _step_end(label, f"{len(all_brand_ids)} brand(s) discovered total: {sorted(all_brand_ids)}")
    return all_brand_ids


def run_score_post_sponsorship_per_brand(label: str, db, brand_ids: set[int], batch_limit: int = 500) -> None:
    """
    score_post_sponsorship.py operates on test_creator_brand_partnership_posts
    ROWS, not brands directly (same shape as the instagram_posts-row helpers
    below) — for each brand this drains every pending row
    (score_post_sponsorship(brand_raw_id=...)) before moving to the next.
    """
    _step_start(label)
    total_rows = 0
    for i, bid in enumerate(sorted(brand_ids), start=1):
        logger.info("[%s] (%d/%d) brand_id=%d — starting", label, i, len(brand_ids), bid)
        brand_rows = 0
        batch_num = 0
        while True:
            batch_num += 1
            try:
                processed = score_post_sponsorship(db, brand_raw_id=bid, limit=batch_limit)
            except Exception:
                logger.exception(
                    "[%s] brand_id=%d batch %d — failed, moving to the next brand",
                    label, bid, batch_num,
                )
                break
            if not processed:
                break
            brand_rows += processed
            total_rows += processed
            logger.info("[%s] brand_id=%d batch %d — %d row(s) processed (%d so far for this brand)",
                        label, bid, batch_num, processed, brand_rows)
        logger.info("[%s] (%d/%d) brand_id=%d — done, %d row(s) total", label, i, len(brand_ids), bid, brand_rows)
    _step_end(label, f"{total_rows} row(s) processed across {len(brand_ids)} brand(s)")


def run_per_brand(label: str, fn, db, brand_ids: set[int], **kwargs) -> None:
    """Call fn(db, brand_id=bid, **kwargs) once for each brand_id — for
    steps where one call fully handles one brand."""
    _step_start(label)
    done = 0
    for i, bid in enumerate(sorted(brand_ids), start=1):
        logger.info("[%s] (%d/%d) brand_id=%d — starting", label, i, len(brand_ids), bid)
        try:
            fn(db, brand_id=bid, **kwargs)
        except Exception:
            logger.exception("[%s] (%d/%d) brand_id=%d — failed, continuing with the next brand", label, i, len(brand_ids), bid)
            continue
        done += 1
        logger.info("[%s] (%d/%d) brand_id=%d — done", label, i, len(brand_ids), bid)
    _step_end(label, f"attempted {done}/{len(brand_ids)} brand(s)")


def _pending_for_scoring(db, brand_ids: set[int]) -> set[int]:
    """
    Mirrors run_brand_scoring's own batch-mode (brand_id=None) prerequisite
    filter exactly — score a brand only once EVERY enrichment step this
    script runs has actually completed for it, not just because it was
    discovered. Needed because brand_id= (what run_per_brand always passes)
    bypasses run_brand_scoring's own prerequisite check by design (it's
    meant to let you force-rescore one brand for testing) — without this,
    every discovered brand gets scored regardless of whether shopify/
    tranco/meta_ads/youtube/instagram actually finished for it.
    """
    return {
        row.id for row in
        db.query(BrandRaw.id).filter(
            BrandRaw.id.in_(brand_ids),
            BrandRaw.has_official_website == True,
            BrandRaw.wikidata_enriched  == True,
            BrandRaw.shopify_checked    == True,
            BrandRaw.tranco_checked     == True,
            BrandRaw.meta_ads_fetched   == True,
            BrandRaw.youtube_checked    == True,
            BrandRaw.instagram_checked  == True,
            BrandRaw.initial_brand_scored == False,
        ).all()
    }


def run_score_instagram_post_sponsorship_per_brand(label: str, db, brand_ids: set[int], batch_limit: int = 50) -> None:
    """
    score_instagram_post_sponsorship.py operates on instagram_posts ROWS,
    not brands directly (same shape as run_instagram_users_per_brand below)
    — for each brand this drains every pending post
    (score_instagram_post_sponsorship(brand_raw_id=...)) before moving to
    the next.
    """
    _step_start(label)
    total_posts = 0
    for i, bid in enumerate(sorted(brand_ids), start=1):
        logger.info("[%s] (%d/%d) brand_id=%d — starting", label, i, len(brand_ids), bid)
        brand_posts = 0
        batch_num = 0
        while True:
            batch_num += 1
            try:
                processed = score_instagram_post_sponsorship(db, brand_raw_id=bid, limit=batch_limit)
            except Exception:
                logger.exception(
                    "[%s] brand_id=%d batch %d — failed, moving to the next brand",
                    label, bid, batch_num,
                )
                break
            if not processed:
                break
            brand_posts += processed
            total_posts += processed
            logger.info("[%s] brand_id=%d batch %d — %d post(s) processed (%d so far for this brand)",
                        label, bid, batch_num, processed, brand_posts)
        logger.info("[%s] (%d/%d) brand_id=%d — done, %d post(s) total", label, i, len(brand_ids), bid, brand_posts)
    _step_end(label, f"{total_posts} post(s) processed across {len(brand_ids)} brand(s)")


def run_instagram_users_per_brand(label: str, db, brand_ids: set[int], batch_limit: int = 5) -> None:
    """
    instagram_users.py operates on instagram_posts ROWS, not brands
    directly, so for each brand this drains every pending post
    (enrich_instagram_users(brand_raw_id=...)) before moving to the next.
    """
    _step_start(label)
    total_posts = 0
    for i, bid in enumerate(sorted(brand_ids), start=1):
        logger.info("[%s] (%d/%d) brand_id=%d — starting", label, i, len(brand_ids), bid)
        brand_posts = 0
        batch_num = 0
        while True:
            batch_num += 1
            try:
                processed = enrich_instagram_users(db, brand_raw_id=bid, limit=batch_limit)
            except Exception:
                logger.exception(
                    "[%s] brand_id=%d batch %d — failed, moving to the next brand",
                    label, bid, batch_num,
                )
                break
            if not processed:
                break
            brand_posts += processed
            total_posts += processed
            logger.info("[%s] brand_id=%d batch %d — %d post(s) processed (%d so far for this brand)",
                        label, bid, batch_num, processed, brand_posts)
        logger.info("[%s] (%d/%d) brand_id=%d — done, %d post(s) total", label, i, len(brand_ids), bid, brand_posts)
    _step_end(label, f"{total_posts} post(s) processed across {len(brand_ids)} brand(s)")


def main() -> None:
    db = SessionLocal()
    try:
        brand_ids = run_content_creator_re_fully(db, batch_limit=1)
        if not brand_ids:
            logger.info("No brands discovered from content_creator_re — nothing further to run.")
            return

        logger.info("Discovered brand_id(s): %s", sorted(brand_ids))

        run_score_post_sponsorship_per_brand("2/13 score_post_sponsorship", db, brand_ids, batch_limit=500)

        run_per_brand("3/13 brand_wikidata_lookup",   enrich_brand_wikidata_lookup, db, brand_ids)
        run_per_brand("4/13 brand_instagram_profile", enrich_brand_instagram_profile, db, brand_ids)
        run_per_brand("5/13 wikidata_socials",        enrich_wikidata_socials,      db, brand_ids)
        run_per_brand("6/13 shopify_detect",          enrich_shopify,               db, brand_ids)
        run_per_brand("7/13 tranco",                  enrich_tranco,                db, brand_ids)
        run_per_brand("8/13 youtube_sponsorship",     enrich_youtube_sponsorships,  db, brand_ids)
        run_per_brand("9/13 meta_ads",                enrich_meta_ads,              db, brand_ids)

        website_brand_ids = {
            row_id
            for (row_id,) in db.query(BrandRaw.id)
            .filter(
                BrandRaw.id.in_(brand_ids),
                BrandRaw.has_official_website.is_(True),
            )
            .all()
        }
        if not website_brand_ids:
            logger.info("No discovered brands with has_official_website=True — skipping instagram_posts.")
        else:
            run_per_brand("10/13 instagram_posts", enrich_instagram_posts, db, website_brand_ids)

        run_score_instagram_post_sponsorship_per_brand("11/13 score_instagram_post_sponsorship", db, brand_ids, batch_limit=50)
        run_instagram_users_per_brand("12/13 instagram_users", db, brand_ids, batch_limit=5)

        pending_score_ids = _pending_for_scoring(db, brand_ids)
        skipped = brand_ids - pending_score_ids
        if skipped:
            logger.info(
                "[13/13 initial_brand_scoring] skipping %d brand(s) not yet fully "
                "enriched (or already scored): %s",
                len(skipped), sorted(skipped),
            )
        run_per_brand("13/13 initial_brand_scoring", run_brand_scoring, db, pending_score_ids)
    finally:
        db.close()

    logger.info(_BANNER)
    logger.info("Reverse engineering pipeline complete.")
    logger.info(_BANNER)


if __name__ == "__main__":
    main()

