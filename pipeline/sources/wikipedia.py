"""
pipeline/sources/wikipedia.py

Scrapes brand/company names (and official websites) from Wikipedia category pages.

Dynamically finds categories for any niche via the Wikipedia search API,
scrapes them with BeautifulSoup, filters out human/person articles and
individual products, then resolves each article's Wikidata item via
wbgetentities to fetch P856 (official website).

Return value:
  list[dict] with keys:
    name    – Wikipedia article title used as the brand/company name (str)
    website – official website URL from Wikidata P856, or "" if not found (str)
    domain  – bare domain extracted from website, or "" if not found (str)
"""

import logging
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from pipeline.http import fetch

logger = logging.getLogger(__name__)

WIKI_BASE = "https://en.wikipedia.org"
WIKI_API  = "https://en.wikipedia.org/w/api.php"
WD_API    = "https://www.wikidata.org/w/api.php"

_HEADERS = {
    "User-Agent": "MAVLIT-enrichment/1.0 (https://github.com/axioware/MAVLIT-enrichment)",
}

#  Person markers 
_PERSON_CATEGORY_MARKERS = frozenset([
    # Biographical
    "living people", " births", " deaths",
    "people from", "people by", "people of",
    # Occupations
    "sportspeople", "athletes", "footballers", "cricketers", "basketballers",
    "swimmers", "runners", "gymnasts",
    "musicians", "singers", "rappers", "composers", "conductors",
    "actors", "actresses", "film directors", "screenwriters",
    "politicians", "businesspeople", "entrepreneurs",
    "models (people)", "fashion models",
    # Gender / biographical categories
    "male ", "female ", " women", " men",
    "fictional characters",
])

#  Non-company / noise markers 
_NOISE_CATEGORY_MARKERS = frozenset([
    # Publications
    "books by", "novels by", "magazines", "newspapers", "journals",
    "academic journals", "periodicals", "publications",
    # Films / TV
    "films by", "films set", "television series", "television films",
    "animated series", "documentary films",
    # Music (individual works, not labels)
    " albums", " soundtracks", " singles", " songs by", " discographies",
    # Awards / competitions
    "award", "prize", "competition winners", "film festival",
    # Events
    "conferences", "summits", "festivals", "ceremonies", "fairs",
    # Legislation / policy
    "legislation", " laws", "regulations", "treaties",
    # Other non-company
    "fictional companies", "fictional organizations",
    "video games", "video game franchises",
])

#  Individual product markers 
_PRODUCT_CATEGORY_MARKERS = frozenset([
    "products introduced",
    "product lines",
    "clothing items",
    "shoe models",
    "sneaker models",
    "drink brands",
    "food products",
    "automobile models",
    "mobile phones",
    "tablet computers",
    "software",        # individual software titles, not software companies
])


#  URL / domain helpers 

def _extract_domain(url: str) -> str:
    """
    Extract a bare domain from a URL string.

    Examples:
      "https://www.nike.com/us/"  → "nike.com"
      "http://example.co.uk"      → "example.co.uk"
      ""                          → ""
    """
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.lower()
    except Exception:
        return ""


#  Category / noise filters 

def _is_person(categories: list[str]) -> bool:
    for cat in categories:
        c = cat.lower()
        if any(marker in c for marker in _PERSON_CATEGORY_MARKERS):
            return True
    return False


def _is_noise(categories: list[str]) -> bool:
    for cat in categories:
        c = cat.lower()
        if any(marker in c for marker in _NOISE_CATEGORY_MARKERS):
            return True
    return False


def _is_product(categories: list[str]) -> bool:
    for cat in categories:
        c = cat.lower()
        if any(marker in c for marker in _PRODUCT_CATEGORY_MARKERS):
            return True
    return False


