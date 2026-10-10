"""
pipeline/matching/matcher.py

Stage 3 orchestrator — the real-time matching entry point, get_matches().
Runs on every page load; designed to be cheap:

  Step A — Hard filters: drop brands with essentially zero sponsorship
           activity (a low floor, not a strict cutoff — unscored brands
           are kept, only confirmed-zero-activity ones are dropped). Also
           drops brands carrying any brands_niches tag in the creator's
           excluded_categories ("Brand categories to avoid").

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
           niche string with ANY of:
             - the brand itself
             - one of the brand's confirmed Instagram collaborators
               (instagram_users.niche, for coauthor_producer/tagged_user/
               mention rows — never commenters) — e.g. a creator picking
               "technology" matches Bambu Lab (niche "3d printing") because
               Bambu Lab sponsored @makerworld_official, an Instagram
               creator classified as "technology".
             - a reverse-engineered creator (content_creator_re) the brand
               is linked to, matched against that creator's SOURCE
               content_creator_re.niche (a live join, not the possibly-
               stale copy mirrored onto instagram_users.niche at scrape
               time) — lets brands discovered via the RE pipeline surface
               even when the brand's own niche differs.
           This is a hard yes/no check, separate from and stricter than the
           fuzzy niche_match scoring dimension in Step C, which still runs
           on whatever niche overlap exists for ranking.

           Gender filter, gated on the creator's gender being male or
           female (skipped otherwise). The brand must pass ANY of:
             - brands_raw.target_audience_gender is the creator's gender
               or "both"
             - at least one confirmed Instagram collaborator (non-
               commenter) of the creator's gender, on a post with
               sponsorship_confidence >= 90
             - at least one reverse-engineered creator (instagram_users,
               user_type "contentcreatorRE") of the creator's gender, on a
               test partnership post with sponsorship_confidence >= 90
           e.g. a male creator never sees a female-audience brand unless
           that brand has sponsored at least one male creator.

           Geo-reach filter: brands_raw.geo_reach_score must be 0-40
           (local/small-reach) or NULL (not geo-scored yet).

           Avoided brand niches: brands whose brands_raw.niche is in
           _EXCLUDED_BRAND_NICHES (e.g. "Band") are dropped for every
           creator, on top of the creator's own excluded categories.
  Step B — Semantic shortlist: a single indexed pgvector cosine-distance
           query against the creator's embedding narrows the (already
           hard-filtered) pool nearest-first — every brand passing Step A
           is kept, so the frontend can page through all of them.
  Step C — Score each shortlisted candidate from its "why it's a match"
           taglines (pipeline.matching.match_text.collect_match_reasons):
           every applicable tagline earns its priority as points, scaled
           to 55-100% by pipeline.matching.scoring.score_match (see its
           docstring). Results are sorted by niche tier first (brand niche
           + confident creators match yours > brand niche matches yours >
           only its confident creators match yours > neither), then by
           that score; ties keep the Step B semantic-distance order.

Only brands with a brand_match_profile row are ever considered — that's
the population Stage 1 (brand_signals.py) has already computed signals
for. A brand with no profile row at all hasn't been through Stage 1 yet
and has nothing to score against.

No caching layer (match_cache) exists yet — deferred by design per the
matching doc ("later on"). Every call recomputes live; Step A/B keep this
cheap enough for a real-time page load regardless.
"""

import logging

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from pipeline.db import (
    BrandInstagramUser,
    BrandNiche,
    BrandProfile,
    BrandRaw,
    ContentCreatorRE,
    CreatorProfile,
    InstagramPost,
    InstagramUser,
    TestCreatorBrandPartnershipPost,
)
from pipeline.matching.match_text import collect_match_reasons, generate_match_reasons
from pipeline.matching.scoring import score_match

logger = logging.getLogger(__name__)

_ACTIVITY_FLOOR = 0   # brands with a CONFIRMED score at or below this are dropped; unscored (NULL) brands are kept
_FOLLOWER_TOLERANCE = 2   # +/-200% buffer beyond the brand's confirmed collaborator follower range
# Brand categories always avoided, for every creator — brands_raw.niche values
# (compared case-insensitively). Brands with no niche are kept.
_EXCLUDED_BRAND_NICHES = ("band",)



