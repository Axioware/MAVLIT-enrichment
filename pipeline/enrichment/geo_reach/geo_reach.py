"""
pipeline/enrichment/geo_reach/geo_reach.py

Crawls each brand's own website (brands_raw.website) to estimate how wide a
geographic footprint the brand actually operates in — a single town, a
handful of states, one whole country, or worldwide — and stores that as a
0-100 score.

Score rubric (brands_raw.geo_reach_score / geo_reach_label) — the US and
Canada are treated as ONE combined target region for the granular tiers;
any footprint entirely outside the US/Canada collapses to a single top
bucket regardless of how narrow or wide it is there:

    100 = outside_us_canada             Doesn't operate in the US or Canada
                                         at all — footprint entirely in
                                         other country/countries
     90 = single_city_us_canada         Single city/town, in the US/Canada
     80 = single_state_us_canada        Single state/province, US/Canada
     70 = few_states_us_canada          2-5 states/provinces, US/Canada
     60 = many_states_us_canada         6-15 states/provinces, US/Canada
     40 = nationwide_us_canada          Most/all of the US or Canada,
                                         nowhere else
     20 = multi_country_with_us_canada  US/Canada PLUS at least one other
                                         country (e.g. US + Germany)
      0 = global                        Global/worldwide presence

This ordering is intentional, not a mistake — it exists so a simple
"score DESC" sort surfaces non-US/Canada brands first, then narrows down
through the US/Canada footprint from smallest to nationwide, since that's
the filtering the score is meant to drive.

Crawl design
------------
Starts at the homepage. Every page fetched is parsed for its own real
<a href> links (never a path invented by us — e.g. guessing "/locations"
exists just because it's a common pattern), which are queued as candidates
for the next pages: links whose path/anchor text look location-relevant
(store locator, contact, shipping, about, international, ...) go first,
every other real internal link on the page follows as a fallback. At most
_MAX_PAGES (5) pages are fetched per brand.

Two kinds of memory are kept for the duration of one brand's crawl:

  1. `visited` — the set of page URLs already fetched, so the crawler never
     re-fetches the same page twice even if it's linked from multiple spots.
  2. `accumulated` — the running list of location/reach signals extracted
     from every page scraped so far. Each new page is scored by the LLM
     together with everything accumulated from earlier pages, so a state
     found on page 1 and a different state found on page 3 both end up in
     the same final answer instead of the later page overwriting the
     earlier one.

The LLM is asked, after each page, whether it already has enough evidence to
commit to a final score; if not, the crawl continues to the next page. If
all 5 pages are exhausted without a confident answer, one last "you must
decide now" call is made using everything accumulated. Both the merged
locations list and the per-page crawl log are persisted (geo_reach_locations
/ geo_reach_pages_scraped) so a later look at the row shows exactly what was
found and where.

The brand's own Instagram bio (instagram_posts.biography, most recent row)
is passed to the LLM alongside the page text as extra context.

Safe to re-run — rows with geo_reach_checked=True are skipped. Only brands
with a website are eligible.
"""

import argparse
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from config import OPENAI_KEY
from pipeline.db import BrandRaw, ContentCreatorRE, InstagramPost, SessionLocal, TestCreatorBrandPartnershipPost
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}
_TIMEOUT = 12

_MAX_PAGES = 5
_PAGE_TEXT_LIMIT = 6000   # chars of cleaned page text sent to the LLM per page
_MIN_TEXT_LEN = 40        # pages shorter than this are treated as "no useful info"

# Tags that never carry useful body copy
_NOISE_TAGS = ["script", "style", "nav", "header", "footer", "noscript", "iframe", "form", "aside", "svg"]

