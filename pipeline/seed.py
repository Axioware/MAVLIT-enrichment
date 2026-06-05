"""
pipeline/seed.py

Stage 1 of the brand enrichment pipeline.
Populates brands_raw with raw brand names (and official websites) from
Wikipedia + Wikidata (and optionally Google SERP).

Both search_wikipedia_brands() and search_wikidata_brands() now return
  list[dict] with keys: name, website, domain
google_brand_search() still returns list[str].

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
    country: str | None = None,
    headquarters: str | None = None,
    location: str | None = None,
    operating_area: str | None = None,
) -> int:
    """
    Seed brands_raw for any niche keyword.
    Optional geo filters narrow both the Wikidata SPARQL query and the
    Wikipedia category search to a specific geography.
    """
    geo_active = {
        k: v for k, v in {
            "country":        country,
            "headquarters":   headquarters,
            "location":       location,
            "operating_area": operating_area,
        }.items() if v
    }

    collected: list[dict] = []

    def _row(name: str, source: str, source_url: str,
             website: str = "", domain: str = "") -> dict:
        """Build a seed row, merging geo context + website/domain."""
        return {
            "name":       name,
            "niche":      niche,
            "source":     source,
            "source_url": source_url,
            "website":    website,
            "domain":     domain,
            **geo_active,
        }

    # ── Source 1: Wikipedia ───────────────────────────────────────────────────
    logger.info("Scraping Wikipedia for niche '%s'", niche)
    try:
        wiki_records = search_wikipedia_brands(
            niche,
            country=country,
            location=location,
            operating_area=operating_area,
        )
        cat_url = f"https://en.wikipedia.org/wiki/Category:{niche.replace(' ', '_')}_brands"
        for rec in wiki_records:
            collected.append(_row(
                name=rec["name"],
                source="wikipedia",
                source_url=cat_url,
                website=rec.get("website", ""),
                domain=rec.get("domain", ""),
            ))
        logger.info(
            "Wikipedia yielded %d names (%d with website)",
            len(wiki_records),
            sum(1 for r in wiki_records if r.get("website")),
        )
    except Exception:
        logger.exception("Wikipedia scrape failed for niche '%s'", niche)

    # ── Source 2: Wikidata ────────────────────────────────────────────────────
    logger.info("Querying Wikidata for niche '%s'", niche)
    try:
        wikidata_records = search_wikidata_brands(
            niche,
            country=country,
            headquarters=headquarters,
            location=location,
            operating_area=operating_area,
        )
        for rec in wikidata_records:
            collected.append(_row(
                name=rec["name"],
                source="wikidata",
                source_url="https://query.wikidata.org/sparql",
                website=rec.get("website", ""),
                domain=rec.get("domain", ""),
            ))
        logger.info(
            "Wikidata yielded %d names (%d with website)",
            len(wikidata_records),
            sum(1 for r in wikidata_records if r.get("website")),
        )
    except Exception:
        logger.exception("Wikidata query failed for niche '%s'", niche)

    # ── Source 3: Google SERP (optional) ─────────────────────────────────────
    # google_brand_search still returns list[str]; no website/domain available.
    if use_google:
        query = f"top {niche} brands"
        logger.info("Google SERP: %s", query)
        try:
            serp_names = google_brand_search(query)
            for name in serp_names:
                collected.append(_row(
                    name=name,
                    source="google",
                    source_url=f"https://www.google.com/search?q={query}",
                ))
            logger.info("Google SERP yielded %d names", len(serp_names))
        except Exception:
            logger.exception("Google SERP failed for query '%s'", query)

    logger.info("Total raw names collected: %d", len(collected))

    # ── Normalise & deduplicate ───────────────────────────────────────────────
    for row in collected:
        row["normalized"] = normalize(row["name"])
    collected = [r for r in collected if r["normalized"]]

    deduped_names = set(deduplicate([r["normalized"] for r in collected]))

    seen: set[str] = set()
    deduped_rows: list[dict] = []
    for row in collected:
        norm = row["normalized"]
        if norm in deduped_names and norm not in seen:
            seen.add(norm)
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