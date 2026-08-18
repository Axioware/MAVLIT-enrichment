"""
pipeline/enrichment_re/brand_instagram_profile.py

Third-tier name/website resolution for bare brands_raw rows (name IS NULL,
instagram_handle set) that pipeline/enrichment_re/brand_wikidata_lookup.py
already checked and failed to resolve (instagram_wikidata_checked=True).
Where that step queries Wikidata by instagram_handle, this one goes
straight to the source: the brand's own Instagram profile.

Resolution order, first hit wins:
  1. Scrape the profile (Apify, same actor/addParentData trick used
     elsewhere) for fullName + biography + externalUrl.
  2. Classify the bio's externalUrl via LLM (instagram_link_classify) as
     "website" / "social" / "linktree" / "unknown".
       - "website"  -> store it directly.
       - "linktree" -> scrape that link-in-bio page's outbound links and
         classify each one the same way until one comes back "website".
  3. If nothing resolved from the profile (no external URL, private
     account, or every link classified "social"/"unknown"), fall back to a
     Google Custom Search for "<handle> official website", take the top 5
     results, and ask a second LLM call (brand_website_search_pick) —
     given the profile's own bio/external URL as context to rule out
     lookalikes and marketplace/press listings — which one (if any) is the
     real official site.

fullName backfills brands_raw.name whenever a website is also found (a bare
row with a name but no website/niche/source still isn't useful downstream —
shopify_detect.py, wikidata_socials.py etc. all require an official website
to run at all), but not that everything else. Sets instagram_profile_checked=
True whether or not a website was found, so unresolved handles aren't
retried every run — has_official_website is set False in that case so the
row is still distinguishable from "never attempted".
"""

import logging
import time
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import APIFY_TOKEN, GOOGLE_SEARCH_API_KEY, GOOGLE_SEARCH_CX
from pipeline.db import BrandRaw, Prompt
from pipeline.helpers.apify import ApifyQuotaExceeded, run_apify_actor
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.normalize import normalize
from pipeline.helpers.prompts import (
    LINK_CLASSIFY_PROMPT_NAME, LINK_CLASSIFY_DEFAULT_PROMPT,
    WEBSITE_PICK_PROMPT_NAME, WEBSITE_PICK_DEFAULT_PROMPT,
)
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_ACTOR_ID = "shu8hvrXbJbY3Eb9W"  # same Instagram scraper used across the pipeline
# Full browser-like header set — link-in-bio hosts (Linktree in particular,
# fronted by Fastly) 403 a bare User-Agent-only request as bot traffic; this
# fingerprint is what actually gets a 200 (confirmed live against linktr.ee).
_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
_PAGE_TIMEOUT      = 12
_SEARCH_RESULTS    = 5
_MAX_LINKTREE_LINKS = 10  # safety cap on how many outbound links to classify per linktree page


#  Prompt lookup

def _get_prompt(db: Session, name: str, default: str) -> str:
    row = db.query(Prompt).filter(Prompt.name == name).first()
    return row.content if row else default


#  URL helpers

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


def _normalize_website(url: str) -> str:
    """
    Strip a resolved website down to just its domain root — a bio link,
    linktree entry, or search result is often a specific landing/referral
    page (e.g. "https://jpfans.com/register?ref=800000016"), not the
    brand's actual homepage, and downstream steps (shopify_detect.py etc.)
    expect a plain root URL to crawl. Falls back to the original url
    unchanged if it doesn't parse into a scheme+netloc.
    """
    try:
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return url
        return f"{p.scheme}://{p.netloc}/"
    except Exception:
        return url


#  Instagram profile scrape

