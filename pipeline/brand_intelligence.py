"""
pipeline/brand_intelligence.py

The numbers on the Feed's Brand Intelligence cards, for the brands a user is matched with. For each brand, from its partnership posts: a post
counts when its sponsorship score is at least 90, in instagram_posts or in test_creator_brand_partnership_posts (one post found in both is
counted once).
  - partnerships        how many such posts
  - min/max_followers   the smallest and the biggest creator that made one ("typical creator size")
  - formats             the share of those posts that are a Reel, a Carousel, ... (percent)
  - avg_engagement      the average of those creators' engagement rates
"""
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from pipeline.helpers.engagement import engagement_rate
from pipeline.pitch_posts import post_code
from pipeline.similar_creators import MIN_PARTNERSHIP_CONFIDENCE, post_kind

_POSTS = text("""
    SELECT p.brand_raw_id AS brand_id, p.post_url, p.post_type, coalesce(p.video_view_count, p.video_play_count) AS views, lower(btrim(u.username)) AS who
    FROM instagram_posts p
    LEFT JOIN instagram_users u ON u.post_id = p.post_id AND u.user_type != 'commenter'
    WHERE p.brand_raw_id = ANY(:ids) AND p.sponsorship_confidence >= :min
    UNION
    SELECT t.brand_raw_id, t.post_url, NULL, coalesce(t.video_view_count, t.video_play_count), lower(btrim(t.creator_username))
    FROM test_creator_brand_partnership_posts t
    WHERE t.brand_raw_id = ANY(:ids) AND t.sponsorship_confidence >= :min
""")

# followers (the biggest snapshot we hold), the name, and the average likes and comments of each creator's posts
_CREATORS = text("""
    SELECT lower(btrim(username)) AS who, max(followers_count) AS followers, max(full_name) AS full_name,
           avg(likes_count) FILTER (WHERE likes_count >= 0) AS avg_likes, avg(comments_count) FILTER (WHERE comments_count >= 0) AS avg_comments
    FROM instagram_users
    WHERE user_type != 'commenter' AND lower(btrim(username)) = ANY(:names)
    GROUP BY 1
""")

_STORED = text("SELECT lower(btrim(username)) AS who, engagement_rate, niche FROM creator_niches WHERE lower(btrim(username)) = ANY(:names)")


def _post_key(url: str | None, fallback: int) -> str:
    """The same Instagram post written two ways (with or without a trailing slash, /p/ or /reel/) is one post."""
    code = post_code(url or "")
    return f"code:{code}" if code else (re.sub(r"[/?#].*$|/$", "", (url or "").strip().lower()) or f"row:{fallback}")


def _load(db: Session, brand_ids: list[int], minimum: int):
    """The partnership posts of the brands (each post once, with the creators who made it) and what we know about those creators."""
    posts: dict[int, dict[str, dict]] = {b: {} for b in brand_ids}   # brand -> post key -> {url, type, views, who: set}
    for index, row in enumerate(db.execute(_POSTS, {"ids": list(brand_ids), "min": minimum}).all()):
        entry = posts[row.brand_id].setdefault(_post_key(row.post_url, index), {"url": row.post_url, "type": row.post_type, "views": row.views, "who": set()})
        entry["type"] = entry["type"] or row.post_type
        entry["views"] = entry["views"] or row.views
        if row.who:
            entry["who"].add(row.who)
    names = sorted({w for per in posts.values() for e in per.values() for w in e["who"]})
    creators = {r.who: r for r in db.execute(_CREATORS, {"names": names}).all()} if names else {}
    stored = {r.who: r for r in db.execute(_STORED, {"names": names}).all()} if names else {}
    return posts, creators, stored


def _rate(who: str, creators: dict, stored: dict) -> float | None:
    """A creator's engagement rate: the one saved in creator_niches, else worked out from their likes, comments and followers."""
    saved = stored.get(who)
    if saved is not None and saved.engagement_rate is not None:
        return float(saved.engagement_rate)
    c = creators.get(who)
    return engagement_rate(c.avg_likes, c.avg_comments, c.followers) if c is not None else None


def brand_stats(db: Session, brand_ids: list[int], minimum: int = MIN_PARTNERSHIP_CONFIDENCE) -> dict[int, dict]:
    """{brand_id: {partnerships, creators, min_followers, max_followers, formats [{name, pct}], avg_engagement}} for every id (zeros and None when there is nothing)."""
    out = {b: {"partnerships": 0, "creators": 0, "min_followers": None, "max_followers": None, "formats": [], "avg_engagement": None} for b in brand_ids}
    if not brand_ids:
        return out
    posts, creators, stored = _load(db, brand_ids, minimum)
    for brand_id, per in posts.items():
        entries = list(per.values())
        if not entries:
            continue
        who = sorted({w for e in entries for w in e["who"]})
        followers = [creators[w].followers for w in who if w in creators and creators[w].followers]
        rates = [r for r in (_rate(w, creators, stored) for w in who) if r is not None]
        kinds: dict[str, int] = {}
        for e in entries:
            kind = post_kind(e["url"], e["type"], e["views"])
            kinds[kind] = kinds.get(kind, 0) + 1
        out[brand_id] = {
            "partnerships": len(entries),
            "creators": len(who),
            "min_followers": min(followers) if followers else None,
            "max_followers": max(followers) if followers else None,
            "formats": [{"name": k, "pct": round(n * 100 / len(entries))} for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))],
            "avg_engagement": round(sum(rates) / len(rates), 1) if rates else None,
        }
    return out


def brand_creators(db: Session, brand_id: int, limit: int = 50, minimum: int = MIN_PARTNERSHIP_CONFIDENCE) -> list[dict]:
    """The unique creators behind a brand's partnership posts, the ones with the most posts first, then the biggest."""
    posts, creators, stored = _load(db, [brand_id], minimum)
    count: dict[str, int] = {}
    for entry in posts[brand_id].values():
        for who in entry["who"]:
            count[who] = count.get(who, 0) + 1
    rows = []
    for who, n in count.items():
        c = creators.get(who)
        saved = stored.get(who)
        rows.append({
            "username": who, "full_name": c.full_name if c is not None else None, "profile_url": f"https://www.instagram.com/{who}",
            "niche": saved.niche if saved is not None else None, "followers": c.followers if c is not None and c.followers else None,
            "engagement_rate": _rate(who, creators, stored), "partnership_posts": n,
        })
    rows.sort(key=lambda r: (-r["partnership_posts"], -(r["followers"] or 0), r["username"]))
    return rows[:limit]