def _filter_humans_and_products(titles: list[str]) -> list[str]:
    """
    Drop human/person articles and individual product articles by checking
    their Wikipedia categories in batches of 50.
    On API failure for a batch, that batch is kept (fail-open).
    """
    if not titles:
        return []

    valid: list[str] = []

    for i in range(0, len(titles), 50):
        batch = titles[i : i + 50]
        try:
            resp = httpx.get(
                WIKI_API,
                params={
                    "action": "query",
                    "titles": "|".join(batch),
                    "prop": "categories",
                    "cllimit": 30,
                    "format": "json",
                },
                headers=_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})

            for page in pages.values():
                title = page.get("title", "")
                if not title:
                    continue
                cats = [c["title"] for c in page.get("categories", [])]

                if _is_person(cats):
                    logger.debug("Filtered (human): %s", title)
                    continue
                if _is_noise(cats):
                    logger.debug("Filtered (noise): %s", title)
                    continue
                if _is_product(cats):
                    logger.debug("Filtered (product): %s", title)
                    continue

                valid.append(title)

        except Exception:
            logger.exception("Category filter API call failed for batch %d–%d", i, i + 50)
            valid.extend(batch)  # fail-open: keep batch on error

    logger.info("Category filter: %d → %d after removing humans/products", len(titles), len(valid))
    return valid


#  Wikidata entity lookup for Wikipedia titles 

