"""
pipeline/matching/matcher.py

Stage 3 orchestrator — the real-time matching entry point, get_matches().
Runs on every page load; designed to be cheap:

  Step A — Hard filters: drop brands with essentially zero sponsorship
           activity (a low floor, not a strict cutoff — unscored brands
           are kept, only confirmed-zero-activity ones are dropped). Also
           drops brands whose niche is in the creator's excluded_categories.

           Instagram-specific filters, all gated on primary_platform ==
           "instagram" (not applied for any other primary_platform):
             - brand must not be CONFIRMED absent from Instagram
               (has_instagram is False; NULL/unchecked is kept)
             - brand must have CONFIRMED collaborator history on Instagram
               (insta_lowest/insta_highest not null, from brand_signals.py's
               tag/mention/co-author collaborator pull) — unmeasured brands
               are dropped here, not just confirmed-absent ones
             - if the creator also gave a follower_count, it must fall
               within the brand's confirmed insta_lowest/insta_highest
               range, extended by _FOLLOWER_TOLERANCE on each side (that
               range is already a min/max over every confirmed collaborator
               for the brand, however many there are)

           YouTube-specific filter, gated on primary_platform == "youtube":
             - if the creator gave youtube_followers (their subscriber
               count), it must fall within the brand's confirmed
               youtube_lowest/youtube_highest collaborator range, extended
               by _FOLLOWER_TOLERANCE on each side — same shape as the
               Instagram follower-range check above, just no equivalent of
               the has_instagram/insta_lowest presence checks was asked for
               on the YouTube side.

           Creators must also share at least one EXACT (case-insensitive)
           niche string with the brand — separate from and stricter than
           the fuzzy niche_match scoring dimension in Step C, which still
           runs on whatever niche overlap exists for ranking purposes.
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

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from pipeline.db import BrandProfile, BrandRaw, CreatorProfile
from pipeline.matching.match_text import generate_match_reasons
from pipeline.matching.scoring import score_match

logger = logging.getLogger(__name__)

_SHORTLIST_SIZE = 100
_ACTIVITY_FLOOR = 0   # brands with a CONFIRMED score at or below this are dropped; unscored (NULL) brands are kept
_FOLLOWER_TOLERANCE = 0.30   # +/-30% buffer beyond the brand's confirmed collaborator follower range


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
    if creator.excluded_categories:
        excluded = [c.strip().lower() for c in creator.excluded_categories if c]
        if excluded:
            for niche in excluded:
                query = query.filter(~BrandRaw.niche.ilike(f"%{niche}%"))

    # Instagram-specific hard filters, gated on primary_platform == "instagram".
    if creator.primary_platform and creator.primary_platform.strip().lower() == "instagram":
        query = query.filter(or_(BrandProfile.has_instagram.is_(None), BrandProfile.has_instagram.is_(True)))
        query = query.filter(BrandProfile.insta_lowest.isnot(None), BrandProfile.insta_highest.isnot(None))
        if creator.follower_count is not None:
            query = query.filter(
                creator.follower_count >= BrandProfile.insta_lowest * (1 - _FOLLOWER_TOLERANCE),
                creator.follower_count <= BrandProfile.insta_highest * (1 + _FOLLOWER_TOLERANCE),
            )

    # YouTube follower-range fit, gated on primary_platform == "youtube" —
    # same shape as the Instagram check above, against youtube_lowest/
    # youtube_highest (already a min/max over every confirmed collaborator).
    if creator.primary_platform and creator.primary_platform.strip().lower() == "youtube":
        if creator.youtube_followers is not None:
            query = query.filter(
                BrandProfile.youtube_lowest.isnot(None), BrandProfile.youtube_highest.isnot(None),
                creator.youtube_followers >= BrandProfile.youtube_lowest * (1 - _FOLLOWER_TOLERANCE),
                creator.youtube_followers <= BrandProfile.youtube_highest * (1 + _FOLLOWER_TOLERANCE),
            )

    # Creator must share at least one EXACT niche with the brand — a hard
    # yes/no check, separate from niche_compatibility()'s fuzzy score used
    # for ranking in Step C.
    if creator.content_niche:
        creator_niches = [n.strip() for n in creator.content_niche.split(",") if n.strip()]
        if creator_niches:
            query = query.filter(or_(*[func.lower(BrandRaw.niche) == n.lower() for n in creator_niches]))

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
