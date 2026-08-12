"""
pipeline/enrichment/meta_ads_2.py

PROOF-OF-CONCEPT / TEST SCRIPT — not wired into the pipeline or orchestrator.

meta_ads.py depends on the Meta Graph API (ads_archive), which needs an app
that has passed Meta's App Review for Ad Library access — the verification
wall the user hit. This script tests an alternative that needs no API token
at all: driving the public Ad Library *website* with Playwright, the same
one a human uses at https://www.facebook.com/ads/library/.

brands_raw only has brand NAMES, not Facebook Page IDs, so this reproduces
the same name -> Page step a human does: type the name into the Ad
Library's own search box, capture the JSON its typeahead dropdown is built
from (search_type=page picker — see resolve_page()), and fuzzy-match the
results to pick a page_id. It then loads that Page's ads and intercepts the
GraphQL responses the page itself fires while scrolling (ad_archive_id,
creative text, dates, etc.) — no HTML scraping, no files written.

BRAND_NAME below is hardcoded for this test — edit it to try another brand.

Run directly:
    python3 -m pipeline.enrichment.meta_ads_2
"""

import json
import logging

from playwright.sync_api import sync_playwright
from rapidfuzz import fuzz

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

BRAND_NAME = "Iradah"
TOP_N = 10
SCROLL_ROUNDS = 5

# Ad Library's internal search endpoint — undocumented, called by the page
# itself on load, on every scroll-triggered "load more", and (with a
# different query shape) when the search box's typeahead dropdown fires.
_GRAPHQL_URL_HINT = "/api/graphql/"
_MATCH_THRESHOLD = 80


def _build_ads_url(page_id: str) -> str:
    return (
        "https://www.facebook.com/ads/library/"
        "?active_status=active&ad_type=all&country=ALL"
        "&is_targeted_country=false&media_type=all&search_type=page"
        "&sort_data[direction]=desc&sort_data[mode]=total_impressions"
        f"&view_all_page_id={page_id}"
    )


def _build_search_shell_url(country: str, seed_query: str) -> str:
    """A keyword-search results URL used only to load a page shell with a live search box."""
    from urllib.parse import quote
    return (
        "https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&country={country}"
        f"&is_targeted_country=false&media_type=all&q={quote(seed_query)}"
        "&search_type=keyword_unordered"
    )


def _dismiss_cookie_banner(page) -> None:
    for label in ("Allow all cookies", "Allow essential and optional cookies", "Only allow essential cookies"):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count() and btn.first.is_visible(timeout=2000):
                btn.first.click()
                return
        except Exception:
            continue


def resolve_page(page, brand_name: str) -> list[dict]:
    """
    Types brand_name into the Ad Library's search box and captures the
    typeahead_suggestions.page_results the site's own dropdown is built
    from — the exact candidate Pages a human would see and pick from.
    """
    candidates: list[dict] = []

    def _on_response(response):
        if _GRAPHQL_URL_HINT not in response.url:
            return
        try:
            body = response.text()
        except Exception:
            return
        if "typeahead_suggestions" not in body:
            return
        try:
            parsed = json.loads(body)
        except Exception:
            return
        results = (
            parsed.get("data", {})
            .get("ad_library_main", {})
            .get("typeahead_suggestions", {})
            .get("page_results", [])
        )
        if results:
            candidates.extend(results)

    page.on("response", _on_response)
    try:
        box = page.locator('input[type="search"]').first
        box.click(timeout=10000)
        page.wait_for_timeout(300)
        box.fill("")
        box.type(brand_name, delay=120)
        page.wait_for_timeout(2500)
    finally:
        page.remove_listener("response", _on_response)

    return candidates


