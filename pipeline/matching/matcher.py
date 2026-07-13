"""
pipeline/matching/matcher.py

Stage 3 orchestrator — the real-time matching entry point, get_matches().
Runs on every page load; designed to be cheap:

  Step A — Hard filters: drop brands with essentially zero sponsorship
           activity (a low floor, not a strict cutoff — unscored brands
           are kept, only confirmed-zero-activity ones are dropped) and,
           if the creator has a primary_platform set, brands confirmed
           absent from it. Also drops brands whose niche is in the
           creator's excluded_categories.
  Step B — Semantic shortlist: a single indexed pgvector cosine-distance
           query against the creator's embedding narrows the (already
           hard-filtered) pool to the top _SHORTLIST_SIZE candidates —
           fast because embeddings are precomputed.
  Step C — Weighted scoring (pipeline.matching.scoring.score_match) across
           all 7 dimensions for each shortlisted candidate, plus the Tier-1
           template match-text (pipeline.matching.match_text).

Only brands with a brand_match_profile row are ever considered — that's
the population Stage 1 (brand_signals.py) has already computed signals
for. A brand with no profile row at all hasn't been through Stage 1 yet
and has nothing to score against.

No caching layer (match_cache) exists yet — deferred by design per the
matching doc ("later on"). Every call recomputes live; Step A/B keep this
cheap enough for a real-time page load regardless.
"""

import logging

from sqlalchemy import or_
from sqlalchemy.orm import Session

from pipeline.db import BrandProfile, BrandRaw, CreatorProfile
from pipeline.matching.match_text import generate_match_reasons
from pipeline.matching.scoring import score_match

logger = logging.getLogger(__name__)

_SHORTLIST_SIZE = 100
_ACTIVITY_FLOOR = 0   # brands with a CONFIRMED score at or below this are dropped; unscored (NULL) brands are kept

_PLATFORM_FLAG_ATTR = {
    "instagram": BrandProfile.has_instagram,
    "youtube":   BrandProfile.has_youtube,
    "facebook":  BrandProfile.has_facebook,
    "tiktok":    BrandProfile.has_tiktok,
    "twitter":   BrandProfile.has_twitter,
}


def get_matches(db: Session, creator_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
    """
    Returns up to `limit` ranked brand matches for one creator, best-first,
    starting at `offset`. Each result:
        {
          "brand_raw_id": int, "brand_name": str,
          "total_score": float (0-1), "dimensions": {...},
          "reasons": [str, ...],
        }
    Returns [] if the creator doesn't exist or has no embedding yet
    (Stage 2 — compute_creator_signals — must run first).
    """
    creator = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
    if not creator:
        logger.warning("Matching: creator_id=%d not found", creator_id)
        return []
    if creator.embedding is None:
        logger.info("Matching: creator_id=%d has no embedding yet — run Stage 2 first", creator_id)
        return []

    distance_expr = BrandProfile.embedding.cosine_distance(creator.embedding)

    query = (
        db.query(BrandRaw, BrandProfile, distance_expr.label("distance"))
        .join(BrandProfile, BrandProfile.brand_raw_id == BrandRaw.id)
        .filter(BrandProfile.embedding.isnot(None))
    )

    # Step A — hard filters
    query = query.filter(
        or_(BrandProfile.sponsorship_activity_score.is_(None), BrandProfile.sponsorship_activity_score > _ACTIVITY_FLOOR)
    )
    if creator.primary_platform:
        flag_col = _PLATFORM_FLAG_ATTR.get(creator.primary_platform.strip().lower())
        if flag_col is not None:
            query = query.filter(or_(flag_col.is_(None), flag_col.is_(True)))
    if creator.excluded_categories:
        excluded = [c.strip().lower() for c in creator.excluded_categories if c]
        if excluded:
            for niche in excluded:
                query = query.filter(~BrandRaw.niche.ilike(f"%{niche}%"))

    # Step B — semantic shortlist (single indexed pgvector query)
    query = query.order_by(distance_expr).limit(_SHORTLIST_SIZE)
    shortlist = query.all()

    if not shortlist:
        logger.info("Matching: creator_id=%d — no qualifying brands after hard filters", creator_id)
        return []

    # Step C — weighted scoring + match text
    results = []
    for brand, profile, distance in shortlist:
        scored = score_match(creator, brand, profile, distance)
        reasons = generate_match_reasons(creator, brand, profile, scored["dimensions"])
        results.append({
            "brand_raw_id": brand.id,
            "brand_name":   brand.name,
            "total_score":  round(scored["total_score"], 4),
            "dimensions":   scored["dimensions"],
            "reasons":      reasons,
        })

    results.sort(key=lambda r: r["total_score"], reverse=True)

    logger.info(
        "Matching: creator_id=%d -> %d shortlisted, returning %d (offset=%d)",
        creator_id, len(results), min(limit, max(0, len(results) - offset)), offset,
    )
    return results[offset:offset + limit]
