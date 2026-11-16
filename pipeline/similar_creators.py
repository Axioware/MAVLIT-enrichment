"""
pipeline/similar_creators.py

Real data for the Feed's "Similar Creators" tab.

A creator is a *partnership creator* when an instagram_users row that is not a commenter has a partnership with a
sponsorship score of 90 or more, in either:
  - instagram_posts            (the user's post_id is the brand post, scored in sponsorship_confidence), or
  - test_creator_brand_partnership_posts (creator_username, scored in sponsorship_confidence).
Only these creators get a creator_niches description and an embedding (pipeline/enrichment/creator_niches.py), so the
tab never shows a creator without a confirmed partnership.

A MAVLIT user is shown the partnership creators whose embedding is at least SIMILARITY_THRESHOLD (70 %) cosine-similar
to the user's own profile embedding (creator_profiles.embedding) - the same threshold matching/match_text.py uses.
"""
import logging

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session
from pgvector.sqlalchemy import Vector

from pipeline.db import CreatorNiche, InstagramUser
from pipeline.helpers.engagement import engagement_rate
from pipeline.helpers.gpt_llm import embed_text, embed_text_batch

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.70
MIN_PARTNERSHIP_CONFIDENCE = 90
DEFAULT_LIMIT = 60
MAX_PARTNERSHIPS_SHOWN = 8
_EMBED_BATCH = 100

# Lower-cased usernames of the creators that have a partnership scored >= :min (see the module docstring).
_PARTNERSHIP_USERNAMES = """
    SELECT lower(btrim(u.username)) AS username
    FROM instagram_users u
    JOIN instagram_posts p ON p.post_id = u.post_id
    WHERE u.user_type != 'commenter' AND p.sponsorship_confidence >= :min
    UNION
    SELECT lower(btrim(u.username))
    FROM instagram_users u
    JOIN test_creator_brand_partnership_posts t ON lower(btrim(t.creator_username)) = lower(btrim(u.username))
    WHERE u.user_type != 'commenter' AND t.sponsorship_confidence >= :min
"""


def partnership_usernames(db: Session, minimum: int = MIN_PARTNERSHIP_CONFIDENCE) -> set[str]:
    """Lower-cased usernames of the creators with a partnership scored at least `minimum`."""
    rows = db.execute(text(_PARTNERSHIP_USERNAMES), {"min": minimum}).all()
    return {r[0] for r in rows if r[0]}


def embedding_text(niche: str | None, description: str | None, tags: object) -> str:
    """The text that is embedded for a creator - the same format script_creator_niche/embed_creator_niches.py uses."""
    tag_text = ", ".join(str(t) for t in tags if str(t).strip()) if isinstance(tags, list) else ""
    return f"Niche: {niche}\nDescription: {description}\nTags: {tag_text}"


def embed_pending_creators(db: Session, usernames: set[str], batch_size: int = _EMBED_BATCH) -> int:
    """Embed the creator_niches rows of `usernames` that have a description and no embedding yet. Returns how many were embedded."""
    if not usernames:
        return 0
    rows = [
        r for r in db.query(CreatorNiche).filter(CreatorNiche.embedding.is_(None)).order_by(CreatorNiche.id).all()
        if (r.username or "").strip().casefold() in usernames
        and (r.niche or "").strip() and (r.description or "").strip()
    ]
    done = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        vectors = embed_text_batch(
            [embedding_text(r.niche, r.description, r.tags) for r in batch],
            context=f"similar creators embeddings {start // batch_size + 1}",
        )
        if len(vectors) != len(batch):
            logger.error("Similar creators: embedding batch starting at %d failed (%d creators left without one)", start, len(batch))
            continue
        for row, vector in zip(batch, vectors):
            row.embedding = vector
        db.commit()
        done += len(batch)
    logger.info("Similar creators: embedded %d creator(s)", done)
    return done


AUDIENCE_UP = 2     # "same audience size": up to 2x the user's followers ...
AUDIENCE_DOWN = 3   # ... and down to a third of them