def best_match(brand_name: str, candidates: list[dict]) -> dict | None:
    """
    Picks the candidate Page most likely to BE brand_name (typeahead also
    returns fan pages, sub-brands, and unrelated pages that merely share the
    word). Returns None (skip) rather than guess when:
      - no candidate scores above _MATCH_THRESHOLD, or
      - more than one DISTINCT page ties for the best match — e.g. two
        unrelated companies both literally named "Iradah". The name alone
        can't disambiguate them, so picking one would risk silently
        attributing another brand's ads.
    """
    if not candidates:
        return None
    by_page_id = {c["page_id"]: c for c in candidates}  # typeahead can repeat a page across calls

    name_lower = brand_name.strip().lower()
    exact = [c for c in by_page_id.values() if (c.get("name") or "").strip().lower() == name_lower]
    if exact:
        if len(exact) > 1:
            logger.warning("Skipping '%s' — %d different pages are all named exactly this: %s",
                            brand_name, len(exact), [c["page_id"] for c in exact])
            return None
        return exact[0]

    scored = sorted(by_page_id.values(), key=lambda c: fuzz.token_sort_ratio(brand_name, c.get("name") or ""), reverse=True)
    top_score = fuzz.token_sort_ratio(brand_name, scored[0].get("name") or "")
    if top_score < _MATCH_THRESHOLD:
        return None

    tied = [c for c in scored if fuzz.token_sort_ratio(brand_name, c.get("name") or "") == top_score]
    if len(tied) > 1:
        logger.warning("Skipping '%s' — %d different pages tie for the best name match: %s",
                        brand_name, len(tied), [c["page_id"] for c in tied])
        return None

    return scored[0]


def _extract_ads(obj) -> list[dict]:
    """Recursively pulls every ad record (any dict carrying 'ad_archive_id') out of a parsed GraphQL body."""
    found: list[dict] = []

    def _walk(node):
        if isinstance(node, dict):
            if "ad_archive_id" in node:
                found.append(node)
            for val in node.values():
                _walk(val)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return found


def _resolve_page_id(page, brand_name: str) -> tuple[str, dict] | None:
    shell_url = _build_search_shell_url("US", brand_name)
    page.goto(shell_url, wait_until="domcontentloaded", timeout=45000)
    _dismiss_cookie_banner(page)
    page.wait_for_timeout(2500)

    candidates = resolve_page(page, brand_name)
    if not candidates:  # typeahead is occasionally slow on the first pass
        candidates = resolve_page(page, brand_name)

    match = best_match(brand_name, candidates)
    return (match["page_id"], match) if match else None


def fetch_recent_ads(brand_name: str, top_n: int, scroll_rounds: int) -> list[dict]:
    ads_by_id: dict[str, dict] = {}

    def _on_ads_response(response):
        if _GRAPHQL_URL_HINT not in response.url:
            return
        try:
            body = response.text()
        except Exception:
            return
        if "ad_archive_id" not in body:
            return
        try:
            parsed = json.loads(body)
        except Exception:
            return
        for ad in _extract_ads(parsed):
            ads_by_id[ad["ad_archive_id"]] = ad

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 1000},
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = context.new_page()

        resolved = _resolve_page_id(page, brand_name)
        if not resolved:
            context.close()
            browser.close()
            return []
        page_id, match = resolved
        print(f"Resolved '{brand_name}' -> {match['name']} (page_id={page_id}, verification={match.get('verification')})")

        page.on("response", _on_ads_response)
        page.goto(_build_ads_url(page_id), wait_until="domcontentloaded", timeout=45000)
        _dismiss_cookie_banner(page)
        page.wait_for_timeout(3000)

        for _ in range(scroll_rounds):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1500)

        context.close()
        browser.close()

    ads = sorted(ads_by_id.values(), key=lambda a: a.get("start_date") or 0, reverse=True)
    return ads[:top_n]


def main() -> None:
    ads = fetch_recent_ads(BRAND_NAME, TOP_N, SCROLL_ROUNDS)

    if not ads:
        print(f"No ads found for '{BRAND_NAME}'.")
        return

    print(f"\nTop {len(ads)} most recent ads for {BRAND_NAME}:\n")
    for i, ad in enumerate(ads, 1):
        from datetime import datetime, timezone
        start = ad.get("start_date")
        start_str = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d") if start else "unknown"
        snapshot = ad.get("snapshot", {})
        body = (snapshot.get("body") or {}).get("text") or ""
        title = snapshot.get("title") or ""
        link = snapshot.get("link_url") or ""

        print(f"{i}. Ad ID: {ad['ad_archive_id']}  |  Started: {start_str}  |  Active: {ad.get('is_active')}")
        if title:
            print(f"   Title: {title}")
        if body:
            print(f"   Text:  {body[:200]}")
        if link:
            print(f"   Link:  {link}")
        print()


if __name__ == "__main__":
    main()