# Keywords used to rank homepage links by how likely they are to carry
# geographic-reach evidence (store locators, shipping/service-area pages, ...).
# Deliberately excludes generic e-commerce terms like "shop"/"find"/"near" —
# on large retailer nav/mega-menus those match dozens of unrelated product
# category links (e.g. "shop-all-categories") and crowd out the pages that
# actually carry location evidence within the 5-page budget.
#
# Includes common non-English equivalents (German/French/Spanish/Italian) —
# a large share of brands_raw sites are European, and an English-only list
# scores every link 0 on those sites, leaving the crawler to fall back to
# document order (which can land on a near-empty page like /search instead
# of a real contact/shipping/imprint page).
_LINK_KEYWORDS = (
    "store-locator", "storelocator", "find-a-store", "find-store", "our-stores",
    "location", "contact", "shipping", "international", "worldwide",
    "countr", "about", "our-story", "company", "nationwide", "region",
    # German
    "kontakt", "versand", "lieferung", "ueber-uns", "über-uns", "unternehmen",
    "impressum", "standort", "filiale", "laender", "länder",
    # French
    "contactez", "livraison", "a-propos", "à-propos", "societe", "société",
    "international", "pays", "magasins", "boutiques",
    # Spanish / Italian
    "contacto", "envio", "envío", "spedizione", "sobre-nosotros", "chi-siamo",
    "tiendas", "negozi", "paises", "países",
)

_ALLOWED_SCORES = {100, 90, 80, 70, 60, 40, 20, 0}
_SCORE_LABELS = {
    100: "outside_us_canada",
    90: "single_city_us_canada",
    80: "single_state_us_canada",
    70: "few_states_us_canada",
    60: "many_states_us_canada",
    40: "nationwide_us_canada",
    20: "multi_country_with_us_canada",
    0: "global",
}

_RUBRIC_TEXT = """100 = Doesn't operate in the US or Canada at all — footprint entirely in other country/countries (any scope there — one city, nationwide, several countries, doesn't matter, as long as none of it is the US/Canada)
90 = Single city/town, and that city is in the US or Canada
80 = Single state/province, and that state/province is in the US or Canada
70 = 2-5 states/provinces, all within the US and/or Canada
60 = 6-15 states/provinces, all within the US and/or Canada
40 = Most/all of the US or Canada (nationwide), with no presence in any other country
20 = Multiple countries where the US or Canada is one of them, PLUS at least one other country (e.g. US + Germany, or Canada + Japan) — not yet truly worldwide
0 = Global/worldwide presence

How to apply this — check in this order:
1. Does the brand operate ANYWHERE in the US or Canada (stores, shipping, service area, stated market)? If NO — it only operates in some other country or countries and nowhere in the US/Canada — the score is 100, no matter how narrow or wide its footprint is in those other countries. Stop here, skip the rest.
2. If it DOES operate somewhere in the US or Canada, is its overall presence truly global/worldwide (available essentially everywhere)? If yes, score 0.
3. If it operates in the US/Canada PLUS one or more other countries, but is not truly worldwide, score 20.
4. If it operates ONLY within the US and/or Canada (no other country at all), score by how much of the US/Canada it covers: a single city/town is 90, a single state/province is 80, 2-5 states/provinces is 70, 6-15 states/provinces is 60, most/all of the US or Canada (nationwide) is 40."""

# A page's LLM call is only allowed to end the crawl early when it reports
# confidence at/above this bar — otherwise the crawler keeps going, using
# every real page it can find, up to _MAX_PAGES. Trusting the model's own
# boolean self-report alone (an earlier version of this prompt) let it
# commit to a score after 2 thin pages while pages 3-5 were still
# available; a numeric floor enforced in code (not just requested in the
# prompt) is what actually guarantees "keep going unless truly sure".
_CONFIDENCE_STOP_THRESHOLD = 90

