"""
pipeline/enrichment/wikidata_socials.py

Fetches social media handles for brands that have a Wikidata QID.

Properties fetched per entity:
  P2003 → instagram_handle
  P2002 → twitter_handle
  P2397 → youtube_channel_id
  P4003 → facebook_page
  P7085 → tiktok_handle
  P4264 → linkedin_id

Works in batches of 50 QIDs per SPARQL request.
Updates brands_raw in-place; sets wikidata_enriched=True when done.
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
    Returns {qid: {instagram_handle, twitter_handle, youtube_channel_id,
                   facebook_page, tiktok_handle, linkedin_id}}
    """
    values_block = " ".join(f"wd:{q}" for q in qids)
    sparql = f"""
SELECT ?entity ?ig ?tw ?yt ?fb ?tt ?li WHERE {{
  VALUES ?entity {{ {values_block} }}
  OPTIONAL {{ ?entity wdt:P2003 ?ig . }}
  OPTIONAL {{ ?entity wdt:P2002 ?tw . }}
  OPTIONAL {{ ?entity wdt:P2397 ?yt . }}
  OPTIONAL {{ ?entity wdt:P4003 ?fb . }}
  OPTIONAL {{ ?entity wdt:P7085 ?tt . }}
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
        if "tw" in row:
            v = row["tw"]["value"]
            entry["twitter_handle"]     = f"https://twitter.com/{v}"
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
        if "tt" in row:
            v = row["tt"]["value"]
            entry["tiktok_handle"]      = f"https://www.tiktok.com/@{v}"
        if "li" in row:
            v = row["li"]["value"]
            entry["linkedin_id"]        = f"https://www.linkedin.com/company/{v}"
    return results


def enrich_wikidata_socials(db: Session, limit: int = 500) -> int:
    """
    Find brands_raw rows with wikidata_id and wikidata_enriched=False,
    fetch their social handles, write back, mark wikidata_enriched=True.
    Returns the number of rows updated.
    """
    brands: list[BrandRaw] = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.wikidata_id.isnot(None),
            BrandRaw.wikidata_enriched == False,
        )
        .limit(limit)
        .all()
    )

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