def _fetch_wikidata_for_titles(titles: list[str]) -> dict[str, dict]:
    """
    Given Wikipedia article titles, return a mapping
    { title → {wikidata_id, website, description} }
    using Wikidata's wbgetentities API (P856 for website, descriptions for text).

    Works in batches of 50 (Wikidata API limit).
    Titles with no Wikidata entity are absent from the returned dict.
    On API failure for a batch, that batch is skipped (fail-safe).
    """
    result: dict[str, dict] = {}

    for i in range(0, len(titles), 50):
        batch = titles[i : i + 50]
        try:
            resp = httpx.get(
                WD_API,
                params={
                    "action":  "wbgetentities",
                    "sites":   "enwiki",
                    "titles":  "|".join(batch),
                    "props":   "claims|sitelinks|descriptions",
                    "format":  "json",
                },
                headers=_HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            entities = resp.json().get("entities", {})

            for entity in entities.values():
                if entity.get("id", "").startswith("-"):
                    continue

                enwiki_title = (
                    entity.get("sitelinks", {})
                          .get("enwiki", {})
                          .get("title", "")
                )
                if not enwiki_title:
                    continue

                entity_id = entity.get("id", "")

                p856_claims = entity.get("claims", {}).get("P856", [])
                url = ""
                if p856_claims:
                    url = (
                        p856_claims[0]
                        .get("mainsnak", {})
                        .get("datavalue", {})
                        .get("value", "")
                    )

                description = (
                    entity.get("descriptions", {})
                          .get("en", {})
                          .get("value", "")
                )

                result[enwiki_title] = {
                    "wikidata_id": entity_id,
                    "website":     url,
                    "description": description,
                }
                if url:
                    logger.debug("P856 resolved: %s → %s (%s)", enwiki_title, url, entity_id)

        except Exception:
            logger.exception(
                "Wikidata fetch failed for Wikipedia titles batch %d–%d", i, i + 50,
            )

    logger.info(
        "Wikidata entity lookup: %d titles → %d resolved (%d with website)",
        len(titles),
        len(result),
        sum(1 for v in result.values() if v["website"]),
    )
    return result


#  Wikipedia scraping helpers 

def _find_category_urls(niche: str, limit: int = 3) -> list[str]:
    """
    Search Wikipedia namespace 14 (Category) for brand/company category pages.
    Geographic filtering is intentionally omitted here — Wikipedia's text
    search produces completely wrong results when geo terms are appended.
    Wikidata (structured data) handles geographic filtering instead.
    """
    resp = httpx.get(
        WIKI_API,
        params={
            "action":      "query",
            "list":        "search",
            "srsearch":    f"{niche} brands OR {niche} companies",
            "srnamespace": 14,
            "srlimit":     limit,
            "format":      "json",
        },
        headers=_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json().get("query", {}).get("search", [])
    urls = [
        f"{WIKI_BASE}/wiki/{r['title'].replace(' ', '_')}"
        for r in results
    ]
    logger.info("Wikipedia category search → %s", [r["title"] for r in results])
    return urls


def _scrape_category(
    category_url: str,
    _visited: set[str] | None = None,
    _subcat_depth: int = 0,
    max_subcat_depth: int = 1,
) -> list[str]:
    """
    Scrape raw article titles from a Wikipedia category page.
    Follows 'next page' pagination and recurses into subcategories
    up to max_subcat_depth levels deep.
    """
    if _visited is None:
        _visited = set()
    if category_url in _visited:
        return []
    _visited.add(category_url)

    html = fetch(category_url)
    soup = BeautifulSoup(html, "html.parser")

    titles = [a.get_text() for a in soup.select("#mw-pages .mw-category-group li a")]

    next_link = soup.find("a", string=lambda t: t and "next page" in t.lower())
    if next_link and next_link.get("href"):
        titles += _scrape_category(
            WIKI_BASE + next_link["href"],
            _visited,
            _subcat_depth,
            max_subcat_depth,
        )

    if _subcat_depth < max_subcat_depth:
        for link in soup.select("#mw-subcategories .mw-category-group li a"):
            if not link.get("href"):
                continue
            titles += _scrape_category(
                WIKI_BASE + link["href"],
                _visited,
                _subcat_depth + 1,
                max_subcat_depth,
            )

    return titles


#  Main public function 

def search_wikipedia_brands(niche: str, **_kwargs) -> list[dict]:
    """
    Find Wikipedia categories for `niche`, scrape all article titles,
    filter out humans and individual products, then resolve Wikidata metadata.

    Returns a list of dicts:
      {
        "wikidata_id":   str,   # Wikidata QID, or ""
        "name":          str,   # Wikipedia article title
        "website":       str,   # official website URL (P856), or ""
        "domain":        str,   # bare domain, or ""
        "description":   str,   # Wikidata English description, or ""
        "wikipedia_url": str,   # canonical English Wikipedia URL
      }

    Any extra kwargs (geo filters) are accepted but ignored —
    geographic filtering is delegated to Wikidata's SPARQL query.
    """
    category_urls = _find_category_urls(niche)
    if not category_urls:
        logger.warning("No Wikipedia categories found for niche '%s'", niche)
        return []

    raw_titles: list[str] = []
    visited: set[str] = set()

    for url in category_urls:
        logger.info("Scraping Wikipedia: %s", url)
        try:
            titles = _scrape_category(url, visited)
            raw_titles.extend(titles)
            logger.info("  → %d raw titles from %s", len(titles), url)
        except Exception:
            logger.exception("Failed to scrape %s", url)

    seen: set[str] = set()
    unique_titles: list[str] = []
    for t in raw_titles:
        if t not in seen:
            seen.add(t)
            unique_titles.append(t)

    filtered_titles = _filter_humans_and_products(unique_titles)

    wikidata_map = _fetch_wikidata_for_titles(filtered_titles)

    records: list[dict] = []
    for title in filtered_titles:
        meta    = wikidata_map.get(title, {})
        website = meta.get("website", "")
        records.append({
            "wikidata_id":   meta.get("wikidata_id", ""),
            "name":          title,
            "website":       website,
            "domain":        _extract_domain(website),
            "description":   meta.get("description", ""),
            "wikipedia_url": f"{WIKI_BASE}/wiki/{title.replace(' ', '_')}",
        })

    website_count = sum(1 for r in records if r["website"])
    logger.info(
        "Wikipedia returned %d entities for niche '%s' (%d with P856 website)",
        len(records), niche, website_count,
    )
    return records