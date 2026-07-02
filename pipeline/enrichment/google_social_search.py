"""
pipeline/enrichment/google_social_search.py

Uses Google Custom Search API to find missing social media profile URLs for brands.

Runs AFTER wikidata_socials.py and shopify_detect.py so it only fills in
handles those two steps couldn't find.

For each brand with at least one NULL social handle, it runs a targeted
site-restricted Google search per missing platform:

  instagram_handle   → site:instagram.com {brand}
  tiktok_handle      → site:tiktok.com {brand}
  youtube_channel_id → site:youtube.com {brand}
  facebook_page      → site:facebook.com {brand}
  twitter_handle     → site:twitter.com {brand}
  linkedin_id        → site:linkedin.com/company {brand}

Each brand costs up to 6 quota units (one per missing platform).
Google Custom Search free tier: 100 queries/day → ~16 brands/day.

Sets google_social_checked=True after processing each brand.
Requires GOOGLE_API_KEY and GOOGLE_CX in .env.

Changes:
  - Domain-anchored search: when brand.domain is available, the query is
    "{brand} {domain} site:platform.com" instead of just "{brand} site:platform.com".
    This reduces false positives for generic brand names (e.g. "Dove", "Axe", "Coast")
    because Google anchors results to pages that mention the brand's own website.
    If the domain-anchored search returns no valid profile, falls back to the
    name-only query automatically (so brands without a known domain still work).
"""

import logging
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from config import GOOGLE_API_KEY, GOOGLE_CX
from pipeline.db import BrandRaw
from pipeline.helpers.social import normalize_social_url

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"
_TIMEOUT    = 10

# Maps DB field name → (site restriction, URL validator)
_PLATFORMS: list[tuple[str, str, str]] = [
    # (db_field,          site_query,                    expected_domain)
    ("instagram_handle",   "site:instagram.com",          "instagram.com"),
    ("tiktok_handle",      "site:tiktok.com",             "tiktok.com"),
    ("youtube_channel_id", "site:youtube.com",            "youtube.com"),
    ("facebook_page",      "site:facebook.com",           "facebook.com"),
    ("twitter_handle",     "site:twitter.com OR site:x.com", "twitter.com"),
    ("linkedin_id",        "site:linkedin.com/company",   "linkedin.com"),
]

# Paths that indicate a non-profile page — skip these results
_SKIP_PATH_PREFIXES = frozenset([
    "/share", "/sharer", "/intent", "/login", "/signup",
    "/help", "/about", "/policies", "/ads", "/explore",
    "/hashtag", "/search", "/p/", "/reel/", "/watch",
    "/shorts/", "/results", "/playlist",
])


def _is_valid_profile_url(url: str, expected_domain: str) -> bool:
    """Return True if the URL looks like a real social media profile page."""
    try:
        p = urlparse(url)
        netloc = p.netloc.lower().lstrip("www.")
        if expected_domain not in netloc:
            return False
        path = p.path.rstrip("/")
        if not path or path == "/":
            return False
        # Must have at least one path segment that looks like a handle
        parts = [seg for seg in path.split("/") if seg]
        if not parts:
            return False
        first = parts[0].lower()
        if any(first.startswith(skip.lstrip("/")) for skip in _SKIP_PATH_PREFIXES):
            return False
        # Reject paths that look like posts/videos not profiles
        if len(parts) > 2 and expected_domain in ("instagram.com", "tiktok.com"):
            return False
        return True
    except Exception:
        return False


class _APIUnavailable(Exception):
    """Raised when the Custom Search API key is invalid or the API is not enabled.
    Aborts the entire run so brands are not wrongly marked as checked."""


