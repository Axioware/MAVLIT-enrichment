"""
test_searxng_brand_search.py

Standalone test script (not part of the enrichment pipeline) for a local
SearXNG instance running at http://localhost:8080. Given an Instagram
handle, builds an "<handle> official website" query, sends it to SearXNG's
/search endpoint, and prints the top 5 result URLs.

SearXNG only returns JSON if JSON output is enabled on the instance —
by default only "html" is allowed. Enable it in your SearXNG settings.yml:

    search:
      formats:
        - html
        - json

then restart SearXNG. Without this, every request below will fail with a
403 (see _fetch_search_results' error handling for that specific case).

Run:
    python3 test_searxng_brand_search.py
"""

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_SEARXNG_URL = "http://localhost:8080/search"
_REQUEST_TIMEOUT = 10.0  # seconds
_TOP_N = 5


def build_search_query(instagram_handle: str) -> str:
    """Turn a bare Instagram handle into an "official website" search query."""
    return f"{instagram_handle} official website"


def fetch_search_results(query: str) -> list[dict[str, Any]]:
    """
    GET query against the local SearXNG instance and return its raw
    "results" list (each item is a dict with at least a "url" key, per
    SearXNG's JSON API format).

    Returns [] on any failure (connection refused, timeout, non-2xx status,
    malformed JSON) — logged via `logging`, never printed directly, so
    callers can decide how to handle an empty result set.
    """
    params = {"q": query, "format": "json"}

    try:
        response = httpx.get(_SEARXNG_URL, params=params, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
    except httpx.TimeoutException:
        logger.error("Request to SearXNG timed out after %.0fs (query=%r)", _REQUEST_TIMEOUT, query)
        return []
    except httpx.ConnectError:
        logger.error(
            "Could not connect to SearXNG at %s — is it running? (query=%r)",
            _SEARXNG_URL, query,
        )
        return []
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            logger.error(
                "SearXNG returned 403 for query=%r — JSON output format is likely "
                "disabled on this instance. Add 'json' to search.formats in "
                "settings.yml and restart SearXNG.",
                query,
            )
        else:
            logger.error(
                "SearXNG returned HTTP %d for query=%r: %s",
                exc.response.status_code, query, exc,
            )
        return []
    except httpx.RequestError as exc:
        logger.error("Request to SearXNG failed for query=%r: %s", query, exc)
        return []

    try:
        data = response.json()
    except ValueError:
        logger.error("SearXNG response for query=%r was not valid JSON", query)
        return []

    results = data.get("results")
    if not isinstance(results, list):
        logger.error("SearXNG response for query=%r had no usable 'results' list", query)
        return []

    return results


def extract_top_urls(results: list[dict[str, Any]], top_n: int = _TOP_N) -> list[str]:
    """
    Pull URLs out of SearXNG result items, skipping empty ones and
    deduplicating while preserving order, capped at top_n.
    """
    seen: set[str] = set()
    urls: list[str] = []

    for item in results:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        # urlparse just to sanity-check it's a real URL, not to transform it —
        # a malformed "url" field (missing scheme/netloc) is skipped rather
        # than shown as a broken/misleading result.
        parsed = urlparse(url)
        if not (parsed.scheme and parsed.netloc):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= top_n:
            break

    return urls


def print_results(query: str, total_results: int, urls: list[str]) -> None:
    """Print the query, result count, and top URLs in the requested format."""
    print(f"Search query: {query}")
    print(f"Total results returned: {total_results}")
    print()

    if not urls:
        print("No URLs found.")
        return

    print(f"Top {len(urls)} URL(s):\n")
    for i, url in enumerate(urls, start=1):
        print(f"{i}. {url}")


def main() -> None:
    instagram_handle = "nike"

    query = build_search_query(instagram_handle)
    print(f"Searching for: {query}\n")

    results = fetch_search_results(query)
    urls = extract_top_urls(results, top_n=_TOP_N)

    print_results(query, total_results=len(results), urls=urls)


if __name__ == "__main__":
    main()
