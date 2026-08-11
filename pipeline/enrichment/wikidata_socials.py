"""
pipeline/enrichment/wikidata_socials.py

Fetches social media handles for brands that have a Wikidata QID.

Properties fetched per entity:
  P2003 → instagram_handle
  P2397 → youtube_channel_id
  P4003 → facebook_page
  P4264 → linkedin_id

Works in batches of 50 QIDs per SPARQL request.
Updates brands_raw in-place; sets wikidata_enriched=True when done.

if social handle is already present in brands_raw, it will not be overwritten.
"""

import logging
import time

import httpx
from sqlalchemy.orm import Session

from pipeline.db import BrandRaw

logger = logging.getLogger(__name__)

_SPARQL_URL = "https://query.wikidata.org/sparql"
_HEADERS = {
    "Accept": "application/sparql-results+json",
    "User-Agent": "MAVLIT-enrichment/1.0 (https://github.com/axioware/MAVLIT-enrichment)",
}
_BATCH_SIZE = 50
_RETRY_WAIT  = 65   # seconds to wait on 429


def _fetch_socials_batch(qids: list[str]) -> dict[str, dict]:
    """
    SPARQL query for a batch of Wikidata QIDs.
    Returns {qid: {instagram_handle, youtube_channel_id,
                   facebook_page, linkedin_id}}
    """
    values_block = " ".join(f"wd:{q}" for q in qids)
    sparql = f"""
SELECT ?entity ?ig ?yt ?fb ?li WHERE {{
  VALUES ?entity {{ {values_block} }}
  OPTIONAL {{ ?entity wdt:P2003 ?ig . }}
  OPTIONAL {{ ?entity wdt:P2397 ?yt . }}
  OPTIONAL {{ ?entity wdt:P4003 ?fb . }}
  OPTIONAL {{ ?entity wdt:P4264 ?li . }}
}}
"""
    for attempt in range(3):
        try:
            resp = httpx.get(
                _SPARQL_URL,
                params={"query": sparql, "format": "json"},
                headers=_HEADERS,
                timeout=30,
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", _RETRY_WAIT))
                logger.warning("Wikidata socials 429 — waiting %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        except httpx.HTTPStatusError:
            if attempt == 2:
                raise
            time.sleep(10 * (attempt + 1))
    else:
        return {}

    results: dict[str, dict] = {}
    for row in resp.json().get("results", {}).get("bindings", []):
        qid = row["entity"]["value"].rsplit("/", 1)[-1]
        entry = results.setdefault(qid, {})
        if "ig" in row:
            v = row["ig"]["value"]
            entry["instagram_handle"]   = f"https://www.instagram.com/{v}"
        if "yt" in row:
            v = row["yt"]["value"]
            # P2397 stores either a channel ID (UC...) or a handle (@...)
            if v.startswith("UC"):
                entry["youtube_channel_id"] = f"https://www.youtube.com/channel/{v}"
            else:
                entry["youtube_channel_id"] = f"https://www.youtube.com/@{v}"
        if "fb" in row:
            v = row["fb"]["value"]
            entry["facebook_page"]      = f"https://www.facebook.com/{v}"
        if "li" in row:
            v = row["li"]["value"]
            entry["linkedin_id"]        = f"https://www.linkedin.com/company/{v}"
    return results


def enrich_wikidata_socials(db: Session, limit: int = 500, brand_id: int | None = None) -> int:
    """
    Find brands_raw rows with wikidata_id, has_official_website=True, and
    wikidata_enriched=False, fetch their social handles, write back, mark
    wikidata_enriched=True. Returns the number of rows updated.

    Pass brand_id to target one specific brand directly — this bypasses the
    has_official_website/wikidata_enriched filters (so you can re-run/test a
    brand that was already processed), but wikidata_id must still be set.
    """
    query = db.query(BrandRaw).filter(
        BrandRaw.wikidata_id.isnot(None),
        BrandRaw.has_official_website.is_(True),
    )
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    else:
        query = query.filter(BrandRaw.wikidata_enriched == False)

    brands: list[BrandRaw] = query.limit(limit).all()

    if not brands:
        logger.info("Wikidata socials: no pending brands")
        return 0

    logger.info("Wikidata socials: enriching %d brands", len(brands))
    updated = 0

    for i in range(0, len(brands), _BATCH_SIZE):
        batch = brands[i : i + _BATCH_SIZE]
        qids  = [b.wikidata_id for b in batch]

        try:
            socials = _fetch_socials_batch(qids)
        except Exception:
            logger.exception("Wikidata socials batch %d failed — marking skipped", i)
            for brand in batch:
                brand.wikidata_enriched = True
            db.commit()
            continue

        for brand in batch:
            data = socials.get(brand.wikidata_id, {})
            for field, value in data.items():
                if value and not getattr(brand, field):
                    setattr(brand, field, value)
            brand.wikidata_enriched = True
            updated += 1

        db.commit()
        logger.info(
            "Wikidata socials: batch %d–%d done (%d had data)",
            i, i + len(batch), sum(1 for b in batch if b.wikidata_id in socials),
        )
        if i + _BATCH_SIZE < len(brands):
            time.sleep(1)

    logger.info("Wikidata socials: updated %d brands", updated)
    return updated
