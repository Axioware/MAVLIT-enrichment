"""
test_brand_collabs.py

Finds official Instagram brand collaborations using Meta Ad Library
Branded Content section via Apify Brand Collaboration Scraper.

Why this is better than @mention scraping:
  - Uses Meta's official paid partnership tags
  - Zero false positives
  - No login required
  - Official Meta transparency data

Usage:
  python3 test_brand_collabs.py

Requirements:
  pip install httpx --user
"""

import httpx
import json
from datetime import datetime, timedelta
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────

APIFY_TOKEN   = "apify_api_C9TSc0CmS3PLlny5n37pcNcQHLrDeg3ACh7Y"
ACTOR_ID      = "WHGjr2emdF9lMaefB"

BRAND_NAME    = "Nike"
BRAND_ID      = "17841400602400210"   # from Meta Ad Library URL ?id=...
DAYS_BACK     = 365                   # how far back to search
RESULTS_LIMIT = 100

# ── Helpers ───────────────────────────────────────────────────────────────────

def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def build_url(brand_id: str, brand_name: str, days_back: int = 365) -> str:
    """Build Meta Ad Library branded content URL with date range."""
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    return (
        f"https://www.facebook.com/ads/library/branded_content/"
        f"?id={brand_id}&query={brand_name}&target=instagram"
        f"&end_date={end_date}&start_date={start_date}"
    )


# ── Fetch from Apify ──────────────────────────────────────────────────────────