def _hard_filtered_query(db: Session, creator: CreatorProfile, apply_gender_filter: bool = True):
    """
    Step A — the hard-filtered (BrandRaw, BrandProfile, distance) query for
    one creator, unordered and unlimited, plus the cosine-distance
    expression to order it by. Shared by get_matches (v1) and
    hard_filtered_brands (v2 LLM ranking) so both use identical filters.
    """
    distance_expr = BrandProfile.embedding.cosine_distance(creator.embedding)

    query = (
        db.query(BrandRaw, BrandProfile, distance_expr.label("distance"))
        .join(BrandProfile, BrandProfile.brand_raw_id == BrandRaw.id)
        .filter(BrandRaw.name.isnot(None))
        .filter(BrandProfile.embedding.isnot(None))
    )

    # Step A — hard filters
    query = query.filter(
        or_(BrandProfile.sponsorship_activity_score.is_(None), BrandProfile.sponsorship_activity_score > _ACTIVITY_FLOOR)
    )
    # Local/small-reach brands only (geo_reach_score 0-40); NULL = not
    # geo-scored yet, kept.
    query = query.filter(or_(BrandRaw.geo_reach_score.is_(None), BrandRaw.geo_reach_score.between(0, 40)))
    # Hard-coded brand categories to avoid (brands_raw.niche), for everyone.
    query = query.filter(or_(
        BrandRaw.niche.is_(None),
        ~func.lower(func.trim(BrandRaw.niche)).in_(_EXCLUDED_BRAND_NICHES),
    ))
    # "Brand categories to avoid" are brands_niches.tags (picked on the
    # creator-profile page from the tags of their primary niche) — drop any
    # brand carrying one of them (exact tag, case-insensitive).
    excluded_tags = sorted({
        c.strip().lower() for c in (creator.excluded_categories or [])
        if isinstance(c, str) and c.strip()
    })
    if excluded_tags:
        tagged_brand_ids = (
            db.query(BrandNiche.brand_raw_id)
            .filter(
                func.jsonb_typeof(BrandNiche.tags) == "array",
                text(
                    "EXISTS (SELECT 1 FROM jsonb_array_elements_text(brands_niches.tags) AS t(tag) "
                    "WHERE lower(trim(t.tag)) = ANY(:excluded_tags))"
                ).bindparams(excluded_tags=excluded_tags),
            )
        )
        query = query.filter(~BrandRaw.id.in_(tagged_brand_ids))

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
    if (
        creator.primary_platform
        and creator.primary_platform.strip().lower() == "youtube"
        and creator.youtube_followers is not None
    ):
        query = query.filter(
            BrandProfile.youtube_lowest.isnot(None), BrandProfile.youtube_highest.isnot(None),
            creator.youtube_followers >= BrandProfile.youtube_lowest * (1 - _FOLLOWER_TOLERANCE),
            creator.youtube_followers <= BrandProfile.youtube_highest * (1 + _FOLLOWER_TOLERANCE),
        )

    # Keep brands eligible regardless of partner-creator embedding similarity.
    # This was acting as a hard exclusion and could remove perfectly relevant
    # niche matches (like beauty brands) before the semantic shortlist even ran.
    # The similarity floor still exists as a soft ranking signal in the later
    # weighted score step, but it should not zero out matches outright.

    # Creator must share at least one EXACT niche with EITHER the brand
    # itself OR one of the brand's confirmed Instagram collaborators — a
    # hard yes/no check, separate from niche_compatibility()'s fuzzy score
    # used for ranking in Step C.
    creator_niche_values = (
        creator.instagram_primary_niche,
        creator.youtube_primary_niche,
        creator.content_niche,
    )
    creator_niches = {
        niche.strip().lower()
        for value in creator_niche_values
        for niche in (value or "").split(",")
        if niche.strip()
    }
    if creator_niches:
        brand_niche_match = or_(*[func.lower(BrandRaw.niche) == n for n in creator_niches])
        collaborator_niche_match = BrandRaw.id.in_(
            db.query(BrandInstagramUser.brand_raw_id)
            .join(InstagramUser, InstagramUser.id == BrandInstagramUser.instagram_user_id)
            .filter(
                InstagramUser.user_type != "commenter",
                func.lower(InstagramUser.niche).in_(creator_niches),
            )
        )
        # Reverse-engineering path: a brand also passes if its test
        # partnership evidence links it to a content_creator_re row whose
        # source niche matches. Use the evidence table directly so brands
        # discovered only through reverse engineering are not missed.
        re_creator_niche_match = BrandRaw.id.in_(
            db.query(TestCreatorBrandPartnershipPost.brand_raw_id)
            .join(
                ContentCreatorRE,
                ContentCreatorRE.id == TestCreatorBrandPartnershipPost.content_creator_re_id,
            )
            .filter(
                func.lower(ContentCreatorRE.niche).in_(creator_niches),
            )
        )
        match_clauses = [brand_niche_match, collaborator_niche_match, re_creator_niche_match]

        query = query.filter(or_(*match_clauses))

    # Gender hard filter — only applied when the creator's gender is male or
    # female. A brand passes if ANY of:
    #   - brands_raw.target_audience_gender is the creator's gender or "both"
    #   - at least one of the brand's Instagram collaborators (non-commenter)
    #     has the creator's gender, via a sponsorship_confidence >= 90 post
    #   - at least one reverse-engineered creator (instagram_users row with
    #     user_type "contentcreatorRE") has the creator's gender, via a
    #     sponsorship_confidence >= 90 test partnership post
    creator_gender = (creator.gender or "").strip().lower()
    if apply_gender_filter and creator_gender in ("male", "female"):
        audience_gender_match = func.lower(func.trim(BrandRaw.target_audience_gender)).in_((creator_gender, "both"))
        collaborator_gender_match, re_creator_gender_match = _creator_gender_evidence(db, creator_gender)
        query = query.filter(or_(audience_gender_match, collaborator_gender_match, re_creator_gender_match))

    return query, distance_expr


