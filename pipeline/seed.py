"""
pipeline/seed.py

Stage 1 of the brand enrichment pipeline.
Populates brands_raw with raw brand names from Wikipedia + Wikidata
(and optionally Google SERP). Accepts any niche keyword.

Usage:
    python main.py --niche footwear
    python main.py --niche "electric vehicles" --google
"""

import logging
from itertools import zip_longest
from typing import Optional

from sqlalchemy.orm import Session

from pipeline.db import insert_brands_batch
from pipeline.normalize import deduplicate, normalize
from pipeline.sources.google_serp import google_brand_search
from pipeline.sources.wikidata import search_wikidata_brands
from pipeline.sources.wikipedia import search_wikipedia_brands

logger = logging.getLogger(__name__)


def _interleave_sources(rows: list[dict]) -> list[dict]:
    """
    Round-robin rows by source so that no single source monopolises the
    results when a limit is applied.

    e.g. [wiki_1, wiki_2, ..., wiki_60, wd_1, ..., wd_30]
    becomes [wiki_1, wd_1, wiki_2, wd_2, ..., wiki_30, wd_30, wiki_31, ...]
    """
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row["source"], []).append(row)

    _sentinel = object()
    result: list[dict] = []
    for group in zip_longest(*buckets.values(), fillvalue=_sentinel):
        result.extend(item for item in group if item is not _sentinel)
    return result


def run_seed(
    niche: str,
    db: Session,
    use_google: bool = False,
    limit: Optional[int] = None,
) -> int:
    """
    Seed brands_raw for any niche keyword.

    Steps:
      1. Fetch brand names from Wikidata SPARQL.
      2. Optionally augment with Google SERP results.
      3. Normalize each name (lowercase, strip legal suffixes).
      4. Fuzzy-deduplicate within the batch (threshold=88).
      5. Bulk-insert with ON CONFLICT DO NOTHING (idempotent).

    Returns:
        Count of newly inserted rows.
    """
    collected: list[dict] = []

    # Source 1: Wikipedia
    logger.info("Scraping Wikipedia for niche '%s'", niche)
    try:
        wiki_names = search_wikipedia_brands(niche)
        for name in wiki_names:
            collected.append({
                "name": name,
                "niche": niche,
                "source": "wikipedia",
                "source_url": f"https://en.wikipedia.org/wiki/Category:{niche.replace(' ', '_')}_brands",
            })
        logger.info("Wikipedia yielded %d names", len(wiki_names))
    except Exception:
        logger.exception("Wikipedia scrape failed for niche '%s'", niche)

    # Source 2: Wikidata
    logger.info("Querying Wikidata for niche '%s'", niche)
    try:
        wikidata_names = search_wikidata_brands(niche)
        for name in wikidata_names:
            collected.append({
                "name": name,
                "niche": niche,
                "source": "wikidata",
                "source_url": "https://query.wikidata.org/sparql",
            })
        logger.info("Wikidata yielded %d names", len(wikidata_names))
    except Exception:
        logger.exception("Wikidata query failed for niche '%s'", niche)

    # Source 3: Google SERP (optional — slow, may be blocked)
    if use_google:
        query = f"top {niche} brands"
        logger.info("Google SERP: %s", query)
        try:
            serp_names = google_brand_search(query)
            for name in serp_names:
                collected.append({
                    "name": name,
                    "niche": niche,
                    "source": "google",
                    "source_url": f"https://www.google.com/search?q={query}",
                })
            logger.info("Google SERP yielded %d names", len(serp_names))
        except Exception:
            logger.exception("Google SERP failed for query '%s'", query)

    logger.info("Total raw names collected: %d", len(collected))

    for row in collected:
        row["normalized"] = normalize(row["name"])
    collected = [r for r in collected if r["normalized"]]

    normalized_names = [r["normalized"] for r in collected]
    deduped_names = set(deduplicate(normalized_names))

    seen: set[str] = set()
    deduped_rows: list[dict] = []
    for row in collected:
        if row["normalized"] in deduped_names and row["normalized"] not in seen:
            seen.add(row["normalized"])
            deduped_rows.append(row)

    logger.info("After deduplication: %d unique names", len(deduped_rows))

    # Interleave by source so every source contributes proportionally
    # when a limit is applied (prevents Wikipedia filling all limit slots).
    deduped_rows = _interleave_sources(deduped_rows)

    if limit is not None:
        deduped_rows = deduped_rows[:limit]

    inserted = insert_brands_batch(db, deduped_rows)
    logger.info("Inserted %d new rows for niche '%s'", inserted, niche)
    return inserted