GEO_REACH_PAGE_PROMPT = """
You are analyzing a brand's website to determine its geographic market reach — how widely the brand actually operates (physical stores, service area, shipping coverage, stated target market), NOT how popular or well-known it is.

Score using EXACTLY one of these values:

""" + _RUBRIC_TEXT + """

Brand name: {brand_name}
Brand niche: {niche}
Instagram bio: {bio}

Locations/reach signals already confirmed from previously scraped pages of this same website (do not lose these — your answer must account for them too): {accumulated_locations}

Below is cleaned text scraped from ONE page of this brand's website ({page_url}). Look for concrete evidence of where the brand operates: physical addresses, store lists, "we ship to", service areas, "available in", named states/provinces/countries, phrases like "nationwide", "worldwide shipping", etc.

PAGE TEXT:
{page_text}

Critical distinction — reach vs. origin:
Do NOT treat country-of-origin, manufacturing-location, or "Made in X" / "Designed in X" claims as evidence of geographic reach. "Made in Germany" tells you where a product is manufactured, not where the brand sells, ships, or operates — a brand made in one country can sell in one city or worldwide. Likewise, a company's registered legal/HQ address (e.g. from an imprint/legal page) tells you where it's based, not how far it reaches. Only count evidence of where the brand actually SELLS, SHIPS TO, HAS STORES/SERVICE, or SERVES CUSTOMERS.

Instructions:
- "locations_found": a JSON array of any NEW city/state/province/country names or explicit reach statements found on THIS page (e.g. "Austin, Texas", "nationwide USA", "ships worldwide") that aren't already in the accumulated list above, and that reflect actual reach per the distinction above (not origin/HQ). Empty array if this page has nothing new and usable.
- Judge the full picture: accumulated_locations combined with this page's locations_found together.
- Be careful with "store locator" / "find a store" / interactive map pages: they very often default to showing only the stores nearest to a visitor (e.g. IP-geolocated) or a small default subset, NOT the complete list. A handful of stores clustered in one city/state on such a page is weak, inconclusive evidence — do NOT treat it as proof of a narrow single-city/single-state footprint. Prefer explicit aggregate statements instead ("1,900+ stores nationwide", "stores in all 50 states", "operating in 40 countries", "we ship worldwide").
- "confidence": an integer 0-100 — how confident you are, using accumulated_locations plus this page combined, that you could commit to a final score right now. A single passing mention (one country name, one city, an origin/HQ claim) is weak evidence — keep confidence LOW for that. Only report 90+ when the evidence is explicit and leaves little real doubt (e.g. a clear aggregate statement of reach, or a store/shipping list that itself spans the full claimed area). When in doubt, prefer a lower number — there are more pages left to check.
- "score": your best current single best-guess score per the rubric given everything so far, even if confidence is low. Use null only if there is truly no usable reach evidence at all yet.
- "reasoning": one short sentence.

Respond with ONLY valid JSON:
{"locations_found": [], "confidence": 0, "score": null, "reasoning": ""}
"""

GEO_REACH_FINAL_PROMPT = """
You already crawled this brand's website (up to 5 pages) and gathered the location evidence below. You must now commit to a final geographic reach score — do not ask for more pages, none are left.

Brand name: {brand_name}
Brand niche: {niche}
Instagram bio: {bio}

All location/reach signals found across the scraped pages: {accumulated_locations}

Score using EXACTLY one of these values:

""" + _RUBRIC_TEXT + """

Reminders:
- Country-of-origin / "Made in X" / manufacturing-location claims, and a company's registered legal/HQ address, are NOT evidence of reach — ignore any such signals in the list above when scoring. Only actual sell/ship/operate/service evidence counts.
- If these signals came mainly from a "store locator" style page, they may only be a nearby/default subset rather than the brand's full footprint — weigh that possibility, but you must still pick your best-supported answer now.
- If there is truly no usable reach evidence at all once origin/HQ-only signals are discounted, set "score" to null instead of guessing.

Respond with ONLY valid JSON:
{"score": null, "reasoning": ""}
"""


def _normalize_origin(website: str) -> str:
    website = website.strip()
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    parsed = urlparse(website)
    return f"{parsed.scheme}://{parsed.netloc}"


def _fetch_page(url: str) -> str | None:
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.debug("geo_reach: fetch failed for %s: %s", url, exc)
        return None


def _clean_page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    return text[:_PAGE_TEXT_LIMIT]


def _registrable_domain(netloc: str) -> str:
    """
    Best-effort eTLD+1 (e.g. "corporate.target.com" -> "target.com"), no
    public-suffix-list lookup — good enough to let a brand's own corporate/
    investor/about subdomain (a common home for aggregate "how many stores /
    countries" copy) be followed from a link on the main site, without
    pulling in a new dependency for the rare multi-part-TLD case this misses.
    """
    netloc = netloc.lower()
    parts = netloc.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else netloc


