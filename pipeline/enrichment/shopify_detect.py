"""
pipeline/enrichment/shopify_detect.py

Fetches each brand's homepage to:
  1. Detect Shopify (cdn.shopify.com fingerprint) → is_shopify
  2. Detect WooCommerce (wp-content/plugins/woocommerce fingerprint) → is_woocommerce
  3. Extract full social media profile URLs → instagram_handle, twitter_handle,
     facebook_page, youtube_channel_id, tiktok_handle, linkedin_id

Social fields are only written if the column is currently NULL so they don't
overwrite more-authoritative Wikidata data fetched earlier.

Sets shopify_checked=True for every processed brand.
Only runs for brands that have a website (has_official_website=True).
"""

import logging
import time
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from pipeline.db import BrandRaw
from pipeline.helpers.social import normalize_social_url

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}
_SHOPIFY_MARKER      = "cdn.shopify.com"
_WOOCOMMERCE_MARKERS = (
    "wp-content/plugins/woocommerce",
    "wc-ajax=",
    "woocommerce-page",
)
_TIMEOUT = 12

# Paths that appear in social share/utility URLs — not profile pages
_SKIP_PATHS = frozenset([
    "share", "sharer", "sharer.php", "intent", "login", "signup",
    "register", "logout", "help", "about", "policies", "legal",
    "p", "reel", "explore", "hashtag", "search", "ads",
    "watch", "playlist", "shorts", "results",
])


def _fetch_page(url: str) -> str | None:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.debug("Page fetch failed for %s: %s", url, exc)
        return None


def _extract_socials(html: str) -> dict[str, str]:
    """
    Parse all <a href> tags and return full social media profile URLs.
    Returns a dict with any subset of keys:
      instagram_handle, twitter_handle, facebook_page,
      youtube_channel_id, tiktok_handle, linkedin_id
    Values are full cleaned URLs (https, no query/fragment).
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        try:
            parsed = urlparse(href)
        except Exception:
            continue

        # Must be an absolute URL with a recognised social domain
        if not parsed.scheme.startswith("http"):
            continue

        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]

        path  = parsed.path.strip("/")
        parts = [p for p in path.split("/") if p]
        if not parts:
            continue

        first = parts[0].lower()
        if first in _SKIP_PATHS:
            continue

        if netloc == "instagram.com" and "instagram_handle" not in found:
            if first not in ("p", "reel", "stories", "explore", "tv"):
                found["instagram_handle"] = normalize_social_url(href)

        elif netloc in ("twitter.com", "x.com") and "twitter_handle" not in found:
            if first not in _SKIP_PATHS:
                found["twitter_handle"] = normalize_social_url(href)

        elif netloc == "facebook.com" and "facebook_page" not in found:
            if first == "pages" and len(parts) >= 2:
                found["facebook_page"] = normalize_social_url(href)
            elif first not in _SKIP_PATHS and "." not in first:
                found["facebook_page"] = normalize_social_url(href)

        elif netloc == "youtube.com" and "youtube_channel_id" not in found:
            if first in ("channel", "c", "user") and len(parts) >= 2:
                found["youtube_channel_id"] = normalize_social_url(href)
            elif parts[0].startswith("@"):
                found["youtube_channel_id"] = normalize_social_url(href)
            elif first not in _SKIP_PATHS:
                found["youtube_channel_id"] = normalize_social_url(href)

        elif netloc == "tiktok.com" and "tiktok_handle" not in found:
            handle = parts[0].lower()
            if handle.startswith("@"):
                handle = handle[1:]
            if handle and handle not in _SKIP_PATHS:
                found["tiktok_handle"] = normalize_social_url(href)

        elif netloc == "linkedin.com" and "linkedin_id" not in found:
            if first == "company" and len(parts) >= 2:
                found["linkedin_id"] = normalize_social_url(href)

    return found


def enrich_shopify(db: Session, limit: int = 300) -> int:
    """
    Fetch homepage for each brand with a website URL and shopify_checked=False.
    Sets is_shopify / is_woocommerce and fills any NULL social URL columns from
    links found on the page. Returns number of rows updated.
    """
    brands: list[BrandRaw] = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.website.isnot(None),
            BrandRaw.shopify_checked == False,
        )
        .limit(limit)
        .all()
    )

    if not brands:
        logger.info("Shopify/socials check: no pending brands")
        return 0

    logger.info("Shopify/socials check: processing %d brands", len(brands))
    updated       = 0
    shopify_count = 0
    socials_count = 0

    for brand in brands:
        html = _fetch_page(brand.website)

        if html is None:
            brand.is_shopify      = False
            brand.is_woocommerce  = False
            brand.shopify_checked = True
        else:
            brand.is_shopify      = _SHOPIFY_MARKER in html
            brand.is_woocommerce  = any(m in html for m in _WOOCOMMERCE_MARKERS)
            brand.shopify_checked = True

            socials = _extract_socials(html)
            for field, url in socials.items():
                if url:
                    setattr(brand, field, url)
                    socials_count += 1
                    logger.debug("  %s.%s = %s", brand.name, field, url)

            if brand.is_shopify:
                shopify_count += 1

        updated += 1
        db.commit()
        time.sleep(0.3)

    woo_count = sum(1 for b in brands if b.is_woocommerce)
    logger.info(
        "Shopify/socials check: %d done, %d Shopify, %d WooCommerce, %d social URLs filled",
        updated, shopify_count, woo_count, socials_count,
    )
    return updated
