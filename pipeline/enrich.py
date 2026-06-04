"""
pipeline/enrich.py

Stage 2 of the brand enrichment pipeline.
Reads brands_raw WHERE enriched = false AND enrichment_failed = false,
enriches each via Apollo (primary) → Hunter (fallback), writes to brands,
then sets brands_raw.enriched = true.

Runs nightly at 2:00am via scheduler.py.
Usage (manual): import and call run_enrich(db, batch_size=50)
"""

import logging
from typing import Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from config import APOLLO_API_KEY, ENRICH_BATCH_SIZE, HUNTER_API_KEY
from pipeline.db import Brand, BrandRaw

logger = logging.getLogger(__name__)

_ACCEPTED_MATCH_STATUSES = {"perfect_match", "partial_match"}


def _enrich_via_apollo(brand_name: str) -> Optional[dict]:
    try:
        resp = httpx.post(
            "https://api.apollo.io/v1/organizations/enrich",
            headers={"X-Api-Key": APOLLO_API_KEY},
            json={"name": brand_name},
            timeout=10,
        )
        resp.raise_for_status()
        org = resp.json().get("organization")
        if not org:
            return None
        if org.get("match_status") not in _ACCEPTED_MATCH_STATUSES:
            logger.debug("Apollo low-confidence match for '%s' (status=%s)", brand_name, org.get("match_status"))
            return None
        domain = org.get("primary_domain")
        if not domain:
            return None
        return {
            "domain": domain,
            "industry": org.get("industry"),
            "employee_count": org.get("estimated_num_employees"),
            "linkedin_url": org.get("linkedin_url"),
            "hq_country": org.get("country"),
            "email_pattern": None,
            "enrichment_source": "apollo",
        }
    except Exception:
        logger.exception("Apollo request failed for '%s'", brand_name)
        return None


def _enrich_via_hunter(brand_name: str) -> Optional[dict]:
    try:
        resp = httpx.get(
            "https://api.hunter.io/v2/domain-search",
            params={"company": brand_name, "api_key": HUNTER_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        domain = data.get("domain")
        if not domain:
            return None
        return {
            "domain": domain,
            "industry": None,
            "employee_count": None,
            "linkedin_url": None,
            "hq_country": data.get("country"),
            "email_pattern": data.get("pattern"),
            "enrichment_source": "hunter",
        }
    except Exception:
        logger.exception("Hunter request failed for '%s'", brand_name)
        return None


def _upsert_brand(db: Session, raw: BrandRaw, data: dict) -> None:
    values = {
        "raw_id": raw.id,
        "name": raw.name,
        "domain": data["domain"],
        "industry": data.get("industry"),
        "employee_count": data.get("employee_count"),
        "linkedin_url": data.get("linkedin_url"),
        "hq_country": data.get("hq_country"),
        "email_pattern": data.get("email_pattern"),
        "enrichment_source": data.get("enrichment_source"),
    }
    update_set = {k: v for k, v in values.items() if k not in ("raw_id", "domain")}
    stmt = (
        insert(Brand)
        .values(**values)
        .on_conflict_do_update(index_elements=["domain"], set_=update_set)
    )
    db.execute(stmt)


def run_enrich(db: Session, batch_size: int = ENRICH_BATCH_SIZE) -> int:
    """
    Enrich one batch of brands_raw rows.
    Returns the count of successfully enriched brands.
    """
    rows = (
        db.execute(
            select(BrandRaw)
            .where(BrandRaw.enriched == False)  # noqa: E712
            .where(BrandRaw.enrichment_failed == False)  # noqa: E712
            .limit(batch_size)
        )
        .scalars()
        .all()
    )
    logger.info("Enrich batch: %d brands to process", len(rows))

    enriched_count = 0
    for raw in rows:
        data = _enrich_via_apollo(raw.name) or _enrich_via_hunter(raw.name)

        if data:
            _upsert_brand(db, raw, data)
            db.execute(
                update(BrandRaw).where(BrandRaw.id == raw.id).values(enriched=True)
            )
            enriched_count += 1
            logger.debug("Enriched '%s' → %s (via %s)", raw.name, data["domain"], data["enrichment_source"])
        else:
            db.execute(
                update(BrandRaw).where(BrandRaw.id == raw.id).values(enrichment_failed=True)
            )
            logger.warning("Both APIs failed for '%s' — marked enrichment_failed", raw.name)

        db.commit()

    logger.info("Enrich done — %d/%d enriched", enriched_count, len(rows))
    return enriched_count
