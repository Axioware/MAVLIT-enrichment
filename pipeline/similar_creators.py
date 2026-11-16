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
from pipeline.helpers.gpt_llm import embed_text_batch

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


_SIMILAR = text("""
    SELECT cn.id, cn.instagram_user_id, cn.username, cn.niche, cn.description, cn.tags,
           1 - (cn.embedding <=> :vec) AS similarity
    FROM creator_niches cn
    WHERE cn.embedding IS NOT NULL
      AND lower(btrim(cn.username)) = ANY(:partners)
      AND lower(btrim(cn.username)) <> :own
      AND 1 - (cn.embedding <=> :vec) >= :threshold
    ORDER BY similarity DESC, cn.id
    LIMIT :limit
""").bindparams(bindparam("vec", type_=Vector(1024)))

_PARTNERSHIPS = text("""
    SELECT who, brand, post_url, at, confidence FROM (
        SELECT lower(btrim(u.username)) AS who, b.name AS brand, p.post_url, p.timestamp AS at, p.sponsorship_confidence AS confidence
        FROM instagram_users u
        JOIN instagram_posts p ON p.post_id = u.post_id
        LEFT JOIN brands_raw b ON b.id = p.brand_raw_id
        WHERE u.user_type != 'commenter' AND p.sponsorship_confidence >= :min AND lower(btrim(u.username)) = ANY(:names)
        UNION
        SELECT lower(btrim(t.creator_username)), coalesce(t.brand_name, b.name), t.post_url, t.post_timestamp, t.sponsorship_confidence
        FROM test_creator_brand_partnership_posts t
        LEFT JOIN brands_raw b ON b.id = t.brand_raw_id
        WHERE t.sponsorship_confidence >= :min AND lower(btrim(t.creator_username)) = ANY(:names)
    ) all_partnerships
    ORDER BY who, at DESC NULLS LAST
""")


def find_similar_creators(
    db: Session, user_embedding, own_handle: str | None = None, limit: int = DEFAULT_LIMIT,
    threshold: float = SIMILARITY_THRESHOLD, minimum: int = MIN_PARTNERSHIP_CONFIDENCE,
) -> list[dict]:
    """
    The partnership creators at least `threshold` similar to `user_embedding`, most similar first, each with its
    profile numbers and its partnerships. `own_handle` (the user's own Instagram handle) is never returned.
    """
    own = (own_handle or "").strip().lstrip("@").lower() or "\x00"
    partners = sorted(partnership_usernames(db, minimum))
    if not partners:
        return []
    found = db.execute(_SIMILAR, {"vec": list(user_embedding), "partners": partners, "own": own, "threshold": threshold, "limit": limit}).all()
    if not found:
        return []
    names = [r.username.strip().lower() for r in found]
    partnerships: dict[str, list[dict]] = {}
    seen: set[tuple[str, str | None]] = set()
    for who, brand, post_url, at, confidence in db.execute(_PARTNERSHIPS, {"min": minimum, "names": names}).all():
        if (who, post_url) in seen:
            continue
        seen.add((who, post_url))
        partnerships.setdefault(who, []).append({"brand": brand, "post_url": post_url, "date": at, "confidence": confidence})
    profiles = {u.id: u for u in db.query(InstagramUser).filter(InstagramUser.id.in_([r.instagram_user_id for r in found])).all()}

    result = []
    for r in found:
        user = profiles.get(r.instagram_user_id)
        who = r.username.strip().lower()
        shown = partnerships.get(who, [])
        result.append({
            "username": r.username.strip(),
            "full_name": user.full_name if user else None,
            "profile_url": (user.profile_url if user and user.profile_url else f"https://www.instagram.com/{r.username.strip()}"),
            "niche": r.niche,
            "description": r.description,
            "tags": r.tags if isinstance(r.tags, list) else [],
            "country": user.country if user else None,
            "followers": user.followers_count if user else None,
            "similarity": round(float(r.similarity) * 100),
            "partnership_count": len(shown),
            "partnerships": shown[:MAX_PARTNERSHIPS_SHOWN],
        })
    return result
