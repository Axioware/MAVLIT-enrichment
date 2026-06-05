"""
pipeline/sources/wikidata.py

Fetches brand/company names from Wikidata using a two-step approach:
  1. wbsearchentities (x2) — niche keyword + "niche company" to capture
     both product/industry QIDs and company-type QIDs
  2. SPARQL — three complementary patterns:
       a) industry (P452) match
       b) product/service (P1056) match
       c) instance-of niche entity type — catches "record label",
          "footwear brand", "soft drink company", etc. directly

Uses httpx directly (not pipeline.http.fetch) because the SPARQL
endpoint requires Accept: application/sparql-results+json.
"""

import logging
import random
import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

_HEADERS = {
    "User-Agent": "MAVLIT-enrichment/1.0 (https://github.com/axioware/MAVLIT-enrichment)",
    "Accept": "application/sparql-results+json",
}


_SPARQL_SLEEP = (8.0, 15.0)   # longer gap before each SPARQL call
_API_SLEEP    = (1.5,  3.0)   # shorter gap before entity-search API calls
_MAX_RETRY_AFTER = 60          # never honour a Retry-After above this


@retry(
    wait=wait_exponential(min=15, max=90),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _get_json(url: str, params: dict) -> dict:
    # Be more polite to the SPARQL endpoint than to the search API
    lo, hi = _SPARQL_SLEEP if "sparql" in url else _API_SLEEP
    time.sleep(random.uniform(lo, hi))

    resp = httpx.get(url, params=params, headers=_HEADERS, timeout=60)

    if resp.status_code == 429:
        raw = int(resp.headers.get("Retry-After", 30))
        wait_secs = min(raw, _MAX_RETRY_AFTER)
        logger.warning(
            "Rate-limited (429) — Retry-After was %ds, capped to %ds", raw, wait_secs
        )
        time.sleep(wait_secs)
        resp.raise_for_status()

    resp.raise_for_status()
    return resp.json()


def _search_qids(niche: str) -> list[dict]:
    """
    Search for Wikidata QIDs using two passes:
      - niche keyword alone      → product/industry QIDs (e.g. "footwear", "music")
      - niche + " company"       → company-type QIDs (e.g. "footwear company",
                                   "record label", "soft drink company")

    Combining both passes gives SPARQL pattern 3 (instance-of) the right targets
    alongside patterns 1 & 2 (industry / product).
    """
    seen: set[str] = set()
    results: list[dict] = []

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
                results.append(item)
                
    logger.debug("QID search for '%s' → %d entities", niche, len(results))
    return results


def search_wikidata_brands(niche: str) -> list[str]:
    """
    Return a deduplicated list of company/brand names from Wikidata
    for any niche keyword (software, sports, footwear, music, drinks, …).

    SPARQL uses three complementary patterns so no niche is left uncovered:
      1. Industry match   — company whose P452 industry traces back to a niche QID
      2. Product match    — company that produces (P1056) something in the niche
      3. Entity-type match — company that IS a niche-specific type
                             (record label, footwear brand, soft drink company …)

    Results are scored by niche-keyword overlap so the most relevant names
    sort first; everything is returned and the pipeline's normalize/dedup
    is the final quality gate.
    """
    search_results = _search_qids(niche)
    if not search_results:
        logger.warning("Wikidata entity search returned nothing for '%s'", niche)
        return []

    values_block = " ".join(f"wd:{item['id']}" for item in search_results)
    niche_keywords = [item.get("label", "").lower() for item in search_results]

    # Patterns 1 & 2 use direct VALUES matching (no sub-traversal) to keep
    # the query cheap. Pattern 3 keeps one wdt:P279* walk which is fast
    # because it starts from a specific, small entity class.
    query = f"""
SELECT DISTINCT ?company ?companyLabel WHERE {{

  {{
    # Pattern 1: company whose industry (P452) is one of the niche QIDs
    ?company wdt:P31/wdt:P279* wd:Q4830453 .
    VALUES ?industry {{ {values_block} }}
    ?company wdt:P452 ?industry .
  }}

  UNION

  {{
    # Pattern 2: company that produces (P1056) something in the niche
    ?company wdt:P31/wdt:P279* wd:Q4830453 .
    VALUES ?product {{ {values_block} }}
    ?company wdt:P1056 ?product .
  }}

  UNION

  {{
    # Pattern 3: company IS a niche-specific type
    # (record label, footwear brand, soft drink company, sports club …)
    VALUES ?nicheClass {{ {values_block} }}
    ?company wdt:P31/wdt:P279* ?nicheClass .
    FILTER NOT EXISTS {{ ?company wdt:P31 wd:Q5 }}
  }}

  SERVICE wikibase:label {{
    bd:serviceParam wikibase:language "en".
  }}
}}
LIMIT 500
"""

    data = _get_json(SPARQL_ENDPOINT, {"query": query})

    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    for item in data["results"]["bindings"]:
        label = item.get("companyLabel", {}).get("value", "")
        uri = item.get("company", {}).get("value", "")

        if not label or not uri:
            continue
        if label.startswith("Q") and label[1:].isdigit():
            continue

        qid = uri.split("/")[-1]
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

        scored.append((score, label))

    scored.sort(key=lambda x: x[0], reverse=True)
    names = [label for _, label in scored]
    logger.info("Wikidata returned %d companies for niche '%s'", len(names), niche)
    return names