def _discover_location_links(html: str, origin: str) -> list[str]:
    """
    Real, same-brand internal links found on THIS page — never a path we
    invent ourselves. Links whose path/anchor text look location-relevant
    (store locator, contact, shipping, about, ...) are ranked first; every
    other real internal link on the page is kept after them as a fallback,
    so the crawler always has genuine pages to try next instead of guessing
    a URL that may not exist on the site.
    """
    soup = BeautifulSoup(html, "html.parser")
    origin_domain = _registrable_domain(urlparse(origin).netloc)

    scored: list[tuple[int, str]] = []
    seen_paths: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        full = urljoin(origin + "/", href)
        parsed = urlparse(full)
        if _registrable_domain(parsed.netloc) != origin_domain:
            continue  # external link (same-brand subdomains like corporate.<domain> are kept)

        path = parsed.path.rstrip("/") or "/"
        dedupe_key = f"{parsed.netloc.lower()}{path}"
        if dedupe_key in seen_paths:
            continue
        seen_paths.add(dedupe_key)

        link_text = a.get_text(" ", strip=True).lower()
        haystack = f"{path.lower()} {link_text}"
        score = sum(1 for kw in _LINK_KEYWORDS if kw in haystack)
        scored.append((score, f"{parsed.scheme}://{parsed.netloc}{path}"))

    # Stable sort: keyword-relevant links first, but same-score links (incl.
    # score 0) keep their original document order — so unmatched real links
    # still end up in the queue as a fallback, just behind the likelier ones.
    scored.sort(key=lambda t: -t[0])
    return [url for _, url in scored]


def _merge_locations(existing: list[str], new: list) -> list[str]:
    seen = {loc.strip().lower() for loc in existing}
    merged = list(existing)
    for loc in new:
        if not isinstance(loc, str):
            continue
        loc = loc.strip()
        if not loc or loc.lower() in seen:
            continue
        seen.add(loc.lower())
        merged.append(loc)
    return merged


def _normalize_score(value: object) -> int | None:
    if value is None:
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None
    return num if num in _ALLOWED_SCORES else None


def _normalize_confidence(value: object) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, num))


def _get_brand_bio(db: Session, brand_raw_id: int) -> str:
    post = (
        db.query(InstagramPost)
        .filter(InstagramPost.brand_raw_id == brand_raw_id, InstagramPost.biography.isnot(None))
        .order_by(InstagramPost.fetched_at.desc())
        .first()
    )
    return (post.biography or "").strip() if post else ""


def _call_geo_llm_page(brand: BrandRaw, bio: str, accumulated: list[str], page_url: str, page_text: str) -> dict:
    prompt = fill_template(
        GEO_REACH_PAGE_PROMPT,
        brand_name=brand.name or "unknown",
        niche=brand.niche or "unknown",
        bio=bio or "none provided",
        accumulated_locations=", ".join(accumulated) if accumulated else "none found yet",
        page_url=page_url,
        page_text=page_text,
    )
    result = call_gpt_json(prompt, context=f"geo reach page for brand_id={brand.id} url={page_url}")
    return result if isinstance(result, dict) else {}


def _call_geo_llm_final(brand: BrandRaw, bio: str, accumulated: list[str]) -> dict:
    prompt = fill_template(
        GEO_REACH_FINAL_PROMPT,
        brand_name=brand.name or "unknown",
        niche=brand.niche or "unknown",
        bio=bio or "none provided",
        accumulated_locations=", ".join(accumulated) if accumulated else "none found",
    )
    result = call_gpt_json(prompt, context=f"geo reach final decision for brand_id={brand.id}")
    return result if isinstance(result, dict) else {}


def _crawl_and_score(brand: BrandRaw, bio: str) -> tuple[int | None, str | None, list[str], list[dict]]:
    origin = _normalize_origin(brand.website)

    visited: set[str] = set()
    queue: list[str] = [origin]
    accumulated: list[str] = []
    pages_log: list[dict] = []
    final_score: int | None = None

    while queue and len(visited) < _MAX_PAGES:
        url = queue.pop(0)
        key = url.rstrip("/")
        if key in visited:
            continue
        visited.add(key)

        html = _fetch_page(url)
        if html is None:
            pages_log.append({"url": url, "status": "fetch_failed"})
            continue

        # Save this page's own real links as candidates for the next pages —
        # never a path we invent ourselves. Location-relevant links (store
        # locator, contact, shipping, about, ...) are queued first; every
        # other real internal link found on the page follows as a fallback.
        for link in _discover_location_links(html, origin):
            if link.rstrip("/") not in visited and link not in queue:
                queue.append(link)

        text = _clean_page_text(html)
        if len(text) < _MIN_TEXT_LEN:
            pages_log.append({"url": url, "status": "no_text"})
            continue

        result = _call_geo_llm_page(brand, bio, accumulated, url, text)
        new_locs = _merge_locations([], result.get("locations_found") or [])
        accumulated = _merge_locations(accumulated, new_locs)
        confidence = _normalize_confidence(result.get("confidence"))
        page_score = _normalize_score(result.get("score"))
        pages_log.append({
            "url": url, "status": "ok",
            "locations_found": new_locs, "confidence": confidence, "score_guess": page_score,
        })

        # Only end the crawl early on a high numeric confidence — a boolean
        # self-report alone let the model settle on 2 thin pages before
        # (see module docstring / _CONFIDENCE_STOP_THRESHOLD). Otherwise keep
        # going, using every real page available, up to _MAX_PAGES.
        if confidence >= _CONFIDENCE_STOP_THRESHOLD and page_score is not None:
            final_score = page_score
            break

    if final_score is None and accumulated:
        result = _call_geo_llm_final(brand, bio, accumulated)
        final_score = _normalize_score(result.get("score"))

    label = _SCORE_LABELS.get(final_score) if final_score is not None else None
    return final_score, label, accumulated, pages_log