def _google_search(query: str) -> list[str]:
    """
    Run one Google Custom Search query and return up to 3 result URLs.
    Raises _APIUnavailable on 403 (API not enabled / key invalid) so the
    caller can abort the run without marking brands as checked.
    Returns [] on quota exhaustion or other transient errors.
    """
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return []
    try:
        resp = httpx.get(
            _SEARCH_URL,
            params={
                "key": GOOGLE_API_KEY,
                "cx":  GOOGLE_CX,
                "q":   query,
                "num": 3,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code == 403:
            err_msg = resp.json().get("error", {}).get("message", resp.text[:200])
            raise _APIUnavailable(err_msg)
        if resp.status_code == 429:
            logger.warning("Google CSE quota exhausted (429) — stopping for today")
            raise _APIUnavailable("quota exhausted")
        if resp.status_code >= 400:
            logger.warning("Google CSE error %s: %s", resp.status_code, resp.text[:200])
            return []
        items = resp.json().get("items", [])
        return [item["link"] for item in items if "link" in item]
    except _APIUnavailable:
        raise
    except Exception as exc:
        logger.debug("Google CSE request failed: %s", exc)
        return []


def _find_social_url(
    brand_name: str,
    site_query: str,
    expected_domain: str,
    brand_domain: str | None = None,
) -> str | None:
    """
    Search Google for a brand's social profile on one platform.
    When brand_domain is available, includes it in the query so Google anchors
    results to pages that mention the brand's own website — reducing false positives
    for generic brand names (e.g. "Axe", "Coast", "Dove").
    Falls back to name-only search if the domain-anchored search returns nothing.
    Propagates _APIUnavailable so the caller can abort the run.
    """
    # Domain-anchored search first (higher precision)
    if brand_domain:
        anchored_query = f"{brand_name} {brand_domain} {site_query}"
        urls = _google_search(anchored_query)
        for url in urls:
            if _is_valid_profile_url(url, expected_domain):
                return normalize_social_url(url)

    # Fallback: name-only search (lower precision, but catches brands whose
    # social About pages don't mention the domain)
    query = f"{brand_name} {site_query}"
    urls  = _google_search(query)
    for url in urls:
        if _is_valid_profile_url(url, expected_domain):
            return normalize_social_url(url)

    return None


def enrich_google_socials(db: Session, limit: int = 50) -> int:
    """
    For each brand with at least one missing social handle:
      1. For each NULL social field, run a targeted Google search
      2. Validate the result is a real profile URL for that platform
      3. Write it to brands_raw only if the field is still NULL
      4. Mark google_social_checked=True

    Returns number of brands processed.
    Requires GOOGLE_API_KEY and GOOGLE_CX in .env.
    """
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logger.warning("Google Social Search: GOOGLE_API_KEY or GOOGLE_CX not set — skipping")
        return 0

    # Only process brands that haven't been google-social-checked yet
    # AND are missing at least one social handle
    brands: list[BrandRaw] = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.google_social_checked == False,
            (
                BrandRaw.instagram_handle.is_(None)   |
                BrandRaw.tiktok_handle.is_(None)      |
                BrandRaw.youtube_channel_id.is_(None) |
                BrandRaw.facebook_page.is_(None)      |
                BrandRaw.twitter_handle.is_(None)     |
                BrandRaw.linkedin_id.is_(None)
            ),
        )
        .limit(limit)
        .all()
    )

    if not brands:
        logger.info("Google Social Search: no pending brands")
        return 0

    logger.info("Google Social Search: processing %d brands", len(brands))
    total_found = 0

    for brand in brands:
        found_count = 0

        for field, site_query, expected_domain in _PLATFORMS:
            # Skip if already filled (wikidata or shopify already found it)
            if getattr(brand, field):
                continue

            try:
                url = _find_social_url(brand.name, site_query, expected_domain, brand_domain=brand.domain)
            except _APIUnavailable as exc:
                logger.error(
                    "Google Social Search: API unavailable — aborting run without "
                    "marking brands as checked. Fix your API key then re-run. (%s)", exc
                )
                logger.error(
                    "To fix: go to console.cloud.google.com → APIs & Services → "
                    "Enable 'Custom Search API' for your project."
                )
                db.rollback()
                return 0

            if url:
                setattr(brand, field, url)
                found_count += 1
                logger.debug("  %s.%s = %s", brand.name, field, url)

            time.sleep(0.3)   # stay within CSE rate limits

        brand.google_social_checked = True
        db.commit()
        total_found += found_count
        logger.info(
            "Google Social Search: '%s' → %d social URL(s) found",
            brand.name, found_count,
        )
        time.sleep(0.3)

    logger.info(
        "Google Social Search: %d brands processed, %d social URLs filled",
        len(brands), total_found,
    )
    return len(brands)
