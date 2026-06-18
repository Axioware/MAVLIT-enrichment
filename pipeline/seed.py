"""
pipeline/seed.py

Stage 1 of the brand enrichment pipeline.
Populates brands_raw with raw brand names (and official websites) from
Wikipedia + Wikidata (and optionally Google SERP).

Deduplication priority: wikidata_id > domain > normalized name (fuzzy).
Source confidence: wikidata=100, wikipedia=80, google=50.

Usage:
    python main.py --niche footwear
    python main.py --niche "electric vehicles" --google
"""

import logging
from itertools import zip_longest
from typing import Optional

from sqlalchemy.orm import Session

from pipeline.db import SOURCE_CONFIDENCE, insert_brands_batch
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

    def _row(
        name: str,
        source: str,
        source_url: str,
        website: str = "",
        domain: str = "",
        wikidata_id: str = "",
        entity_type: str = "",
        description: str = "",
        wikipedia_url: str = "",
    ) -> dict:
        """Build a seed row, merging geo context and entity metadata."""
        return {
            "name":             name,
            "niche":            niche,
            "source":           source,
            "source_url":       source_url,
            "source_confidence": SOURCE_CONFIDENCE.get(source, 50),
            "website":          website,
            "domain":           domain,
            "wikidata_id":      wikidata_id or None,
            "entity_type":      entity_type or None,
            "description":      description or None,
            "wikipedia_url":    wikipedia_url or None,
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
                wikidata_id=rec.get("wikidata_id", ""),
                description=rec.get("description", ""),
                wikipedia_url=rec.get("wikipedia_url", ""),
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
                wikidata_id=rec.get("wikidata_id", ""),
                entity_type=rec.get("entity_type", ""),
                description=rec.get("description", ""),
                wikipedia_url=rec.get("wikipedia_url", ""),
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

    # ── Normalise ────────────────────────────────────────────────────────────
    for row in collected:
        row["normalized"] = normalize(row["name"])
    collected = [r for r in collected if r["normalized"]]

    # ── Deduplicate — priority: wikidata_id > fuzzy name ─────────────────────
    # Pass 1: group by wikidata_id; keep the highest-confidence source record.
    qid_best: dict[str, dict] = {}
    no_qid_rows: list[dict] = []
    for row in collected:
        qid = row.get("wikidata_id")
        if qid:
            existing = qid_best.get(qid)
            if not existing or row["source_confidence"] > existing["source_confidence"]:
                qid_best[qid] = row
        else:
            no_qid_rows.append(row)

    # Pass 2: fuzzy name dedup on the remaining no-QID rows.
    deduped_names = set(deduplicate([r["normalized"] for r in no_qid_rows]))
    seen_norms: set[str] = set()
    final_no_qid: list[dict] = []
    for row in no_qid_rows:
        norm = row["normalized"]
        if norm in deduped_names and norm not in seen_norms:
            seen_norms.add(norm)
            final_no_qid.append(row)

    deduped_rows = list(qid_best.values()) + final_no_qid
    logger.info(
        "After deduplication: %d unique entities (%d with Wikidata QID, %d without)",
        len(deduped_rows), len(qid_best), len(final_no_qid),
    )

    # Interleave by source so every source contributes proportionally
    # when a limit is applied (prevents Wikipedia filling all limit slots).
    deduped_rows = _interleave_sources(deduped_rows)

    # Only seed brands that have a website — brands without one cannot be enriched
    before_filter = len(deduped_rows)
    deduped_rows = [r for r in deduped_rows if r.get("website")]
    logger.info(
        "Website filter: %d → %d rows (%d dropped, no website)",
        before_filter, len(deduped_rows), before_filter - len(deduped_rows),
    )

    if limit is not None:
        deduped_rows = deduped_rows[:limit]

    inserted = insert_brands_batch(db, deduped_rows)
    logger.info("Inserted %d new rows for niche '%s'", inserted, niche)
    return inserted