def _score_brand_geo_reach(db: Session, brand: BrandRaw) -> None:
    bio = _get_brand_bio(db, brand.id)
    score, label, locations, pages = _crawl_and_score(brand, bio)

    brand.geo_reach_score = score
    brand.geo_reach_label = label
    brand.geo_reach_locations = locations
    brand.geo_reach_pages_scraped = pages
    brand.geo_reach_checked = True
    db.commit()

    logger.info(
        "geo_reach: id=%s name=%s -> score=%s label=%s pages_scraped=%d locations=%s",
        brand.id, brand.name, score, label, len(pages), locations,
    )


def enrich_geo_reach(
    db: Session,
    limit: int | None = None,
    brand_id: int | None = None,
    niche: str | None = None,
    creator_niche: str | None = None,
) -> int:
    """
    Score geographic reach for pending brands_raw rows that have a website.

    niche filters on the brand's OWN brands_raw.niche. creator_niche instead
    filters on the niche of the content_creator_re row(s) that discovered
    this brand — via test_creator_brand_partnership_posts.brand_raw_id ->
    content_creator_re.niche — for reverse-engineering-sourced brands that
    don't necessarily carry their own niche but were found through a
    creator in a given niche (e.g. every brand a "fitness" creator was
    caught partnering with). Both can be combined (AND) if given together.
    """
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping geo reach scoring")
        return 0

    query = db.query(BrandRaw).filter(
        BrandRaw.geo_reach_checked.is_(False),
        BrandRaw.website.isnot(None),
        BrandRaw.website != "",
    )
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    if niche is not None:
        query = query.filter(BrandRaw.niche == niche)
    if creator_niche is not None:
        creator_brand_ids = (
            db.query(TestCreatorBrandPartnershipPost.brand_raw_id)
            .join(
                ContentCreatorRE,
                TestCreatorBrandPartnershipPost.content_creator_re_id == ContentCreatorRE.id,
            )
            .filter(ContentCreatorRE.niche == creator_niche)
            .distinct()
        )
        query = query.filter(BrandRaw.id.in_(creator_brand_ids))
    if limit is not None:
        query = query.limit(limit)

    rows = query.all()
    if not rows:
        logger.info("Geo reach scoring: no rows pending")
        return 0

    logger.info("Geo reach scoring: processing %d row(s)", len(rows))
    updated = 0
    for row in rows:
        _score_brand_geo_reach(db, row)
        updated += 1

    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="Score brands_raw rows for geographic market reach.")
    parser.add_argument("--limit", type=int, default=None, help="Max number of brands to score in this run.")
    parser.add_argument("--brand-id", type=int, default=None, dest="brand_id", help="Limit to one brands_raw.id.")
    parser.add_argument("--niche", type=str, default=None, help="Limit to one brands_raw.niche value.")
    parser.add_argument(
        "--creator-niche", type=str, default=None, dest="creator_niche",
        help="Limit to brands discovered via a content_creator_re row with this niche.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        updated = enrich_geo_reach(
            db, limit=args.limit, brand_id=args.brand_id, niche=args.niche, creator_niche=args.creator_niche,
        )
        print(f"geo_reach: updated={updated}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
