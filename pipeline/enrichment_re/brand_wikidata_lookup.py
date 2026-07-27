"""
pipeline/enrichment_re/brand_wikidata_lookup.py

Reverse lookup for bare brand rows: brands_raw rows created with only
instagram_handle set (name IS NULL) — e.g. from content_creator_re's
brand_check flow (pipeline/enrichment_re/content_creator_re.py) — have no
name, description, website, etc. This queries Wikidata by instagram_handle
(P2003) to find the matching entity, if one exists, and backfills
whatever pipeline/seed.py would normally have set at seed time:
  name, wikidata_id, entity_type, description, website, domain.

niche is intentionally left untouched — Wikidata doesn't expose a
per-entity "niche" property this platform reads; niche is a search
*input* at seed time (pipeline/sources/wikidata.py), not something
readable back off a single entity.

Once wikidata_id is set here, the existing enrich_wikidata_socials()
step (pipeline/enrichment/wikidata_socials.py) picks the brand up
naturally on its next run (it only requires wikidata_id present and
wikidata_enriched=False) to backfill remaining social handles — no
duplicated logic needed here.

Marks instagram_wikidata_checked=True whether or not a match was found,
so unresolvable handles aren't retried every run.
"""

import logging
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from pipeline.db import BrandRaw
from pipeline.helpers.normalize import normalize
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_SPARQL_URL = "https://query.wikidata.org/sparql"
_HEADERS = {
    "Accept": "application/sparql-results+json",
    "User-Agent": "MAVLIT-enrichment/1.0 (https://github.com/axioware/MAVLIT-enrichment)",
}
_BATCH_SIZE  = 50
_RETRY_WAIT  = 65


def _extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.lower()
    except Exception:
        return ""


def _fetch_by_instagram_handles(handles: list[str]) -> dict[str, dict]:
    """
    Reverse SPARQL lookup: given bare Instagram usernames, find the
    Wikidata entity (if any) whose P2003 value matches, plus name/website/
    description/entity_type. Returns {handle: {...}} — handles with no
    match are simply absent from the result.
    """
    values_block = " ".join(f'"{h}"' for h in handles)
    sparql = f"""
SELECT ?ig ?entity ?entityLabel ?entityDescription ?website ?instanceOfLabel WHERE {{
  VALUES ?ig {{ {values_block} }}
  ?entity wdt:P2003 ?ig .
  OPTIONAL {{ ?entity wdt:P856 ?website . }}
  OPTIONAL {{ ?entity wdt:P31 ?instanceOf . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
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
                logger.warning("Brand Wikidata lookup 429 — waiting %ds", wait)
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
        handle = row["ig"]["value"]
        entry = results.setdefault(handle, {})
        if "entity" in row and not entry.get("wikidata_id"):
            entry["wikidata_id"] = row["entity"]["value"].rsplit("/", 1)[-1]
        if "entityLabel" in row and not entry.get("name"):
            label = row["entityLabel"]["value"]
            # Wikidata falls back to the bare QID as the label when no
            # English label exists — don't treat that as a real name.
            if not (label.startswith("Q") and label[1:].isdigit()):
                entry["name"] = label
        if "entityDescription" in row and not entry.get("description"):
            entry["description"] = row["entityDescription"]["value"]
        if "website" in row and not entry.get("website"):
            entry["website"] = row["website"]["value"]
        if "instanceOfLabel" in row and not entry.get("entity_type"):
            entry["entity_type"] = row["instanceOfLabel"]["value"]
    return results


def _apply_match(db: Session, brand: BrandRaw, data: dict, handle: str) -> None:
    """Apply a confirmed Wikidata match to one bare brand row and commit."""
    name = data["name"]
    website = data.get("website") or None
    brand.name              = name
    brand.name_normalized   = normalize(name)
    brand.wikidata_id       = data.get("wikidata_id")
    brand.entity_type       = data.get("entity_type") or None
    brand.description       = data.get("description") or None
    brand.website           = website
    brand.domain            = _extract_domain(website) or None
    brand.has_official_website = bool(website)
    brand.website_source    = "wikidata" if website else None
    brand.source            = "wikidata"
    brand.source_confidence = 100
    brand.instagram_wikidata_checked = True

    try:
        db.commit()
        logger.info(
            "Brand Wikidata lookup: @%s → '%s' (%s)",
            handle, name, data.get("wikidata_id"),
        )
    except IntegrityError:
        # name_normalized collided with an existing brand (e.g. a properly
        # seeded row for the same real-world brand already exists) — leave
        # this row bare rather than crash the batch, but still mark checked.
        db.rollback()
        brand.instagram_wikidata_checked = True
        db.commit()
        logger.warning(
            "Brand Wikidata lookup: @%s → '%s' collided with an existing brand — left unresolved",
            handle, name,
        )


def enrich_brand_wikidata_lookup(db: Session, limit: int = 50, brand_id: int | None = None) -> int:
    """
    For brands_raw rows with name IS NULL and instagram_handle set,
    reverse-lookup Wikidata by their Instagram handle and backfill name,
    wikidata_id, entity_type, description, website, domain. niche is left
    untouched.

    Pass brand_id to target one specific brand directly — bypasses the
    instagram_wikidata_checked filter.

    Returns number of brand rows processed (matched or not).
    """
    query = db.query(BrandRaw).filter(
        BrandRaw.name.is_(None),
        BrandRaw.instagram_handle.isnot(None),
    )
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    else:
        query = query.filter(BrandRaw.instagram_wikidata_checked == False)

    brands: list[BrandRaw] = query.limit(limit).all()

    if not brands:
        logger.info("Brand Wikidata lookup: no pending bare brands")
        return 0

    logger.info("Brand Wikidata lookup: processing %d brand(s)", len(brands))

    # bare username -> [brand rows sharing that handle] (rare, but be safe)
    handle_map: dict[str, list[BrandRaw]] = {}
    for brand in brands:
        bare = normalize_handle(brand.instagram_handle)
        if bare:
            handle_map.setdefault(bare, []).append(brand)

    processed = 0
    handles = list(handle_map.keys())

    for i in range(0, len(handles), _BATCH_SIZE):
        batch = handles[i : i + _BATCH_SIZE]
        try:
            found = _fetch_by_instagram_handles(batch)
        except Exception:
            logger.exception("Brand Wikidata lookup batch %d failed — marking skipped", i)
            for h in batch:
                for brand in handle_map[h]:
                    brand.instagram_wikidata_checked = True
                    processed += 1
            db.commit()
            continue

        for h in batch:
            data = found.get(h)
            for brand in handle_map[h]:
                if data and data.get("name"):
                    _apply_match(db, brand, data, h)
                else:
                    logger.info("Brand Wikidata lookup: @%s → no match", h)
                    brand.instagram_wikidata_checked = True
                    db.commit()
                processed += 1

        if i + _BATCH_SIZE < len(handles):
            time.sleep(1)

    logger.info("Brand Wikidata lookup: %d brand(s) processed", processed)
    return processed
