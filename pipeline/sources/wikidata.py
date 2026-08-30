"""
pipeline/sources/wikidata.py

Fetches brand/company names (and official websites) from Wikidata.

Two-step process:
  1. wbsearchentities (×2) — niche keyword + "niche company" → QIDs
  2. SPARQL — two complementary patterns with optional P856 website fetch

Return value:
  list[dict] with keys:
    name    – company/brand label (str)
    website – official website URL from P856, or "" if not found (str)
    domain  – bare domain extracted from website, or "" if not found (str)

Geo filter fixes (issues 1–2):
  P159 (headquarters) uses a two-hop join through P17, because headquarters
    points to a city/building, not directly to a country.
  P276 (location) uses the same two-hop join.

Industry/product fixes (issues 3–4):
  P452 and P1056 patterns restore the wdt:P279* subclass walk so that
  sub-industries (e.g. "sportswear" under "fashion") still match.

Pattern 3 fix (issue 6):
  Requires ?company wdt:P31/wdt:P279* wd:Q4830453 so only business entities
  are returned, not events, associations, or research projects.

QID noise filter (issue 10):
  Descriptions that clearly indicate books, films, awards, or persons are
  dropped before building the VALUES block.

Location QID validation (issue 5):
  _resolve_location_qid fetches the top 5 candidates and picks the first
  one whose description suggests a geographic entity.
"""

import logging
import random
import time
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

_HEADERS = {
    "User-Agent": "MAVLIT-enrichment/1.0 (https://github.com/axioware/MAVLIT-enrichment)",
    "Accept": "application/sparql-results+json",
}

_SPARQL_SLEEP    = (8.0, 15.0)
_API_SLEEP       = (1.5,  3.0)
_MAX_RETRY_AFTER = 120  # cap for Retry-After header; raised to handle 1 req/min outage rules

#  QID noise filter 
# Use PHRASES not single words — single words ("scientific", "series", "event")
# cause false positives for legitimate niche terms like "technology" whose
# Wikidata description reads "practical application of scientific knowledge".
_NOISE_DESC_TERMS = frozenset([
    # Specific media works
    "novel by ", "book by ", "written by ",
    "film directed by", "directed by ",
    "album by ", "studio album", "live album", "compilation album",
    "song by ", "single by ", "music video",
    "video game developed", "role-playing game",
    "television series", "television show", "tv series",
    # Persons
    "human being", "born in ", "birth name",
    # Awards / ceremonies
    "award ceremony", "film festival", "music award", "prize awarded",
    "sports award",
    # One-off events
    "annual conference", "summit meeting", "trade fair",
    # Legislation
    "act of parliament", "law of ", "federal law", "regulation of ",
    # Fictional
    "fictional ",
])

#  Location QID validator 
_GEO_DESC_TERMS = frozenset([
    "country", "sovereign state", "republic", "kingdom", "federation",
    "empire", "union", "nation",
    "continent", "subregion", "region", "area",
    "city", "municipality", "capital",
    "state", "province", "district", "territory", "island",
])

_GEO_PROP_LABELS = {
    "P17":   "country",
    "P159":  "headquarters",
    "P276":  "location",
    "P2541": "operating area",
}


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
        # Strip leading 'www.' (and 'www2.', etc.) but preserve all other subdomains.
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.lower()
    except Exception:
        return ""


def _website_score(url: str) -> int:
    """
    Wikidata sometimes lists multiple P856 (official website) values for one
    entity with no reliable rank/language distinction — e.g. Maybelline
    (Q1351054) has both maybelline.com and the Brazil-specific
    maybelline.com.br at the same "normal" rank. Prefer a generic top-level
    domain (.com/.org/.net/.io) over one with a country-code suffix
    (.com.br, .co.uk, etc.), which is almost always the brand's global site
    rather than a regional mirror.
    """
    domain = _extract_domain(url)
    if not domain:
        return -1
    parts = domain.split(".")
    if len(parts) == 2 and parts[-1] in ("com", "org", "net", "io"):
        return 2
    if len(parts) == 2:
        return 1
    return 0


