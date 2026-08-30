"""
pipeline/seed.py

Stage 1 of the brand enrichment pipeline.
Populates brands_raw with raw brand names (and official websites) from
Wikidata.

Wikipedia (pipeline/sources/wikipedia.py) was removed from this pipeline —
its country/location/operating_area filters were silently no-ops (the
function swallowed them via **_kwargs without ever applying them), so a
frontend country selection only ever actually filtered Wikidata results.
The module file itself is kept on disk but no longer imported/called here.

Deduplication priority: wikidata_id > domain > normalized name (fuzzy).
Source confidence: wikidata=100.

Usage:
    python main.py --niche footwear
    python main.py --niche "electric vehicles"
"""

import logging
from itertools import zip_longest
from typing import Optional

from sqlalchemy.orm import Session

from pipeline.db import SOURCE_CONFIDENCE, insert_brands_batch
from pipeline.helpers.normalize import deduplicate, normalize
from pipeline.sources.wikidata import search_wikidata_brands

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
    limit: Optional[int] = None,
    country: str | None = None,
    headquarters: str | None = None,
    location: str | None = None,
    operating_area: str | None = None,
    niche_label: str | None = None,
) -> int:
    """
    Seed brands_raw for any niche keyword.
    Optional geo filters narrow the Wikidata SPARQL query to a specific
    geography.

    `niche` still drives the search query (Wikidata SPARQL) exactly as
    typed. Pass niche_label to store a DIFFERENT value in brands_raw.niche
    for every row this run inserts — e.g. search for "k-pop groups" but
    file every result under the fixed label "Music". Falls back to `niche`
    itself when not given.
    """
    stored_niche = niche_label or niche
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
            "niche":            stored_niche,
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

    #  Source: Wikidata
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

    logger.info("Total raw names collected: %d", len(collected))

    #  Normalise 
    for row in collected:
        row["normalized"] = normalize(row["name"])
    collected = [r for r in collected if r["normalized"]]

    #  Deduplicate — priority: wikidata_id > fuzzy name 
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

    # Interleave by source so every source contributes proportionally when a
    # limit is applied — a no-op today with only one source (wikidata), kept
    # so it resumes working automatically if another source is added back.
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
    logger.info("Inserted %d new rows for niche '%s' (stored as '%s')", inserted, niche, stored_niche)
    return inserted