def _creator_gender_evidence(db: Session, gender: str):
    """
    (collaborator_match, re_creator_match) filter clauses: the brand has at
    least one creator of `gender` on a sponsorship_confidence >= 90 post —
      - a non-commenter Instagram collaborator on the brand's own posts
      - a reverse-engineered creator (instagram_users user_type
        "contentcreatorRE") on a test partnership post
    Shared by the v1/v2 gender hard filter and v3's NULL-audience rule.
    """
    collaborator_match = BrandRaw.id.in_(
        db.query(BrandInstagramUser.brand_raw_id)
        .join(InstagramUser, InstagramUser.id == BrandInstagramUser.instagram_user_id)
        .join(InstagramPost, InstagramPost.post_id == InstagramUser.post_id)
        .filter(
            InstagramUser.user_type != "commenter",
            func.lower(func.trim(InstagramUser.gender)) == gender,
            InstagramPost.brand_raw_id == BrandInstagramUser.brand_raw_id,
            InstagramPost.sponsorship_confidence >= 90,
        )
    )
    re_creator_match = BrandRaw.id.in_(
        db.query(TestCreatorBrandPartnershipPost.brand_raw_id)
        .join(
            InstagramUser,
            func.lower(InstagramUser.username) == func.lower(TestCreatorBrandPartnershipPost.creator_username),
        )
        .filter(
            InstagramUser.user_type == "contentcreatorRE",
            func.lower(func.trim(InstagramUser.gender)) == gender,
            TestCreatorBrandPartnershipPost.sponsorship_confidence >= 90,
        )
    )
    return collaborator_match, re_creator_match


def brand_ids_with_creator_gender(db: Session, brand_ids: list[int], gender: str) -> set[int]:
    """
    Which of `brand_ids` have at least one creator of `gender` (male /
    female) on a sponsorship_confidence >= 90 post — the same evidence the
    v1/v2 gender hard filter accepts. Empty set for any other gender.
    """
    gender = (gender or "").strip().lower()
    if not brand_ids or gender not in ("male", "female"):
        return set()
    collaborator_match, re_creator_match = _creator_gender_evidence(db, gender)
    return {
        brand_id for (brand_id,) in db.query(BrandRaw.id).filter(
            BrandRaw.id.in_(brand_ids), or_(collaborator_match, re_creator_match),
        )
    }


def hard_filtered_brands(
    db: Session, creator: CreatorProfile, limit: int | None = None, apply_gender_filter: bool = True,
) -> list:
    """
    Every brand passing the Step A hard filters for `creator`, as
    (BrandRaw, BrandProfile, distance) tuples nearest-first by embedding,
    optionally capped at `limit`. [] if the creator has no embedding.

    apply_gender_filter=False skips only the gender hard filter (v3 LLM
    ranking judges gender fit itself); every other filter still applies.
    """
    if creator.embedding is None:
        return []
    query, distance_expr = _hard_filtered_query(db, creator, apply_gender_filter=apply_gender_filter)
    query = query.order_by(distance_expr)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def get_matches(
    db: Session,
    creator_id: int,
    limit: int = 100,
    offset: int = 0,
    *,
    include_total: bool = False,
) -> list[dict] | tuple[list[dict], int]:
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
        return ([], 0) if include_total else []
    if creator.embedding is None:
        logger.info("Matching: creator_id=%d has no embedding yet — run Stage 2 first", creator_id)
        return ([], 0) if include_total else []

    query, distance_expr = _hard_filtered_query(db, creator)

    total = query.count()

    # Step B — semantic shortlist (single indexed pgvector query)
    query = query.order_by(distance_expr)
    shortlist = query.all()

    if not shortlist:
        logger.info("Matching: creator_id=%d — no qualifying brands after hard filters", creator_id)
        return ([], total) if include_total else []

    # Step C — tagline-points scoring + match text (one shared reasons pass)
    results = []
    for brand, profile, _distance in shortlist:
        reason_rows = collect_match_reasons(creator, brand, profile, db)
        scored = score_match(db, creator, brand, reason_rows)
        reasons = generate_match_reasons(creator, brand, profile, reasons=reason_rows)
        results.append({
            "brand_raw_id": brand.id,
            "brand_name":   brand.name,
            "website":      brand.website,
            "niche":        brand.niche,
            "total_score":  round(scored["total_score"], 4),
            "niche_tier":   scored["niche_tier"],
            "dimensions":   scored["dimensions"],
            "reasons":      reasons,
        })

    # Niche tier first (see scoring._niche_tier), then score within a tier.
    results.sort(key=lambda r: (r["niche_tier"], r["total_score"]), reverse=True)
    logger.info("length======== %d", len(results))
    print("length========", len(results))

    logger.info(
        "Matching: creator_id=%d -> %d shortlisted, returning %d (offset=%d)",
        creator_id, len(results), min(limit, max(0, len(results) - offset)), offset,
    )
    page = results[offset:offset + limit]
    return (page, total) if include_total else page