def _scrape_profile(handle: str) -> dict | None:
    """
    One Apify call for profile fields only (fullName/biography/externalUrl).
    resultsLimit=1 is enough — addParentData embeds the full profile
    snapshot on every post item regardless of how many are requested (same
    trick instagram_users.py uses for commenters, which only ever need 1).

    Returns:
      dict   — call succeeded (fullName/biography/externalUrl, any of
               which may be empty for a sparse or private profile)
      None   — the Apify call itself failed (quota/network/actor crash) —
               caller should leave the row unmarked so it's retried later,
               NOT treat it as "checked, nothing found"
    """
    items = run_apify_actor(
        _ACTOR_ID,
        {
            "addParentData": True,
            "directUrls":    [f"https://www.instagram.com/{handle}/"],
            "resultsType":   "posts",
            "resultsLimit":  1,
        },
        label=f"IGProfile @{handle}",
        require_success=True,
    )
    if items is None:
        return None
    if not items:
        return {}  # succeeded, but nothing came back (private/deleted account)

    md = items[0].get("metaData") or {}
    return {
        "fullName":    md.get("fullName") or items[0].get("ownerFullName") or "",
        "biography":   md.get("biography") or "",
        "externalUrl": md.get("externalUrl") or "",
    }


#  Link classification (bio URL, and linktree outbound links)

def _classify_link(db: Session, handle: str, bio: str, url: str) -> str:
    """Returns one of 'website' / 'social' / 'linktree' / 'unknown'."""
    prompt = fill_template(
        _get_prompt(db, LINK_CLASSIFY_PROMPT_NAME, LINK_CLASSIFY_DEFAULT_PROMPT),
        handle=handle, bio=bio[:500], url=url,
    )
    result = call_gpt_json(prompt, context=f"link_classify @{handle} {url}")
    category = str(result.get("category") or "unknown").strip().lower()
    return category if category in ("website", "social", "linktree") else "unknown"


