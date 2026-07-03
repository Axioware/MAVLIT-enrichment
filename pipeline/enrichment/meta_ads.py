"""
pipeline/enrichment/meta_ads.py

Searches Meta Ad Library for active ads associated with each brand.
https://graph.facebook.com/v21.0/ads_archive

Requires META_ACCESS_TOKEN in config.
Stores each ad as a row in meta_ads table.
Sets meta_ads_fetched=True on the brands_raw row after processing.

Search strategy:
  Ads are fetched exclusively by search_page_ids (numeric Facebook page ID).
  search_terms is NOT used — it produces too many false positives.

Page ID resolution:
  When a brand has facebook_page URL but no facebook_page_id, the Graph API
  is called to resolve the numeric page ID from the page slug.  The ID is
  persisted so it is only resolved once per brand.
  If the page ID cannot be resolved, the brand is skipped (not marked as fetched).
"""

import logging
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from config import META_ACCESS_TOKEN
from pipeline.db import BrandRaw, MetaAd
from pipeline.helpers.db import upsert_rows

logger = logging.getLogger(__name__)

_BASE_URL = "https://graph.facebook.com/v21.0"
_ADS_URL  = f"{_BASE_URL}/ads_archive"
_AD_LIMIT    = 100   # max per page allowed by Meta Ad Library API
_MAX_PAGES   = 10    # safety cap — 10 × 100 = 1,000 ads per brand max
_FIELDS   = ",".join([
    "id",
    "page_name",
    "page_id",
    "ad_creative_bodies",
    "publisher_platforms",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "impressions",
    "spend",
    "currency",
])


def _slug_from_url(url: str) -> str | None:
    """Extract page slug from a Facebook URL.
    https://www.facebook.com/nike  →  'nike'
    https://www.facebook.com/pages/Brand/123456  →  '123456'
    """
    try:
        parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
        if not parts:
            return None
        # /pages/Name/ID format — numeric ID at the end
        if parts[0].lower() == "pages" and len(parts) >= 3:
            return parts[-1]
        return parts[-1]
    except Exception:
        return None


def _resolve_page_id(slug: str) -> str | None:
    """
    Call Graph API to get the numeric Page ID for a vanity slug or numeric ID.
    Returns None on any error or if token is missing.
    """
    if not META_ACCESS_TOKEN:
        return None
    try:
        resp = httpx.get(
            f"{_BASE_URL}/{slug}",
            params={"fields": "id", "access_token": META_ACCESS_TOKEN},
            timeout=10,
        )
        if resp.status_code == 200:
            pid = resp.json().get("id")
            logger.debug("Resolved page ID: %s → %s", slug, pid)
            return pid
        err = resp.json().get("error", {})
        logger.warning(
            "Page ID resolution failed for '%s': HTTP %s — %s (code %s)",
            slug, resp.status_code, err.get("message"), err.get("code"),
        )
    except Exception as exc:
        logger.debug("Page ID resolution error for '%s': %s", slug, exc)
    return None


class _AuthError(Exception):
    """Raised when the Meta token is expired or invalid — aborts the whole run."""


def _fetch_ads(*, page_id: str | None, brand_name: str) -> list[dict]:
    """
    Call Meta Ad Library API and paginate through all results.
    Uses search_page_ids when page_id is available, otherwise search_terms.
    Fetches up to _MAX_PAGES × _AD_LIMIT ads per brand.
    Raises _AuthError if the token is expired/invalid so the caller can abort.
    """
    if not META_ACCESS_TOKEN:
        return []

    if page_id:
        params: dict = {
            "search_page_ids":      f'["{page_id}"]',
            "ad_type":              "ALL",
            "ad_reached_countries": '["US","GB","CA","AU"]',
            "limit":                _AD_LIMIT,
            "fields":               _FIELDS,
            "access_token":         META_ACCESS_TOKEN,
        }
        logger.debug("Meta Ads: searching by page_id=%s for '%s'", page_id, brand_name)
    else:
        params = {
            "search_terms":         brand_name,
            "ad_type":              "ALL",
            "ad_reached_countries": '["US","GB","CA","AU"]',
            "limit":                _AD_LIMIT,
            "fields":               _FIELDS,
            "access_token":         META_ACCESS_TOKEN,
        }
        logger.debug("Meta Ads: searching by name='%s' (no page_id)", brand_name)

    all_ads: list[dict] = []
    url = _ADS_URL

    for page_num in range(1, _MAX_PAGES + 1):
        try:
            resp = httpx.get(url, params=params, timeout=20)
            if resp.status_code == 429:
                logger.warning("Meta Ads API 429 — waiting 60s")
                time.sleep(60)
                resp = httpx.get(url, params=params, timeout=20)
            if resp.status_code >= 400:
                try:
                    body_err = resp.json()
                    err = body_err.get("error", {})
                    # Auth errors (expired/invalid token) — abort the entire run
                    # Only code 190 = token expired/invalid; other OAuthException
                    # codes (e.g. 100 = invalid parameter) are non-fatal per-brand errors.
                    if err.get("code") == 190:
                        raise _AuthError(err.get("message", "token expired/invalid"))
                    logger.error("Meta Ads API %s for '%s' — %s", resp.status_code, brand_name, body_err)
                except _AuthError:
                    raise
                except Exception:
                    logger.error("Meta Ads API %s for '%s' — %s", resp.status_code, brand_name, resp.text[:300])
                break
            body = resp.json()
        except _AuthError:
            raise
        except Exception:
            logger.exception("Meta Ads API call failed for '%s'", brand_name)
            break

        page_ads = body.get("data", [])
        all_ads.extend(page_ads)
        logger.debug("Meta Ads: '%s' page %d → %d ads (total so far: %d)",
                     brand_name, page_num, len(page_ads), len(all_ads))

        # Follow next-page cursor if available
        next_url = body.get("paging", {}).get("next")
        if not next_url or len(page_ads) < _AD_LIMIT:
            break

        # next page — Meta returns a full URL but omits ad_reached_countries,
        # so pass it explicitly to avoid a 400 "parameter required" error.
        url    = next_url
        params = {"ad_reached_countries": '["US","GB","CA","AU"]'}
        time.sleep(0.3)

    return all_ads


