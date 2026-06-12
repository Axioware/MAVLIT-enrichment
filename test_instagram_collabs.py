"""
test_instagram_all_collabs.py

Reads ALL posts of a brand and finds total unique collaborators.

Strategy:
  - Uses Apify instagram-profile-scraper to get posts in batches
  - Extracts @mentions from every post caption
  - Counts unique usernames mentioned across all posts
  - Classifies each as collab or plain mention
  - Shows per-post breakdown of collabs and mentions

Usage:
  python3 test_instagram_all_collabs.py

Requirements:
  pip install httpx --user
"""

import httpx
import json
import re
from collections import defaultdict
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

APIFY_TOKEN  = "apify_api_C9TSc0CmS3PLlny5n37pcNcQHLrDeg3ACh7Y"
BRAND_HANDLE = "iradahclothing"
MAX_POSTS    = None  # None = all posts, or set a number e.g. 50

# ── Collab signals ────────────────────────────────────────────────────────────

COLLAB_SIGNALS = [
    # Standard international
    "#ad", "#sponsored", "#collab", "#gifted",
    "#paidpartnership", "#paidpromotion",
    "paid partnership", "paid promotion",
    "sponsored by", "gifted by",
    "in collaboration", "collab with",
    "partner with", "official partner",
    "brand ambassador",
    # Pakistani brand patterns
    "#zer0lifestyle", "#zerofamily", "#zeroambassador",
    "zero family", "our ambassador", "introducing",
    "we welcome", "officially joining",
    "in partnership", "exclusive partner",
    "zero regal", "zero luna", "zbuds",
]