def _scrape_outbound_links(url: str) -> list[str]:
    """Fetch a link-in-bio page and return its outbound link URLs (deduped by domain, capped)."""
    try:
        resp = httpx.get(url, headers=_PAGE_HEADERS, timeout=_PAGE_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.debug("Linktree page fetch failed for %s: %s", url, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    page_domain = _extract_domain(str(resp.url))
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue
        domain = _extract_domain(href)
        if not domain or domain == page_domain or domain in seen:
            continue
        seen.add(domain)
        links.append(href)
        if len(links) >= _MAX_LINKTREE_LINKS:
            break
    return links


def _resolve_from_profile(db: Session, handle: str, bio: str, external_url: str) -> tuple[str | None, str | None]:
    """Returns (website, website_source) or (None, None)."""
    if not external_url:
        return None, None

    category = _classify_link(db, handle, bio, external_url)
    if category == "website":
        return external_url, "instagram_bio"

    if category == "linktree":
        for link in _scrape_outbound_links(external_url):
            if _classify_link(db, handle, bio, link) == "website":
                return link, "instagram_linktree"

    return None, None


#  Google search fallback

def _google_search(query: str) -> list[dict]:
    """Top results: [{"title", "url", "snippet"}, ...] — [] if unconfigured or the call fails."""
    if not (GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX):
        logger.warning("GOOGLE_SEARCH_API_KEY/GOOGLE_SEARCH_CX not set — skipping search fallback")
        return []
    try:
        resp = httpx.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": GOOGLE_SEARCH_API_KEY,
                "cx":  GOOGLE_SEARCH_CX,
                "q":   query,
                "num": _SEARCH_RESULTS,
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items") or []
        return [
            {"title": i.get("title", ""), "url": i.get("link", ""), "snippet": i.get("snippet", "")}
            for i in items[:_SEARCH_RESULTS]
        ]
    except Exception as exc:
        logger.warning("Google search failed for %r: %s", query, exc)
        return []


def _resolve_from_search(db: Session, handle: str, bio: str, external_url: str) -> tuple[str | None, str | None]:
    """Returns (website, website_source) or (None, None)."""
    query = f"{handle} official website"
    results = _google_search(query)
    if not results:
        return None, None

    listing = "\n".join(
        f"{i + 1}. {r['title']} — {r['url']} — {r['snippet']}"
        for i, r in enumerate(results)
    )
    prompt = fill_template(
        _get_prompt(db, WEBSITE_PICK_PROMPT_NAME, WEBSITE_PICK_DEFAULT_PROMPT),
        handle=handle, bio=bio[:500], external_url=external_url or "none",
        query=query, results=listing,
    )
    result = call_gpt_json(prompt, context=f"website_pick @{handle}")
    try:
        idx = int(result.get("index", 0))
    except (TypeError, ValueError):
        idx = 0

    if 1 <= idx <= len(results):
        return results[idx - 1]["url"], "google_search"
    return None, None


#  Apply + commit

def _apply_result(
    db: Session, brand: BrandRaw, full_name: str, website: str | None, website_source: str | None,
) -> None:
    if full_name and not brand.name:
        brand.name = full_name
        brand.name_normalized = normalize(full_name)

    if website:
        website = _normalize_website(website)
        brand.website = website
        brand.domain = _extract_domain(website)
        brand.has_official_website = True
        brand.website_source = website_source
    else:
        brand.has_official_website = False

    brand.instagram_profile_checked = True

    try:
        db.commit()
    except IntegrityError:
        # name_normalized collided with an existing brand (e.g. a properly
        # seeded row for the same real-world brand already exists) — leave
        # this row bare rather than crash the batch, but still mark checked.
        db.rollback()
        brand.name = None
        brand.name_normalized = None
        brand.instagram_profile_checked = True
        db.commit()
        logger.warning("Brand Instagram profile lookup: name collided with an existing brand — left unresolved")


#  Main entry point

def enrich_brand_instagram_profile(db: Session, limit: int = 50, brand_id: int | None = None) -> int:
    """
    For bare brands_raw rows (name IS NULL, instagram_handle set) that
    brand_wikidata_lookup.py already checked and failed to resolve
    (instagram_wikidata_checked=True), scrape the brand's own Instagram
    profile to backfill name and website — see module docstring for the
    full resolution order.

    Pass brand_id to target one specific brand directly — bypasses both the
    instagram_wikidata_checked and instagram_profile_checked filters.

    Returns number of brand rows processed (resolved or not).
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping brand_instagram_profile enrichment")
        return 0

    query = db.query(BrandRaw).filter(
        BrandRaw.name.is_(None),
        BrandRaw.instagram_handle.isnot(None),
    )
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    else:
        query = query.filter(
            BrandRaw.instagram_wikidata_checked == True,
            BrandRaw.instagram_profile_checked == False,
        )

    brands: list[BrandRaw] = query.limit(limit).all()
    if not brands:
        logger.info("Brand Instagram profile lookup: no pending bare brands")
        return 0

    logger.info("Brand Instagram profile lookup: processing %d brand(s)", len(brands))
    processed = 0

    try:
        for brand in brands:
            handle = normalize_handle(brand.instagram_handle)
            if not handle:
                brand.instagram_profile_checked = True
                db.commit()
                processed += 1
                continue

            profile = _scrape_profile(handle)
            if profile is None:
                logger.warning(
                    "Brand Instagram profile lookup: @%s scrape failed (actor limit reached, "
                    "network error, etc.) — leaving instagram_profile_checked=False so this "
                    "row is retried",
                    handle,
                )
                continue

            full_name    = profile.get("fullName", "")
            bio          = profile.get("biography", "")
            external_url = profile.get("externalUrl", "")

            website, website_source = _resolve_from_profile(db, handle, bio, external_url)
            if not website:
                website, website_source = _resolve_from_search(db, handle, bio, external_url)

            _apply_result(db, brand, full_name, website, website_source)
            logger.info(
                "Brand Instagram profile lookup: @%s → %s (%s)",
                handle, website or "no website found", website_source or "unresolved",
            )
            processed += 1
            time.sleep(1)
    except ApifyQuotaExceeded as exc:
        logger.error(
            "Brand Instagram profile lookup: %s — stopping this run early after %d brand(s) "
            "processed; remaining brands stay instagram_profile_checked=False for the next run",
            exc, processed,
        )

    logger.info("Brand Instagram profile lookup: %d brand(s) processed", processed)
    return processed
