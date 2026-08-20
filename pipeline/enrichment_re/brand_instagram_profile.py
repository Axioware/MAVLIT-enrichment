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
     "website" / "social" / "linktree" / "marketplace" / "unknown". This same
     call also derives the brand's real name (from fullName/bio/the
     website's own domain together) whenever — and only whenever — it
     lands on "website"; every other category returns an empty name.
       - "website"  -> store the URL and the LLM-derived name directly.
       - "linktree" -> scrape that link-in-bio page's outbound links and
         classify each one the same way until one comes back "website"
         (carrying its own derived name along with it).
  3. If nothing resolved from the profile (no external URL, private
     account, or every link classified "social"/"marketplace"/"unknown"),
     fall back to a search against a local SearXNG instance (config.
     SEARXNG_URL) for "<handle> official website" — up to _RAW_RESULTS_LIMIT
     raw results, grouped by domain and ranked (_rank_domain_candidates;
     see its docstring) rather than just taking the first few URLs, since a
     brand's real site often appears as several different subpages spread
     across a long result list, each individually outranked by
     single-appearance social/platform links. The top _TOP_CANDIDATES
     domains go to a second LLM call (brand_website_search_pick) — given
     the profile's own bio/external URL, plus whatever name step 2 may
     already have derived, as context to rule out lookalikes and
     marketplace/press listings and to confirm-or-correct that name —
     which one (if any) is the real official site, and the brand's real
     name to go with it.

