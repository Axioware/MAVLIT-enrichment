"""
test_searxng_brand_search.py

Standalone test script (not part of the enrichment pipeline) for the real
SearXNG + LLM website-resolution logic in
pipeline/enrichment_re/brand_instagram_profile.py — imports and calls
_searxng_search() and _resolve_from_search() directly, rather than
reimplementing the same thing separately, so this exercises the actual
production code path (including the brand_website_search_pick LLM call,
DB-editable via /admin/prompt).

Given a bare Instagram handle, this:
  1. Runs _searxng_search("<handle> official website") and prints the raw
     top-5 SearXNG results (title/url/snippet), before the LLM sees them.
  2. Runs _resolve_from_search() — the same call brand_instagram_profile.py
     makes when its Instagram-profile tier finds no website — and prints
     the LLM's final website + name decision.

Requires:
  - A local SearXNG instance with JSON output enabled (config.SEARXNG_URL,
    defaults to http://localhost:8080). Enable JSON in SearXNG's
    settings.yml:
        search:
          formats:
            - html
            - json
    then restart SearXNG.
  - OPENAI_KEY set in .env — without it, call_gpt_json() returns {} and the
    LLM decision step below will show no website/name (see
    pipeline/helpers/gpt_llm.py).

Run:
    python3 test_searxng_brand_search.py
"""

import logging

from dotenv import load_dotenv
load_dotenv()

from pipeline.db import SessionLocal
from pipeline.enrichment_re.brand_instagram_profile import _resolve_from_search, _searxng_search

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def print_raw_results(query: str, results: list[dict]) -> None:
    """Print the query and the raw SearXNG results (title/url/snippet) before the LLM sees them."""
    print(f"Search query: {query}")
    print(f"Total results returned: {len(results)}\n")

    if not results:
        print("No results found.")
        return

    print(f"Top {len(results)} result(s) (raw, before LLM pick):\n")
    for i, r in enumerate(results, start=1):
        print(f"{i}. {r['url']}")
        print(f"   {r['title']}")
        if r.get("snippet"):
            print(f"   {r['snippet'][:150]}")
        print()


def main() -> None:
    instagram_handle = "simplymander"
    bio = ""          # standalone test — no real scraped profile; empty is fine as LLM context
    external_url = ""  # ditto
    saved_name = ""    # no prior name to confirm/correct in this standalone test

    query = f"{instagram_handle} official website"
    print(f"Searching for: {query}\n")

    # Same function brand_instagram_profile.py uses — shows the raw
    # candidates before the LLM narrows them down to one.
    results = _searxng_search(query)
    print_raw_results(query, results)

    # Full pipeline logic: SearXNG search + brand_website_search_pick LLM call.
    # Calls _searxng_search again internally (results should match above) —
    # kept as two separate calls so this script reuses _resolve_from_search
    # completely unmodified, exactly as brand_instagram_profile.py calls it.
    db = SessionLocal()
    try:
        website, source, name = _resolve_from_search(db, instagram_handle, bio, external_url, saved_name)
    finally:
        db.close()

    print("LLM decision:")
    print(f"  website: {website or 'none — no confident match'}")
    print(f"  source:  {source or 'n/a'}")
    print(f"  name:    {name or 'n/a'}")


if __name__ == "__main__":
    main()