#  HTTP helper 

@retry(
    wait=wait_exponential(min=15, max=90),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _get_json(url: str, params: dict) -> dict:
    lo, hi = _SPARQL_SLEEP if "sparql" in url else _API_SLEEP
    time.sleep(random.uniform(lo, hi))

    timeout = 120 if "sparql" in url else 30
    resp = httpx.get(url, params=params, headers=_HEADERS, timeout=timeout)

    if resp.status_code == 429:
        raw = int(resp.headers.get("Retry-After", 60))
        wait_secs = min(raw, _MAX_RETRY_AFTER)
        logger.warning(
            "Rate-limited (429) — Retry-After %ds, waiting %ds then retrying",
            raw, wait_secs,
        )
        time.sleep(wait_secs)
        # Re-issue the request after the wait — don't just raise and let tenacity sleep
        # again on top; the Retry-After sleep IS the correct backoff for this error.
        resp = httpx.get(url, params=params, headers=_HEADERS, timeout=timeout)
        if resp.status_code == 429:
            logger.warning(
                "Still rate-limited after %ds — escalating to tenacity exponential backoff",
                wait_secs,
            )
            resp.raise_for_status()  # tenacity picks this up for further retries

    resp.raise_for_status()
    return resp.json()


#  QID helpers 

def _is_noisy_qid(item: dict) -> bool:
    """Return True if this QID looks like a book, film, person, event, etc."""
    desc = (item.get("description") or "").lower()
    return any(term in desc for term in _NOISE_DESC_TERMS)


def _is_geographic_entity(item: dict) -> bool:
    """Return True if this QID looks like a geographic place."""
    desc = (item.get("description") or "").lower()
    return any(term in desc for term in _GEO_DESC_TERMS)


def _search_qids(niche: str) -> list[dict]:
    """
    Two-pass entity search: niche keyword + 'niche company'.
    Results are filtered to remove books, films, persons, events, etc.

    Fallback: if the noise filter removes everything (e.g. every QID for
    "technology" happens to match a phrase), the full unfiltered set is
    returned so the SPARQL VALUES block is never empty.
    """
    seen: set[str] = set()
    all_results:      list[dict] = []
    filtered_results: list[dict] = []

    for term in [niche, f"{niche} company"]:
        data = _get_json(WIKIDATA_API, {
            "action": "wbsearchentities",
            "search": term,
            "language": "en",
            "format": "json",
            "limit": 15,
        })
        for item in data.get("search", []):
            if item["id"] not in seen:
                seen.add(item["id"])
                all_results.append(item)
                if not _is_noisy_qid(item):
                    filtered_results.append(item)

    if not filtered_results:
        logger.warning(
            "Noise filter removed ALL QIDs for '%s' — using unfiltered set (%d items)",
            niche, len(all_results),
        )
        return all_results

    logger.debug(
        "QID search for '%s': %d raw → %d after noise filter",
        niche, len(all_results), len(filtered_results),
    )
    return filtered_results


def _resolve_location_qid(name: str) -> str | None:
    """
    Resolve a human-readable location name to its Wikidata QID.
    Fetches top 5 candidates and picks the first geographic entity.
    Falls back to the first result if none is clearly geographic.
    (Fix #5 — ambiguous location names like 'Georgia')
    """
    if not name:
        return None
    try:
        data = _get_json(WIKIDATA_API, {
            "action": "wbsearchentities",
            "search": name,
            "language": "en",
            "format": "json",
            "limit": 5,
        })
        candidates = data.get("search", [])
        # Prefer a result whose description confirms it is a geographic entity
        for item in candidates:
            if _is_geographic_entity(item):
                logger.info(
                    "Resolved location '%s' → %s (%s) [geo-validated]",
                    name, item["id"], item.get("label", name),
                )
                return item["id"]
        # Fall back to first result
        if candidates:
            item = candidates[0]
            logger.warning(
                "Resolved location '%s' → %s (%s) [no geo description found, using first result]",
                name, item["id"], item.get("label", name),
            )
            return item["id"]
        logger.warning("Could not resolve location '%s' to any QID", name)
        return None
    except Exception:
        logger.exception("QID resolution failed for '%s'", name)
        return None


def _build_geo_block(geo_qids: dict[str, str]) -> str:
    """
    Build the SPARQL geo-filter block (OR logic across all provided properties).

    P159 and P276 use a two-hop join through wdt:P17 because they point to
    a specific city/building, not directly to a country.  (Fixes #1 and #2)
    """
    if not geo_qids:
        return ""

    branches: list[str] = []
    for prop, qid in geo_qids.items():
        label = _GEO_PROP_LABELS.get(prop, prop)
        if prop == "P159":
            # Headquarters → city → country
            branches.append(
                f"    {{ ?company wdt:P159 ?hq . ?hq wdt:P17 wd:{qid} . }}  # {label} in country"
            )
        elif prop == "P276":
            # Location → place → country
            branches.append(
                f"    {{ ?company wdt:P276 ?loc . ?loc wdt:P17 wd:{qid} . }}  # {label} in country"
            )
        else:
            branches.append(
                f"    {{ ?company wdt:{prop} wd:{qid} . }}  # {label}"
            )

    union_body = "\n    UNION\n".join(branches)
    return f"\n  # Geographic filter (OR logic)\n  {{\n{union_body}\n  }}\n"


#  Main public function 

def search_wikidata_brands(
    niche: str,
    country: str | None = None,
    headquarters: str | None = None,
    location: str | None = None,
    operating_area: str | None = None,
) -> list[dict]:
    """
    Return deduplicated entity records from Wikidata for any niche.

    Each record is a dict:
      {
        "wikidata_id":   str,   # Wikidata QID (e.g. "Q483551")
        "name":          str,   # entity label
        "website":       str,   # official website (P856), or ""
        "domain":        str,   # bare domain extracted from website, or ""
        "description":   str,   # Wikidata description (English), or ""
        "entity_type":   str,   # label of first P31 (instance-of) value, or ""
        "wikipedia_url": str,   # always "" — schema:about omitted (causes 500s on WDQS)
      }

    SPARQL patterns:
      A. P31 (instance-of) direct match
      B. P452 (industry) / P1056 (product) direct match

    Both patterns fetch P856 (official website) via OPTIONAL.

    Optional geo filters (OR logic, fixes #1 & #2):
      country        — P17   direct match
      headquarters   — P159  two-hop via P17 (hq city → country)
      location       — P276  two-hop via P17 (location → country)
      operating_area — P2541 direct match
    """
    search_results = _search_qids(niche)
    if not search_results:
        logger.warning("Wikidata entity search returned nothing for '%s'", niche)
        return []

    values_block  = " ".join(f"wd:{item['id']}" for item in search_results)
    niche_keywords = [item.get("label", "").lower() for item in search_results]

    # Resolve geo param names → validated QIDs
    raw_geo = {
        "P17":   country,
        "P159":  headquarters,
        "P276":  location,
        "P2541": operating_area,
    }
    geo_qids: dict[str, str] = {}
    for prop, name in raw_geo.items():
        if name:
            qid = _resolve_location_qid(name)
            if qid:
                geo_qids[prop] = qid

    geo_block = _build_geo_block(geo_qids)
    if geo_qids:
        logger.info("Geo filter active: %s", {_GEO_PROP_LABELS[p]: q for p, q in geo_qids.items()})

    # Cap to 8 QIDs — keeps VALUES blocks small and queries fast
    capped = values_block.split()[:8]
    vb = " ".join(capped)

    #  Query A — direct wdt:P31 instance-of match 
    # ?type IS the instanceOf, so BIND gives ?instanceOfLabel via the label service.
    # ?companyDescription comes automatically from the label service.
    # schema:about (Wikipedia URL) is intentionally omitted — it causes 500/timeout
    # errors on Wikidata's SPARQL endpoint due to high cost of that triple pattern.
    query_a = f"""
SELECT DISTINCT ?company ?companyLabel ?companyDescription ?instanceOf ?instanceOfLabel ?website WHERE {{
  VALUES ?type {{ {vb} }}
  ?company wdt:P31 ?type .
  BIND(?type AS ?instanceOf)
  FILTER NOT EXISTS {{ ?company wdt:P31 wd:Q5 }}
  OPTIONAL {{ ?company wdt:P856 ?website . }}
  {geo_block}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 500
"""

    #  Query B — direct P452/P1056 industry/product match 
    # P31 is OPTIONAL here (no VALUES constraint), so multiple P31 values can
    # produce duplicate rows per entity — handled in Python by taking the first
    # non-empty value for each field per QID.
    # schema:about omitted for the same reason as Query A.
    query_b = f"""
SELECT DISTINCT ?company ?companyLabel ?companyDescription ?instanceOf ?instanceOfLabel ?website WHERE {{
  {{
    {{ VALUES ?ind {{ {vb} }} ?company wdt:P452  ?ind . }}
    UNION
    {{ VALUES ?prd {{ {vb} }} ?company wdt:P1056 ?prd . }}
  }}
  OPTIONAL {{ ?company wdt:P856 ?website . }}
  OPTIONAL {{ ?company wdt:P31 ?instanceOf . }}
  {geo_block}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 500
"""

    bindings: list[dict] = []

    for label, query in [("A (instance-of)", query_a), ("B (P452/P1056)", query_b)]:
        try:
            data = _get_json(SPARQL_ENDPOINT, {"query": query})
            rows = data["results"]["bindings"]
            logger.info("Query %s returned %d rows", label, len(rows))
            bindings.extend(rows)
        except Exception:
            logger.exception("SPARQL query %s failed — skipping", label)

    # Accumulate per-QID metadata (first non-empty value wins for each field).
    # Query B may produce multiple rows per QID (different P31 values), so we
    # collect across all rows before deciding which entities to include.
    qid_meta: dict[str, dict] = {}
    scored: list[tuple[int, str, str]] = []   # (score, label, qid)
    seen: set[str] = set()

    for item in bindings:
        label       = item.get("companyLabel",       {}).get("value", "")
        uri         = item.get("company",             {}).get("value", "")
        website     = item.get("website",             {}).get("value", "")
        description = item.get("companyDescription",  {}).get("value", "")
        inst_label  = item.get("instanceOfLabel",     {}).get("value", "")

        if not label or not uri:
            continue
        if label.startswith("Q") and label[1:].isdigit():
            continue

        qid = uri.split("/")[-1]

        # Merge metadata — first non-empty value per field
        meta = qid_meta.setdefault(qid, {
            "website": "", "description": "", "entity_type": "",
        })
        if website and (not meta["website"] or _website_score(website) > _website_score(meta["website"])):
            meta["website"] = website
        if description and not meta["description"]: meta["description"] = description
        if inst_label  and not meta["entity_type"]: meta["entity_type"] = inst_label

        if qid in seen:
            continue
        seen.add(qid)

        score = 0
        label_lower = label.lower()
        for kw in niche_keywords:
            if kw and kw in label_lower:
                score += 2
        if any(x in label_lower for x in ["inc", "ltd", "corp", "group", "company", "co"]):
            score += 1

        scored.append((score, label, qid))

    scored.sort(key=lambda x: x[0], reverse=True)

    records: list[dict] = []
    for _score, name, qid in scored:
        meta = qid_meta.get(qid, {})
        website = meta.get("website", "")
        records.append({
            "wikidata_id":   qid,
            "name":          name,
            "website":       website,
            "domain":        _extract_domain(website),
            "description":   meta.get("description", ""),
            "entity_type":   meta.get("entity_type", ""),
            "wikipedia_url": "",   # not fetched via SPARQL; schema:about causes 500s
        })

    website_count = sum(1 for r in records if r["website"])
    logger.info(
        "Wikidata returned %d entities for niche '%s' (%d with P856 website)",
        len(records), niche, website_count,
    )
    return records