def audience_range(followers) -> tuple[int, int] | None:
    """The follower range of the "same audience size" filter: from followers / 3 (rounded up) to followers * 2. None without a follower count."""
    if not followers or followers <= 0:
        return None
    return -(-int(followers) // AUDIENCE_DOWN), int(followers) * AUDIENCE_UP


def user_query_text(profile) -> str | None:
    """
    The user's profile written in the creators' format (Niche / Description / Tags), so a user and a creator who are alike get alike
    vectors. creator_profiles.embedding is built from a differently worded sentence and is left alone (matching uses it); comparing it with
    the creators' vectors scored a test user only 69 % against their own creator row. None when the profile has no niche and no description.
    """
    niche = (profile.instagram_primary_niche or profile.content_niche or "").strip()
    description = ((profile.instagram_description or profile.youtube_description or "")).strip()
    if not niche and not description:
        return None
    tags: list = []
    for group in (profile.instagram_sub_niches, profile.youtube_sub_niches, profile.content_tags):
        if isinstance(group, list):
            tags.extend(t.strip() for t in group if isinstance(t, str) and t.strip())
    return embedding_text(niche, description, list(dict.fromkeys(tags)))


_QUERY_CACHE: dict[str, list[float]] = {}
_QUERY_CACHE_MAX = 512


def user_query_embedding(profile) -> list[float] | None:
    """The embedding of user_query_text(profile): None when there is no text; kept in memory so a reload costs no OpenAI call."""
    query = user_query_text(profile)
    if query is None:
        return None
    vector = _QUERY_CACHE.get(query)
    if vector is None:
        vector = embed_text(query, context="similar creators: the user's profile")
        if not vector:
            return None
        if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
            _QUERY_CACHE.clear()
        _QUERY_CACHE[query] = vector
    return vector


_SIMILAR = text("""
    SELECT cn.id, cn.instagram_user_id, cn.username, cn.niche, cn.description, cn.tags, cn.engagement_rate,
           1 - (cn.embedding <=> :vec) AS similarity
    FROM creator_niches cn
    LEFT JOIN instagram_users fu ON fu.id = cn.instagram_user_id
    WHERE cn.embedding IS NOT NULL
      AND lower(btrim(cn.username)) = ANY(:partners)
      AND (:lo IS NULL OR fu.followers_count BETWEEN :lo AND :hi)
      AND lower(btrim(cn.username)) <> :own
      AND 1 - (cn.embedding <=> :vec) >= :threshold
    ORDER BY similarity DESC, cn.id
    LIMIT :limit
""").bindparams(bindparam("vec", type_=Vector(1024)))

_PARTNERSHIPS = text("""
    SELECT who, brand, category, post_url, at, confidence, post_type, likes, comments, views FROM (
        SELECT lower(btrim(u.username)) AS who, b.name AS brand, b.niche AS category, p.post_url, p.timestamp AS at,
               p.sponsorship_confidence AS confidence, p.post_type,
               CASE WHEN coalesce(p.likes_count, u.likes_count) >= 0 THEN coalesce(p.likes_count, u.likes_count) END AS likes,
               CASE WHEN coalesce(p.comments_count, u.comments_count) >= 0 THEN coalesce(p.comments_count, u.comments_count) END AS comments,
               coalesce(p.video_view_count, p.video_play_count, u.video_view_count, u.video_play_count) AS views
        FROM instagram_users u
        JOIN instagram_posts p ON p.post_id = u.post_id
        LEFT JOIN brands_raw b ON b.id = p.brand_raw_id
        WHERE u.user_type != 'commenter' AND p.sponsorship_confidence >= :min AND lower(btrim(u.username)) = ANY(:names)
        UNION
        SELECT lower(btrim(t.creator_username)), coalesce(t.brand_name, b.name), b.niche, t.post_url, t.post_timestamp,
               t.sponsorship_confidence, NULL,
               CASE WHEN ux.likes_count >= 0 THEN ux.likes_count END, CASE WHEN ux.comments_count >= 0 THEN ux.comments_count END,
               coalesce(t.video_view_count, t.video_play_count, ux.video_view_count, ux.video_play_count)
        FROM test_creator_brand_partnership_posts t
        LEFT JOIN brands_raw b ON b.id = t.brand_raw_id
        LEFT JOIN instagram_users ux ON ux.post_id = nullif(t.post_id, '')
        WHERE t.sponsorship_confidence >= :min AND lower(btrim(t.creator_username)) = ANY(:names)
    ) all_partnerships
    ORDER BY who, at DESC NULLS LAST
""")


def post_kind(post_url: str | None, post_type: str | None, views) -> str:
    """What to call a partnership post: a Reel (by its link), else Video / Carousel / Photo from Instagram's own post type, else Video when it has views."""
    if post_url and "/reel/" in post_url:
        return "Reel"
    kind = (post_type or "").strip().lower()
    if kind in ("video", "reel"):
        return "Video"
    if kind == "sidecar":
        return "Carousel"
    if kind == "image":
        return "Photo"
    return "Video" if views else "Post"


# Average likes and comments over the posts we hold for each creator (one instagram_users row per post). Instagram hides some counts as -1: left out.
_ENGAGEMENT = text("""
    SELECT lower(btrim(username)) AS who,
           avg(likes_count) FILTER (WHERE likes_count >= 0) AS avg_likes,
           avg(comments_count) FILTER (WHERE comments_count >= 0) AS avg_comments
    FROM instagram_users
    WHERE user_type != 'commenter' AND lower(btrim(username)) = ANY(:names)
    GROUP BY 1
""")


_FILL_SOURCE = text("""
    SELECT cn.id, u.followers_count, e.avg_likes, e.avg_comments
    FROM creator_niches cn
    LEFT JOIN instagram_users u ON u.id = cn.instagram_user_id
    LEFT JOIN (
        SELECT lower(btrim(username)) AS who,
               avg(likes_count) FILTER (WHERE likes_count >= 0) AS avg_likes,
               avg(comments_count) FILTER (WHERE comments_count >= 0) AS avg_comments
        FROM instagram_users WHERE user_type != 'commenter' GROUP BY 1
    ) e ON e.who = lower(btrim(cn.username))
    WHERE cn.engagement_rate IS NULL AND lower(btrim(cn.username)) = ANY(:names)
""")


def fill_engagement_rates(db: Session, usernames: set[str]) -> int:
    """Calculate creator_niches.engagement_rate for the rows of `usernames` that have none yet (average likes + average comments / followers * 100).
    A creator without likes data or without a follower count stays empty. Returns how many rows got a value."""
    if not usernames:
        return 0
    filled = 0
    for row in db.execute(_FILL_SOURCE, {"names": sorted(usernames)}).all():
        rate = engagement_rate(row.avg_likes, row.avg_comments, row.followers_count)
        if rate is not None:
            db.query(CreatorNiche).filter(CreatorNiche.id == row.id).update({"engagement_rate": rate})
            filled += 1
    db.commit()
    logger.info("Similar creators: engagement rate calculated for %d creator(s)", filled)
    return filled


def find_similar_creators(
    db: Session, user_embedding, own_handle: str | None = None, limit: int = DEFAULT_LIMIT,
    threshold: float = SIMILARITY_THRESHOLD, minimum: int = MIN_PARTNERSHIP_CONFIDENCE,
    followers_range: tuple[int, int] | None = None,
) -> list[dict]:
    """
    The partnership creators at least `threshold` similar to `user_embedding`, most similar first, each with its
    profile numbers and its partnerships. `own_handle` (the user's own Instagram handle) is never returned.
    `followers_range` (low, high) is a hard filter: only creators whose follower count is inside it (a creator with no follower count is left out).
    """
    own = (own_handle or "").strip().lstrip("@").lower() or "\x00"
    partners = sorted(partnership_usernames(db, minimum))
    if not partners:
        return []
    found = db.execute(_SIMILAR, {"vec": list(user_embedding), "partners": partners, "own": own, "threshold": threshold, "limit": limit,
                                    "lo": followers_range[0] if followers_range else None, "hi": followers_range[1] if followers_range else None}).all()
    if not found:
        return []
    names = [r.username.strip().lower() for r in found]
    partnerships: dict[str, list[dict]] = {}
    seen: set[tuple[str, str | None]] = set()
    for row in db.execute(_PARTNERSHIPS, {"min": minimum, "names": names}).all():
        if (row.who, row.post_url) in seen:
            continue
        seen.add((row.who, row.post_url))
        partnerships.setdefault(row.who, []).append({
            "brand": row.brand, "category": row.category, "post_url": row.post_url, "date": row.at, "confidence": row.confidence,
            "type": post_kind(row.post_url, row.post_type, row.views), "likes": row.likes, "comments": row.comments, "views": row.views,
        })
    engagement = {r.who: (r.avg_likes, r.avg_comments) for r in db.execute(_ENGAGEMENT, {"names": names}).all()}
    profiles = {u.id: u for u in db.query(InstagramUser).filter(InstagramUser.id.in_([r.instagram_user_id for r in found])).all()}

    result = []
    for r in found:
        user = profiles.get(r.instagram_user_id)
        who = r.username.strip().lower()
        avg_likes, avg_comments = engagement.get(who, (None, None))
        followers = user.followers_count if user else None
        # one post's engagement: its likes + comments over the creator's followers
        shown = [{**p, "engagement": engagement_rate(p["likes"], p["comments"], followers)} for p in partnerships.get(who, [])]
        result.append({
            "username": r.username.strip(),
            "full_name": user.full_name if user else None,
            "profile_url": (user.profile_url if user and user.profile_url else f"https://www.instagram.com/{r.username.strip()}"),
            "niche": r.niche,
            "description": r.description,
            "tags": r.tags if isinstance(r.tags, list) else [],
            "country": user.country if user else None,
            "followers": followers,
            "avg_likes": round(float(avg_likes)) if avg_likes is not None else None,
            "avg_comments": round(float(avg_comments)) if avg_comments is not None else None,
            "engagement_rate": r.engagement_rate if r.engagement_rate is not None else engagement_rate(avg_likes, avg_comments, followers),
            "similarity": round(float(r.similarity) * 100),
            "partnership_count": len(shown),
            "partnerships": shown[:MAX_PARTNERSHIPS_SHOWN],
        })
    return result


# --- Why is the tab empty? ------------------------------------------------------------------------------------------------------
_TOP_ANY = text("""
    SELECT cn.username, 1 - (cn.embedding <=> :vec) AS similarity, lower(btrim(cn.username)) = ANY(:partners) AS has_partnership
    FROM creator_niches cn WHERE cn.embedding IS NOT NULL
    ORDER BY cn.embedding <=> :vec LIMIT 10
""").bindparams(bindparam("vec", type_=Vector(1024)))


def diagnose(db: Session, email: str) -> dict:
    """Read-only counts that show where the Similar Creators list gets empty for the user with this email."""
    from pipeline.db import CreatorProfile
    partners = partnership_usernames(db)
    niche_rows = db.query(CreatorNiche).all()
    names = {(r.username or "").strip().casefold() for r in niche_rows}
    out = {
        "partnership_creators": len(partners),
        "creator_niches_rows": len(niche_rows),
        "creator_niches_with_embedding": sum(1 for r in niche_rows if r.embedding is not None),
        "partnership_creators_with_a_creator_niches_row": len(partners & names),
        "partnership_creators_with_an_embedding": len(partners & {(r.username or "").strip().casefold() for r in niche_rows if r.embedding is not None}),
    }
    user = db.query(CreatorProfile).filter(CreatorProfile.email == email.strip().lower()).first()
    out["user_found"] = user is not None
    query = user_query_embedding(user) if user is not None else None
    out["user_has_embedding"] = query is not None
    if query is not None:
        out["user_query_text"] = user_query_text(user)
        top = db.execute(_TOP_ANY, {"vec": list(query), "partners": sorted(partners)}).all()
        out["closest_creators_any"] = [{"username": r.username, "similarity": round(float(r.similarity) * 100, 1), "has_partnership": bool(r.has_partnership)} for r in top]
    return out


if __name__ == "__main__":   # python -m pipeline.similar_creators you@example.com
    import json
    import sys
    from pipeline.db import BatchSessionLocal
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m pipeline.similar_creators <email of the MAVLIT user>")
    with BatchSessionLocal() as session:
        print(json.dumps(diagnose(session, sys.argv[1]), indent=2))