def fetch_collabs(brand_id: str, brand_name: str) -> list[dict]:
    """
    Call Apify Brand Collaboration Scraper.
    Returns list of collab items from Meta Ad Library.
    Status 200 or 201 both mean success.
    """
    url = build_url(brand_id, brand_name, DAYS_BACK)

    print(f"Searching Meta Ad Library branded content for {brand_name}...")
    print(f"URL : {url}")
    print()

    try:
        resp = httpx.post(
            f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={
                "startUrls":  [url],
                "maxResults": RESULTS_LIMIT,
            },
            timeout=180,
        )

        # 200 and 201 both mean success
        if resp.status_code in (200, 201):
            data = resp.json()
            if isinstance(data, list):
                return data
            print(f"Unexpected response format: {str(data)[:200]}")
            return []
        else:
            print(f"ERROR {resp.status_code}: {resp.text[:300]}")
            return []

    except Exception as e:
        print(f"ERROR: {e}")
        return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"Meta Ad Library Brand Collab Finder — {BRAND_NAME}")
    print("=" * 60)
    print()

    # ── Fetch all collab data ─────────────────────────────────────
    items = fetch_collabs(BRAND_ID, BRAND_NAME)

    if not items:
        print("No collab data returned.")
        print()
        print("Possible reasons:")
        print("  1. Brand has no official paid partnerships in this date range")
        print("  2. Brand ID or name is incorrect")
        print(f"  3. Check manually: https://www.facebook.com/ads/library/branded_content/?query={BRAND_NAME}&target=instagram")
        return

    print(f"Total collab items found : {len(items)}")
    print()

    # ── Parse and group by creator ────────────────────────────────
    by_creator: dict[str, list] = defaultdict(list)
    type_counts: dict[str, int] = defaultdict(int)

    for item in items:
        creator      = item.get("creator", {})
        partners     = item.get("brandPartners", [])
        content_type = item.get("type", "unknown")
        date         = item.get("dateCreated", "")
        link         = item.get("link", "")
        content_id   = item.get("id", "")

        creator_name = creator.get("name", "unknown")
        creator_url  = creator.get("link", "")

        type_counts[content_type] += 1

        by_creator[creator_name].append({
            "content_id":  content_id,
            "type":        content_type,
            "date":        date,
            "link":        link,
            "creator_url": creator_url,
            "partners":    [p.get("name") for p in partners],
        })

    # Sort creators by number of collabs
    sorted_creators = sorted(
        by_creator.items(),
        key=lambda x: len(x[1]),
        reverse=True,
    )

    # Sort all items by date
    sorted_items = sorted(
        items,
        key=lambda x: x.get("dateCreated", ""),
        reverse=True,
    )

    # ════════════════════════════════════════════════════════════
    # SECTION 1 — All Collabs Chronological
    # ════════════════════════════════════════════════════════════
    print("=" * 60)
    print("SECTION 1 — ALL COLLABS (newest first)")
    print("=" * 60)
    print()

    for i, item in enumerate(sorted_items, 1):
        creator      = item.get("creator", {})
        partners     = item.get("brandPartners", [])
        content_type = item.get("type", "unknown")
        date         = item.get("dateCreated", "")
        link         = item.get("link", "")

        partners_str = ", ".join([f"@{p['name']}" for p in partners])
        emoji = {"reel": "🎬", "post": "📸", "story": "📖"}.get(content_type, "📄")

        print(f"{i:3}. {emoji} {content_type.upper():6} — {date}")
        print(f"       Creator  : @{creator.get('name', 'unknown')}")
        print(f"       Partners : {partners_str}")
        print(f"       URL      : {link}")
        print()

    # ════════════════════════════════════════════════════════════
    # SECTION 2 — Unique Creators
    # ════════════════════════════════════════════════════════════
    print("=" * 60)
    print(f"SECTION 2 — UNIQUE CREATORS ({len(by_creator)} total)")
    print("=" * 60)
    print()

    for rank, (creator_name, posts) in enumerate(sorted_creators, 1):
        post_count   = len(posts)
        types        = [p["type"] for p in posts]
        dates        = sorted([p["date"] for p in posts if p["date"]])
        first_collab = dates[0]  if dates else "unknown"
        last_collab  = dates[-1] if dates else "unknown"
        creator_url  = posts[0]["creator_url"]

        type_summary = ", ".join([
            f"{types.count(t)}x {t}"
            for t in sorted(set(types))
        ])

        print(f"{rank:3}. @{creator_name}")
        print(f"       Total collabs : {post_count} ({type_summary})")
        print(f"       First collab  : {first_collab}")
        print(f"       Last collab   : {last_collab}")
        print(f"       Profile       : {creator_url}")

        for p in sorted(posts, key=lambda x: x["date"], reverse=True):
            emoji = {"reel": "🎬", "post": "📸", "story": "📖"}.get(p["type"], "📄")
            print(f"       {emoji} {p['date']} — {p['link']}")
        print()

    # ════════════════════════════════════════════════════════════
    # SECTION 3 — Stats Summary
    # ════════════════════════════════════════════════════════════
    print("=" * 60)
    print("SECTION 3 — STATS SUMMARY")
    print("=" * 60)
    print(f"  Brand           : {BRAND_NAME}")
    print(f"  Date range      : last {DAYS_BACK} days")
    print(f"  Total collabs   : {len(items)}")
    print(f"  Unique creators : {len(by_creator)}")
    print()
    print("  Content breakdown:")
    for ctype, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
        emoji = {"reel": "🎬", "post": "📸", "story": "📖"}.get(ctype, "📄")
        print(f"    {emoji} {ctype.capitalize():8} : {count}")
    print()

    if sorted_creators:
        top_name, top_posts = sorted_creators[0]
        print(f"  Most active creator : @{top_name} ({len(top_posts)} collabs)")
    print()

    # ── Save to JSON ──────────────────────────────────────────────
    output = {
        "brand":           BRAND_NAME,
        "brand_id":        BRAND_ID,
        "days_back":       DAYS_BACK,
        "total_collabs":   len(items),
        "unique_creators": len(by_creator),
        "content_types":   dict(type_counts),
        "creators": [
            {
                "username":     name,
                "profile_url":  posts[0]["creator_url"],
                "collab_count": len(posts),
                "posts":        posts,
            }
            for name, posts in sorted_creators
        ],
        "all_collabs": sorted_items,
    }

    output_file = f"brand_collabs_{BRAND_NAME.lower()}.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)

    print(f"💾 Results saved to: {output_file}")
    print()
    print("To search a different brand:")
    print("  1. Go to https://www.facebook.com/ads/library/branded_content/")
    print("  2. Search brand name → select Instagram")
    print("  3. Copy the URL — get the id= parameter")
    print("  4. Update BRAND_NAME, BRAND_ID at the top of this script")


if __name__ == "__main__":
    main()