NOT_COLLAB = [
    "not sponsored", "not an ad",
    "not gifted", "own purchase",
    "bought myself",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_mentions(caption: str, exclude: str) -> list[str]:
    mentions = re.findall(r'@(\w+)', caption)
    return list(set([
        m.lower() for m in mentions
        if m.lower() != exclude.lower()
    ]))


def detect_signal(caption: str) -> str | None:
    text = caption.lower()
    if any(n in text for n in NOT_COLLAB):
        return None
    return next((s for s in COLLAB_SIGNALS if s.lower() in text), None)


def get_confidence(likes: int, signal: str | None) -> str:
    if signal:
        return "HIGH"
    if likes >= 50_000:
        return "HIGH"
    if likes >= 10_000:
        return "MEDIUM"
    if likes >= 1_000:
        return "LOW"
    return "ORGANIC"


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


# ── Fetch from Apify ──────────────────────────────────────────────────────────

def fetch_all_posts() -> tuple[dict, list[dict]]:
    """
    Fetch brand profile + all posts from Apify.
    Returns (profile_info, posts_list)
    """
    print(f"Fetching ALL posts for @{BRAND_HANDLE}...")
    print("(Free plan = last 12 posts, Paid plan = all posts)")
    print()

    try:
        resp = httpx.post(
            "https://api.apify.com/v2/acts/apify~instagram-profile-scraper/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={
                "usernames":    [BRAND_HANDLE],
                "resultsLimit": MAX_POSTS or 999999,
            },
            timeout=300,
        )
        resp.raise_for_status()
        profiles = resp.json()

        if not profiles:
            print("ERROR: No data returned")
            return {}, []

        profile      = profiles[0]
        latest_posts = profile.get("latestPosts", [])

        print(f"Profile   : @{profile.get('username')}")
        print(f"Full Name : {profile.get('fullName')}")
        print(f"Followers : {format_number(profile.get('followersCount', 0))}")
        print(f"Total Posts on Account : {profile.get('postsCount', 0):,}")
        print(f"Posts Fetched          : {len(latest_posts)}")
        print()

        return profile, latest_posts

    except Exception as e:
        print(f"ERROR: {e}")
        return {}, []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"Instagram All-Posts Collab Finder — @{BRAND_HANDLE}")
    print("=" * 60)
    print()

    profile, posts = fetch_all_posts()
    if not posts:
        return

    # ── Per-post breakdown ────────────────────────────────────────
    post_breakdown = []

    # username -> aggregate stats across all posts
    people: dict[str, dict] = defaultdict(lambda: {
        "posts":       [],
        "total_likes": 0,
        "signals":     set(),
        "max_likes":   0,
    })

    total_posts_with_mentions = 0

    for post_num, post in enumerate(posts, 1):
        caption   = post.get("caption", "") or ""
        post_url  = post.get("url", "")
        likes     = post.get("likesCount", 0) or 0
        comments  = post.get("commentsCount", 0) or 0
        timestamp = post.get("timestamp", "")

        try:
            date_str = datetime.fromisoformat(timestamp).strftime("%Y-%m-%d")
        except Exception:
            date_str = timestamp[:10] if timestamp else "unknown"

        all_mentions = extract_mentions(caption, BRAND_HANDLE)
        if not all_mentions:
            continue

        total_posts_with_mentions += 1
        signal = detect_signal(caption)

        # Separate collabs vs plain tags per post
        post_collabs  = []
        post_mentions = []

        for username in all_mentions:
            if signal or likes >= 10_000:
                post_collabs.append(username)
            else:
                post_mentions.append(username)

            # Aggregate into people dict
            people[username]["posts"].append({
                "url":      post_url,
                "likes":    likes,
                "comments": comments,
                "date":     date_str,
                "signal":   signal,
                "caption":  caption[:200],
            })
            people[username]["total_likes"] += likes
            people[username]["max_likes"]    = max(
                people[username]["max_likes"], likes
            )
            if signal:
                people[username]["signals"].add(signal)

        post_breakdown.append({
            "post_number":    post_num,
            "date":           date_str,
            "url":            post_url,
            "likes":          likes,
            "comments":       comments,
            "signal":         signal,
            "caption":        caption[:200],
            "collab_with":    post_collabs,
            "tagged_with":    post_mentions,
            "total_mentions": len(all_mentions),
        })

    # ── Classify people overall ───────────────────────────────────
    confirmed = {}
    plain     = {}

    for username, data in people.items():
        confidence = get_confidence(
            data["max_likes"],
            next(iter(data["signals"]), None)
        )
        entry = {
            "username":    username,
            "appearances": len(data["posts"]),
            "total_likes": data["total_likes"],
            "max_likes":   data["max_likes"],
            "signals":     list(data["signals"]),
            "confidence":  confidence,
            "posts":       sorted(data["posts"], key=lambda x: x["likes"], reverse=True),
        }
        if data["signals"] or data["max_likes"] >= 10_000:
            confirmed[username] = entry
        else:
            plain[username] = entry

    confirmed_sorted = sorted(confirmed.values(), key=lambda x: x["max_likes"], reverse=True)
    plain_sorted     = sorted(plain.values(),     key=lambda x: x["max_likes"], reverse=True)

    # ════════════════════════════════════════════════════════════
    # SECTION 1 — Per-Post Breakdown
    # ════════════════════════════════════════════════════════════
    print("=" * 60)
    print("SECTION 1 — PER-POST BREAKDOWN")
    print("=" * 60)
    print()

    for p in post_breakdown:
        signal_str = f"[{p['signal']}]" if p["signal"] else "[no signal]"
        print(f"Post {p['post_number']:3} — {p['date']} | "
              f"❤️  {format_number(p['likes'])} | "
              f"💬 {format_number(p['comments'])} | "
              f"{signal_str}")
        print(f"  URL     : {p['url']}")
        print(f"  Caption : {p['caption'][:120]}")

        if p["collab_with"]:
            collabs_str = ", ".join([f"@{u}" for u in p["collab_with"]])
            print(f"  ✅ Collab with : {collabs_str}")

        if p["tagged_with"]:
            tagged_str = ", ".join([f"@{u}" for u in p["tagged_with"]])
            print(f"  🏷️  Tagged with : {tagged_str}")

        print()

    # ════════════════════════════════════════════════════════════
    # SECTION 2 — Overall Summary
    # ════════════════════════════════════════════════════════════
    print("=" * 60)
    print("SECTION 2 — OVERALL SUMMARY")
    print("=" * 60)
    print(f"  Posts scanned           : {len(posts)}")
    print(f"  Posts with @mentions    : {total_posts_with_mentions}")
    print(f"  Unique people mentioned : {len(people)}")
    print(f"  Likely collabs          : {len(confirmed)}")
    print(f"  Plain mentions          : {len(plain)}")
    print()

    # ════════════════════════════════════════════════════════════
    # SECTION 3 — Confirmed Collabs
    # ════════════════════════════════════════════════════════════
    if confirmed_sorted:
        print(f"✅ CONFIRMED COLLABS ({len(confirmed_sorted)})")
        print("-" * 60)
        for i, p in enumerate(confirmed_sorted, 1):
            signals_str = ", ".join(p["signals"]) if p["signals"] else "high engagement"
            print(f"{i:2}. @{p['username']}")
            print(f"      Appearances : {p['appearances']} post(s)")
            print(f"      Max Likes   : {format_number(p['max_likes'])}")
            print(f"      Total Likes : {format_number(p['total_likes'])}")
            print(f"      Confidence  : {p['confidence']}")
            print(f"      Signal      : {signals_str}")
            best = p["posts"][0]
            print(f"      Best Post   : {best['url']}")
            print(f"      Caption     : {best['caption'][:100]}")
            print()
    else:
        print("✅ CONFIRMED COLLABS: None found")
        print()

    # ════════════════════════════════════════════════════════════
    # SECTION 4 — Plain Mentions
    # ════════════════════════════════════════════════════════════
    if plain_sorted:
        print(f"⚠️  PLAIN MENTIONS ({len(plain_sorted)})")
        print("-" * 60)
        for p in plain_sorted:
            print(f"  @{p['username']:30} — "
                  f"{p['appearances']} post(s) — "
                  f"max ❤️  {format_number(p['max_likes'])}")
        print()

    # ── Save to JSON ──────────────────────────────────────────────
    output = {
        "brand":              BRAND_HANDLE,
        "followers":          profile.get("followersCount", 0),
        "posts_scanned":      len(posts),
        "unique_people":      len(people),
        "per_post_breakdown": post_breakdown,
        "confirmed_collabs":  confirmed_sorted,
        "plain_mentions":     plain_sorted,
    }

    output_file = f"instagram_all_collabs_{BRAND_HANDLE}.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)

    print(f"💾 Saved to: {output_file}")
    print()
    print("NOTE: Free Apify plan = last 12 posts only.")
    print("      Upgrade to paid plan to scan all posts.")


if __name__ == "__main__":
    main()