def _insert_ads(db: Session, brand_raw_id: int, ads: list[dict]) -> int:
    if not ads:
        return 0
    rows = []
    for ad in ads:
        rows.append({
            "brand_raw_id":        brand_raw_id,
            "ad_archive_id":       ad.get("id"),
            "page_name":           ad.get("page_name"),
            "page_id":             ad.get("page_id"),
            "ad_creative_bodies":  ad.get("ad_creative_bodies"),
            "publisher_platforms": ad.get("publisher_platforms"),
            "start_date":          ad.get("ad_delivery_start_time"),
            "end_date":            ad.get("ad_delivery_stop_time"),
            "impressions":         ad.get("impressions"),
            "spend":               ad.get("spend"),
            "currency":            ad.get("currency"),
        })
    return upsert_rows(db, MetaAd, rows, ["ad_archive_id"])


def enrich_meta_ads(db: Session, limit: int = 200) -> int:
    """
    For each brand with facebook_page or facebook_page_id set and meta_ads_fetched=False:
      1. Resolve the numeric facebook_page_id from the facebook_page URL via Graph API
         (skipped if facebook_page_id already known or facebook_page is NULL).
      2. Search ads by page_id only — no search_terms fallback.
      3. Store found ads in meta_ads table.
      4. Mark meta_ads_fetched=True.

    Brands where BOTH facebook_page and facebook_page_id are NULL are never
    selected and meta_ads_fetched is left as-is for them.

    Returns number of brand rows processed.
    """
    if not META_ACCESS_TOKEN:
        logger.warning("META_ACCESS_TOKEN not set — skipping Meta Ads enrichment")
        return 0

    brands: list[BrandRaw] = (
        db.query(BrandRaw)
        .filter(
            BrandRaw.meta_ads_fetched == False,
            (
                BrandRaw.facebook_page.isnot(None) |
                BrandRaw.facebook_page_id.isnot(None)
            ),
        )
        .limit(limit)
        .all()
    )

    if not brands:
        logger.info("Meta Ads: no pending brands with facebook info")
        return 0

    logger.info("Meta Ads: processing %d brands", len(brands))
    total_ads    = 0
    resolved_ids = 0

    for brand in brands:
        #  Step 1: resolve facebook_page_id from URL if not already known
        if not brand.facebook_page_id and brand.facebook_page:
            slug = _slug_from_url(brand.facebook_page)
            if slug:
                pid = _resolve_page_id(slug)
                if pid:
                    brand.facebook_page_id = pid
                    resolved_ids += 1
                    db.commit()
            time.sleep(0.3)

        if not brand.facebook_page_id:
            # Could not resolve a page ID — skip without marking as fetched
            logger.warning(
                "Meta Ads: could not resolve page_id for '%s' (facebook_page=%s) — skipping",
                brand.name, brand.facebook_page,
            )
            continue

        #  Step 2: fetch ads by page_id only 
        try:
            ads = _fetch_ads(page_id=brand.facebook_page_id, brand_name=brand.name)
        except _AuthError as exc:
            logger.error(
                "Meta Ads: token expired/invalid — aborting run. "
                "Update META_ACCESS_TOKEN and re-run. (%s)", exc
            )
            return len(brands) - brands.index(brand)

        inserted = _insert_ads(db, brand.id, ads)
        total_ads += inserted

        brand.meta_ads_fetched = True
        db.commit()
        logger.debug("Meta Ads: '%s' (page_id=%s) → %d ads",
                     brand.name, brand.facebook_page_id, inserted)
        time.sleep(0.5)

    logger.info(
        "Meta Ads: %d brands processed, %d page IDs resolved, %d ads stored",
        len(brands), resolved_ids, total_ads,
    )
    return len(brands)
