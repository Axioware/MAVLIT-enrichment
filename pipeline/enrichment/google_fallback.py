"""
pipeline/enrichment/google_fallback.py

For brands without an official website, uses Google Custom Search API
to discover one.  Only fires when GOOGLE_API_KEY and GOOGLE_CX are set.

For each brand without a website:
  - Search: "{brand name} official website"
  - Take the first non-Wikipedia, non-social-media result URL
  - Store in google_discovered_website
  - Set website = that URL, domain = extracted domain, website_source = 'google'
  - Set has_official_website = True

For brands that already have a website (from seed), sets
  has_official_website = True, website_source = 'wikidata'
without calling the API.

For brands with no website after the Google attempt, sets
  has_official_website = False, website_source = 'none'
"""

import logging
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from config import GOOGLE_API_KEY, GOOGLE_CX
from pipeline.db import BrandRaw

logger = logging.getLogger(__name__)

_SEARCH_URL   = "https://www.googleapis.com/customsearch/v1"
_SKIP_DOMAINS = frozenset([
    "wikipedia.org", "wikimedia.org", "wikidata.org",
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "youtube.com", "linkedin.com",
    "crunchbase.com", "bloomberg.com", "reuters.com",
])


def _extract_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.lower()
    except Exception:
        return ""


def _google_search(query: str) -> str | None:
    """Return the first acceptable URL from Google Custom Search, or None."""
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return None
    try:
        resp = httpx.get(
            _SEARCH_URL,
            params={"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 5},
            timeout=15,
        )
        if resp.status_code == 429:
            logger.warning("Google CSE 429 — backing off 60s")
            time.sleep(60)
            return None
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            url    = item.get("link", "")
            domain = _extract_domain(url)
            if not any(domain.endswith(skip) for skip in _SKIP_DOMAINS):
                return url
    except Exception:
        logger.exception("Google CSE search failed for query: %s", query)
    return None


def enrich_google_fallback(db: Session, limit: int = 200) -> int:
    """
    1. For brands that already have a website: stamp has_official_website=True,
       website_source='wikidata' (no API call).
    2. For brands without a website: call Google CSE, set discovered website.
    3. Brands with no result after step 2: has_official_website=False,
       website_source='none'.

    Returns total rows updated.
    """
    updated = 0

    # Pass A — brands with website, website_source not yet set
    stamped: list[BrandRaw] = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.website.isnot(None),
            BrandRaw.website_source.is_(None),
        )
        .all()
    )
    for brand in stamped:
        brand.has_official_website = True
        brand.website_source       = "wikidata"
        updated += 1
    if stamped:
        db.commit()
    logger.info("Google fallback: stamped %d existing-website brands as wikidata", len(stamped))

    # Pass B — brands without website
    pending: list[BrandRaw] = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.website.is_(None),
            BrandRaw.website_source.is_(None),
        )
        .limit(limit)
        .all()
    )

    if not pending:
        logger.info("Google fallback: no brands without website to process")
        return updated

    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logger.warning(
            "Google fallback: GOOGLE_API_KEY/GOOGLE_CX not set — marking %d brands as 'none'",
            len(pending),
        )
        for brand in pending:
            brand.has_official_website = False
            brand.website_source       = "none"
            updated += 1
        db.commit()
        return updated

    logger.info("Google fallback: searching for websites for %d brands", len(pending))
    for brand in pending:
        query = f"{brand.name} official website"
        url   = _google_search(query)
        if url:
            domain = _extract_domain(url)
            brand.google_discovered_website = url
            brand.website                   = url
            brand.domain                    = domain or brand.domain
            brand.has_official_website      = True
            brand.website_source            = "google"
            logger.debug("Google found website for %s: %s", brand.name, url)
        else:
            brand.has_official_website = False
            brand.website_source       = "none"
        updated += 1
        db.commit()
        time.sleep(0.5)  # stay within CSE quota

    logger.info("Google fallback: processed %d brands", len(pending))
    return updated