Every brand this module ever touches has name IS NULL by construction (the
module's own query filter), so there is never an already-set name for
either LLM call here to correct in practice — both simply derive one fresh
whenever they land on a real website, and produce nothing when they don't.

Sets instagram_profile_checked=True whether or not a website was found, so
unresolved handles aren't retried every run — has_official_website is set
False in that case so the row is still distinguishable from "never
attempted".
"""

import logging
import time
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import APIFY_TOKEN, SEARXNG_URL
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
_MAX_LINKTREE_LINKS = 10  # safety cap on how many outbound links to classify per linktree page

# How many raw (URL-deduped) SearXNG results to pull before domain
# aggregation/ranking — deliberately generous. The brand's own site often
# shows up as several different subpages spread across a long result list
# (e.g. simplymander.com/, /about, /membership all individually outranked
# by single-appearance platform links), so capping this too low throws away
# exactly the frequency signal _rank_domain_candidates depends on.
_RAW_RESULTS_LIMIT = 40
# How many top-ranked DOMAIN candidates actually get sent to the LLM.
_TOP_CANDIDATES = 5

# Domains that are never a brand's own official website — filtered out
# before ranking so a social/platform profile can't crowd out the real
# site just by having a higher individual SearXNG rank. Edit this set to
# add/remove platforms.
_PLATFORM_DOMAINS = frozenset([
    "instagram.com", "youtube.com", "tiktok.com", "facebook.com",
    "x.com", "twitter.com", "linkedin.com", "linktr.ee",
    "patreon.com", "reddit.com", "tumblr.com",
])


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

def _classify_link(db: Session, handle: str, full_name: str, bio: str, url: str) -> tuple[str, str]:
    """
    Returns (category, name). category is one of 'website' / 'social' /
    'linktree' / 'marketplace' / 'unknown'. name is only ever non-empty when
    category == 'website' — the prompt is instructed to derive the brand's
    real name (from full_name/bio/domain together) in that case only, and
    to leave it blank otherwise; enforced again here in case the model
    doesn't comply.
    """
    prompt = fill_template(
        _get_prompt(db, LINK_CLASSIFY_PROMPT_NAME, LINK_CLASSIFY_DEFAULT_PROMPT),
        handle=handle, full_name=full_name or "", bio=bio[:500], url=url,
    )
    result = call_gpt_json(prompt, context=f"link_classify @{handle} {url}")
    category = str(result.get("category") or "unknown").strip().lower()
    if category not in ("website", "social", "linktree", "marketplace"):
        category = "unknown"
    name = str(result.get("name") or "").strip() if category == "website" else ""
    return category, name


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


def _resolve_from_profile(
    db: Session, handle: str, full_name: str, bio: str, external_url: str,
) -> tuple[str | None, str | None, str]:
    """Returns (website, website_source, name) — name is "" whenever website is None."""
    if not external_url:
        return None, None, ""

    category, name = _classify_link(db, handle, full_name, bio, external_url)
    if category == "website":
        return external_url, "instagram_bio", name

    if category == "linktree":
        for link in _scrape_outbound_links(external_url):
            link_category, link_name = _classify_link(db, handle, full_name, bio, link)
            if link_category == "website":
                return link, "instagram_linktree", link_name

    return None, None, ""


#  SearXNG search fallback

def _searxng_search(query: str) -> list[dict]:
    """
    Raw results via the local SearXNG instance: [{"title", "url", "snippet"},
    ...] — [] if it's unreachable, JSON output isn't enabled on it (see
    config.SEARXNG_URL's comment), or the call otherwise fails. Deduped by
    exact URL only (domain-level grouping happens separately, in
    _rank_domain_candidates) and capped at _RAW_RESULTS_LIMIT.
    """
    try:
        resp = httpx.get(
            f"{SEARXNG_URL.rstrip('/')}/search",
            params={"q": query, "format": "json"},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("results") or []
    except Exception as exc:
        logger.warning("SearXNG search failed for %r: %s", query, exc)
        return []

    seen: set[str] = set()
    out: list[dict] = []
    for i in items:
        url = (i.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"title": i.get("title", ""), "url": url, "snippet": i.get("content", "")})
        if len(out) >= _RAW_RESULTS_LIMIT:
            break

    logger.info("SearXNG raw results for %r: %d — %s", query, len(out), [r["url"] for r in out])
    return out


def _is_root_path(url: str) -> bool:
    """True if url's path is empty or just "/" — a homepage, not a subpage."""
    try:
        return urlparse(url).path in ("", "/")
    except Exception:
        return False


def _tld_score(domain: str) -> int:
    """Prefer generic top-level domains over country-code/other ones — same heuristic as wikidata.py's _website_score."""
    parts = domain.split(".")
    if len(parts) == 2 and parts[-1] in ("com", "org", "net", "io"):
        return 2
    if len(parts) == 2:
        return 1
    return 0


def _rank_domain_candidates(handle: str, results: list[dict]) -> list[dict]:
    """
    Groups raw SearXNG results by domain, drops known platform/social
    domains (_PLATFORM_DOMAINS), and scores what's left so a brand's real
    site — which often shows up as several different subpages spread across
    a long result list, each individually outranked by single-appearance
    platform links — wins on aggregate signal instead of being discarded
    before the LLM ever sees it.

    Score per domain = handle-in-domain match (+3) + frequency (+1 per
    URL seen, capped at +5) + has a root-path URL in its group (+2) +
    TLD quality (0-2, see _tld_score).

    Returns up to _TOP_CANDIDATES candidates, highest score first, each:
      {"domain", "url" (root path preferred as the representative),
       "title", "snippet", "count", "score"}
    """
    filtered = [r for r in results if _extract_domain(r["url"]) not in _PLATFORM_DOMAINS]
    logger.info(
        "SearXNG filtered results for @%s (platform domains removed): %d -> %d — %s",
        handle, len(results), len(filtered), [r["url"] for r in filtered],
    )

    groups: dict[str, list[dict]] = {}
    for r in filtered:
        domain = _extract_domain(r["url"])
        if domain:
            groups.setdefault(domain, []).append(r)

    logger.info(
        "SearXNG grouped domains for @%s: %s",
        handle, {d: len(g) for d, g in groups.items()},
    )

    handle_key = "".join(ch for ch in handle.lower() if ch.isalnum())

    candidates: list[dict] = []
    for domain, group in groups.items():
        root = next((r for r in group if _is_root_path(r["url"])), None)
        representative = root or group[0]

        domain_key = "".join(ch for ch in domain.split(".")[0].lower() if ch.isalnum())
        handle_match  = 3 if handle_key and handle_key in domain_key else 0
        frequency     = min(len(group), 5)
        root_bonus    = 2 if root else 0
        tld           = _tld_score(domain)
        score = handle_match + frequency + root_bonus + tld

        candidates.append({
            "domain": domain,
            "url": representative["url"],
            "title": representative["title"],
            "snippet": representative["snippet"],
            "count": len(group),
            "score": score,
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)

    logger.info(
        "SearXNG ranked domain candidates for @%s: %s",
        handle, [(c["domain"], f"count={c['count']}", f"score={c['score']}") for c in candidates],
    )

    return candidates[:_TOP_CANDIDATES]


def _resolve_from_search(
    db: Session, handle: str, bio: str, external_url: str, saved_name: str,
) -> tuple[str | None, str | None, str]:
    """
    Returns (website, website_source, name). Candidates sent to the LLM are
    ranked DOMAINS (see _rank_domain_candidates), not raw top-N URLs — a
    domain's frequency across many result pages is itself a strong signal
    that a naive "first N URLs" cutoff would discard. name is derived from
    the search results (using saved_name — whatever brand_instagram_
    profile.py's own LINK_CLASSIFY tier may already have found earlier in
    the same call — as a hint the LLM can confirm or correct), and is ""
    whenever website is None.
    """
    query = f"{handle} official website"
    raw_results = _searxng_search(query)
    if not raw_results:
        return None, None, ""

    candidates = _rank_domain_candidates(handle, raw_results)
    if not candidates:
        return None, None, ""

    listing = "\n".join(
        f"{i + 1}. {c['url']} — {c['title']} — {c['snippet']} (this domain appeared {c['count']}x in search results)"
        for i, c in enumerate(candidates)
    )
    prompt = fill_template(
        _get_prompt(db, WEBSITE_PICK_PROMPT_NAME, WEBSITE_PICK_DEFAULT_PROMPT),
        handle=handle, bio=bio[:500], external_url=external_url or "none",
        saved_name=saved_name or "unknown",
        query=query, results=listing,
    )
    result = call_gpt_json(prompt, context=f"website_pick @{handle}")
    try:
        idx = int(result.get("index", 0))
    except (TypeError, ValueError):
        idx = 0

    if 1 <= idx <= len(candidates):
        name = str(result.get("name") or "").strip()
        return candidates[idx - 1]["url"], "searxng_search", name
    return None, None, ""


#  Apply + commit

def _apply_result(
    db: Session, brand: BrandRaw, name: str, website: str | None, website_source: str | None,
) -> None:
    if name and not brand.name:
        brand.name = name
        brand.name_normalized = normalize(name)

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
        # this row's NAME bare rather than crash the batch, but still mark
        # checked. db.rollback() discards every staged change on `brand`,
        # not just the colliding name (SQLAlchemy expires the whole
        # instance) — website/domain/has_official_website/website_source
        # are unrelated to the collision and must be re-applied here too
        # (using `website`, already normalized above), or a name collision
        # would silently wipe out perfectly good website data as well.
        db.rollback()
        brand.name = None
        brand.name_normalized = None
        if website:
            brand.website = website
            brand.domain = _extract_domain(website)
            brand.has_official_website = True
            brand.website_source = website_source
        else:
            brand.has_official_website = False
        brand.instagram_profile_checked = True
        db.commit()
        logger.warning(
            "Brand Instagram profile lookup: name '%s' collided with an existing brand — "
            "name left unresolved, website/domain still saved",
            name,
        )


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

            website, website_source, name = _resolve_from_profile(db, handle, full_name, bio, external_url)
            if not website:
                # name is always "" here — LINK_CLASSIFY only derives one
                # when it finds "website" itself, which this branch means it
                # didn't — passed through anyway as the search-pick prompt's
                # saved_name hint, in case that ever changes.
                website, website_source, name = _resolve_from_search(db, handle, bio, external_url, name)

            _apply_result(db, brand, name, website, website_source)
            # Read back from `brand` (not the local `website`/`name` vars) —
            # _apply_result normalizes the URL and can clear `name` on a
            # collision, so the local vars no longer reflect what actually
            # got committed.
            logger.info(
                "Brand Instagram profile lookup: @%s → %s / name=%s (%s)",
                handle, brand.website or "no website found", brand.name or "none",
                website_source or "unresolved",
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
