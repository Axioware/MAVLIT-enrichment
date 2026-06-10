"""
pipeline/enrichment/orchestrator.py

Orchestrates all signal enrichment steps in order:
  1. Wikidata socials   — social handles for brands with QID
  2. Google fallback    — website discovery + website_source stamping
  3. Shopify detection  — is_shopify flag for brands with website
  4. Tranco lookup      — in_tranco_list / tranco_rank for brands with domain
  5. Meta Ads           — store raw ad data from Meta Ad Library

Each step is independent; a failure in one doesn't abort the others.
"""

import logging

from sqlalchemy.orm import Session

from .google_fallback import enrich_google_fallback
from .meta_ads import enrich_meta_ads
from .shopify_detect import enrich_shopify
from .tranco import enrich_tranco
from .wikidata_socials import enrich_wikidata_socials

logger = logging.getLogger(__name__)


def run_signal_enrichment(
    db: Session,
    *,
    niche: str | None = None,
    limit_per_step: int = 300,
    steps: list[str] | None = None,
) -> dict[str, int]:
    """
    Run all (or a subset of) signal enrichment steps.

    `steps` can be a list of step names to run selectively:
      ['wikidata_socials', 'google_fallback', 'shopify', 'tranco', 'meta_ads']
    If None, all steps run.

    `niche` is accepted for future per-niche filtering but not applied yet
    (the individual step functions query globally for pending rows).

    Returns a dict mapping step name → rows updated.
    """
    all_steps = ["wikidata_socials", "google_fallback", "shopify", "tranco", "meta_ads"]
    run = set(steps) if steps else set(all_steps)

    results: dict[str, int] = {}

    if "wikidata_socials" in run:
        try:
            results["wikidata_socials"] = enrich_wikidata_socials(db, limit=limit_per_step)
        except Exception:
            logger.exception("wikidata_socials step failed")
            results["wikidata_socials"] = -1

    if "google_fallback" in run:
        try:
            results["google_fallback"] = enrich_google_fallback(db, limit=limit_per_step)
        except Exception:
            logger.exception("google_fallback step failed")
            results["google_fallback"] = -1

    if "shopify" in run:
        try:
            results["shopify"] = enrich_shopify(db, limit=limit_per_step)
        except Exception:
            logger.exception("shopify step failed")
            results["shopify"] = -1

    if "tranco" in run:
        try:
            results["tranco"] = enrich_tranco(db, limit=limit_per_step)
        except Exception:
            logger.exception("tranco step failed")
            results["tranco"] = -1

    if "meta_ads" in run:
        try:
            results["meta_ads"] = enrich_meta_ads(db, limit=limit_per_step)
        except Exception:
            logger.exception("meta_ads step failed")
            results["meta_ads"] = -1

    logger.info("Signal enrichment complete: %s", results)
    return results
