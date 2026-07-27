"""
pipeline/enrichment/orchestrator.py

Orchestrates all signal enrichment steps in order:
  1. wikidata_socials    — social handles for brands with QID
  2. shopify             — is_shopify / is_woocommerce + social URLs from homepage
  3. tranco              — in_tranco_list / tranco_rank for brands with domain
  4. meta_ads            — store raw ad data from Meta Ad Library
  5. youtube             — detect brands being sponsored in YouTube videos

Each step is independent; a failure in one doesn't abort the others.
"""

import logging

from sqlalchemy.orm import Session

from .meta_ads import enrich_meta_ads
from .shopify_detect import enrich_shopify
from .tranco import enrich_tranco
from .wikidata_socials import enrich_wikidata_socials
from .youtube_sponsorship import enrich_youtube_sponsorships

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

    Valid step names:
      wikidata_socials, shopify, tranco, meta_ads, youtube

    If `steps` is None, all steps run.
    Returns a dict mapping step name → rows updated.
    """
    all_steps = [
        "wikidata_socials", "shopify",
        "tranco", "meta_ads", "youtube",
    ]
    run = set(steps) if steps else set(all_steps)
    results: dict[str, int] = {}

    if "wikidata_socials" in run:
        try:
            results["wikidata_socials"] = enrich_wikidata_socials(db, limit=limit_per_step)
        except Exception:
            logger.exception("wikidata_socials step failed")
            results["wikidata_socials"] = -1

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

    if "youtube" in run:
        try:
            results["youtube"] = enrich_youtube_sponsorships(db, limit=limit_per_step)
        except Exception:
            logger.exception("youtube step failed")
            results["youtube"] = -1

    logger.info("Signal enrichment complete: %s", results)
    return results
