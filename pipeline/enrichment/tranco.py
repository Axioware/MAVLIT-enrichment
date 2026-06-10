"""
pipeline/enrichment/tranco.py

Looks up brand domains in the Tranco top-1M list.
https://tranco-list.eu/

The list is downloaded once per process run and cached in memory.
Sets in_tranco_list=True/False and tranco_rank=<int|None>.
Sets tranco_checked=True for each processed brand.

Only runs for brands that have a domain set.
"""

import io
import logging
import zipfile
from functools import lru_cache

import httpx
from sqlalchemy.orm import Session

from pipeline.db import BrandRaw

logger = logging.getLogger(__name__)

_TRANCO_ZIP_URL = "https://tranco-list.eu/top-1m.csv.zip"
_TIMEOUT        = 120


@lru_cache(maxsize=1)
def _load_tranco() -> dict[str, int]:
    """Download and parse the Tranco top-1M list. Cached after first call."""
    logger.info("Downloading Tranco top-1M list…")
    resp = httpx.get(_TRANCO_ZIP_URL, timeout=_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_name = next(n for n in zf.namelist() if n.endswith(".csv"))
        with zf.open(csv_name) as f:
            domain_rank: dict[str, int] = {}
            for line in f:
                parts = line.decode().strip().split(",", 1)
                if len(parts) == 2:
                    rank_str, domain = parts
                    domain_rank[domain.lower().strip()] = int(rank_str)
    logger.info("Tranco list loaded: %d entries", len(domain_rank))
    return domain_rank


def _lookup(domain: str, tranco: dict[str, int]) -> tuple[bool, int | None]:
    """Return (in_list, rank). Tries bare domain and www-prefixed variant."""
    domain = domain.lower().strip()
    if domain in tranco:
        return True, tranco[domain]
    www = "www." + domain
    if www in tranco:
        return True, tranco[www]
    return False, None


def enrich_tranco(db: Session, limit: int = 500) -> int:
    """
    Process brands with domain set and tranco_checked=False.
    Returns number of rows updated.
    """
    brands: list[BrandRaw] = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.domain.isnot(None),
            BrandRaw.tranco_checked == False,
        )
        .limit(limit)
        .all()
    )

    if not brands:
        logger.info("Tranco: no pending brands with domain")
        return 0

    try:
        tranco = _load_tranco()
    except Exception:
        logger.exception("Failed to load Tranco list — skipping Tranco enrichment")
        return 0

    logger.info("Tranco: checking %d brands", len(brands))
    updated = 0

    for brand in brands:
        in_list, rank = _lookup(brand.domain, tranco)
        brand.in_tranco_list = in_list
        brand.tranco_rank    = rank
        brand.tranco_checked = True
        updated += 1

    db.commit()
    logger.info(
        "Tranco: %d done, %d in top-1M",
        updated, sum(1 for b in brands if b.in_tranco_list),
    )
    return updated
