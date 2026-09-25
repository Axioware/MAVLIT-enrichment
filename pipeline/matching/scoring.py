"""
pipeline/matching/scoring.py

Stage 3 Step C — weighted scoring across 5 dimensions, per the matching
design doc. Each dimension is normalized to 0.0-1.0; missing data yields
None (not 0) so it can be excluded and its weight redistributed among the
dimensions that ARE available, rather than unfairly penalizing a brand or
creator just because a signal hasn't been computed yet.

Weights (sum to 1.0):
    niche_match           0.305263 — brand/linked creator niches vs MAVLIT creator niches
    sponsorship_activity  0.252632 — live per creator-brand formula, see _score_sponsorship_activity()
    creator_tier_fit      0.2     — typical_creator_tier vs creator_tier
    semantic_similarity   0.147368 — cosine similarity from the Stage 3B pgvector search
    platform_match        0.094737 — brand_match_profile.has_* vs creator's primary_platform

Tranco rank, HQ country, and website traffic tier are deliberately excluded
— per the design doc, they don't measure fit.

sponsorship_activity is computed live per creator-brand pair (NOT read from
the static brand_match_profile.sponsorship_activity_score column — that
column is still computed by brand_signals.py's compute_sponsorship_activity
and is only used by the Stage 3 activity-floor hard filter in matcher.py,
which runs before this scoring step and is unaffected by this function).
Its components (meta ads, most-recent-post recency windows, and follower
fit) are equally weighted. Audience age and gender are not scoring dimensions.
"""

from sqlalchemy.orm import Session

from pipeline.db import (
    BrandInstagramUser,
    BrandProfile,
    BrandRaw,
    CreatorProfile,
    InstagramPost,
    InstagramUser,
    TestCreatorBrandPartnershipPost,
    YoutubeSponsorship,
)
from pipeline.enrichment.initial_brand_scoring import _days_since
from pipeline.matching.niche_compatibility import niche_compatibility

WEIGHTS: dict[str, float] = {
    "niche_match":           0.30526315789473685,
    "sponsorship_activity":  0.25263157894736843,
    "creator_tier_fit":      0.2,
    "semantic_similarity":   0.14736842105263157,
    "platform_match":        0.09473684210526316,
}

_TIER_ORDER = ["nano", "micro", "macro", "mega"]

_PLATFORM_FLAG_ATTR = {
    "instagram": "has_instagram",
    "youtube":   "has_youtube",
    "facebook":  "has_facebook",
}


def _score_niche(db: Session, creator: CreatorProfile, brand: BrandRaw) -> float | None:
    niches = []
    seen = set()
    for value in (creator.instagram_primary_niche, creator.youtube_primary_niche):
        for niche in (value or "").split(","):
            normalized = niche.strip()
            key = normalized.casefold()
            if normalized and key not in seen:
                niches.append(normalized)
                seen.add(key)

    # Keep supporting profiles created before platform-specific niches existed.
    if not niches:
        niches = [niche.strip() for niche in (creator.content_niche or "").split(",") if niche.strip()]

    if not niches:
        return None

    brand_niches = [brand.niche] if brand.niche else []
    brand_niches.extend(
        niche for (niche,) in db.query(InstagramUser.niche)
        .join(BrandInstagramUser, BrandInstagramUser.instagram_user_id == InstagramUser.id)
        .filter(
            BrandInstagramUser.brand_raw_id == brand.id,
            InstagramUser.user_type != "commenter",
            InstagramUser.niche.isnot(None),
        )
        .distinct()
        .all()
        if niche and niche.strip()
    )

    scores = [niche_compatibility(",".join(niches), brand_niche) for brand_niche in brand_niches]
    scores = [score for score in scores if score is not None]
    return max(scores) if scores else None


# --- sponsorship_activity components ---

_RECENCY_BUCKETS = [(30, 1.0), (60, 0.8), (90, 0.6), (180, 0.3), (200, 0.15)]


def _meta_ads_component(profile: BrandProfile) -> float:
    # Meta Ads enrichment is currently inactive, so give this component full
    # credit instead of penalizing every match for unavailable data.
    return 1.0


def _bucketed_post_score(days_list: list[int]) -> float:
    """Score is just the matched window's weight, not multiplied by how
    many posts/videos fall in it — only how recent the MOST RECENT one is
    matters. Checks 30/60/90/180/200-day windows in that order and returns
    the weight of the first window the most recent item falls into."""
    if not days_list:
        return 0.0
    most_recent = min(days_list)
    for max_days, weight in _RECENCY_BUCKETS:
        if most_recent <= max_days:
            return weight
    return 0.0


def _diff_pct_score(a: float, b: float) -> float:
    """abs-difference-over-average ratio expressed as a percentage, then
    inverted so a smaller gap scores higher. A perfect match (diff% == 0)
    is floored to 1 before inverting — same zero-handling convention as
    meta_ads_recency_days — rather than dividing by zero."""
    denom = a + b / 2
    if denom == 0:
        return 0.0
    diff_pct = abs(a - b) / denom * 100
    diff_pct = diff_pct or 1.0
    return (1.0 / diff_pct) * 2.0


def _score_sponsorship_activity(
    db: Session, creator: CreatorProfile, brand: BrandRaw, profile: BrandProfile | None
) -> float | None:
    """
    Live per creator-brand composite — see the module docstring for why
    this doesn't just read brand_match_profile.sponsorship_activity_score.
        The four remaining components are equally weighted:
            - meta ads activity
            - most recent YouTube sponsorship recency
            - most recent Instagram/content_creatorRE sponsorship recency
            - Instagram collaborator follower fit
    Returns None only if there's no brand profile at all to compare against.
    """
    if profile is None:
        return None

    components = [_meta_ads_component(profile)]

    yt_days = [
        d for (published_at,) in db.query(YoutubeSponsorship.published_at)
        .filter(YoutubeSponsorship.brand_raw_id == brand.id)
        .all()
        for d in [_days_since(published_at)] if d is not None
    ]
    components.append(_bucketed_post_score(yt_days))

    ig_rows = (
        db.query(
            InstagramPost.timestamp, InstagramPost.paid_partnership,
            InstagramPost.sponsors, InstagramPost.tagged_users, InstagramPost.coauthor_producers,
        )
        .filter(InstagramPost.brand_raw_id == brand.id)
        .all()
    )
    ig_days = [
        d for (ts, paid, sponsors, tagged, coauthor) in ig_rows
        if (paid or sponsors or tagged or coauthor)
        for d in [_days_since(ts)] if d is not None
    ]

    # Reverse-engineered content_creatorRE partnerships are stored in their
    # evidence table rather than instagram_posts, so include their confirmed
    # post timestamps in the same Instagram recency score.
    re_ig_days = [
        d for (post_timestamp,) in db.query(TestCreatorBrandPartnershipPost.post_timestamp)
        .filter(
            TestCreatorBrandPartnershipPost.brand_raw_id == brand.id,
            TestCreatorBrandPartnershipPost.sponsorship_confidence >= 90,
        )
        .all()
        for d in [_days_since(post_timestamp)] if d is not None
    ]
    ig_days.extend(re_ig_days)
    components.append(_bucketed_post_score(ig_days))

    if profile.avg_ig_collaborator_followers is not None and creator.instagram_followers is not None:
        components.append(_diff_pct_score(profile.avg_ig_collaborator_followers, creator.instagram_followers))
    else:
        components.append(0.0)

    return sum(components) / len(components)


def _score_creator_tier_fit(creator: CreatorProfile, profile: BrandProfile | None) -> float | None:
    if profile is None or not creator.creator_tier or not profile.typical_creator_tier:
        return None
    try:
        ci = _TIER_ORDER.index(creator.creator_tier)
        bi = _TIER_ORDER.index(profile.typical_creator_tier)
    except ValueError:
        return None
    return 1.0 - abs(ci - bi) / (len(_TIER_ORDER) - 1)


def _score_semantic_similarity(cosine_distance: float | None) -> float | None:
    if cosine_distance is None:
        return None
    return max(0.0, min(1.0, 1.0 - cosine_distance))


def _score_platform_match(creator: CreatorProfile, profile: BrandProfile | None) -> float | None:
    if profile is None or not creator.primary_platform:
        return None
    attr = _PLATFORM_FLAG_ATTR.get(creator.primary_platform.strip().lower())
    if attr is None:
        return None
    flag = getattr(profile, attr)
    return None if flag is None else (1.0 if flag else 0.0)


def score_match(
    db: Session,
    creator: CreatorProfile,
    brand: BrandRaw,
    profile: BrandProfile | None,
    cosine_distance: float | None,
) -> dict:
    """
    Returns {"total_score": 0.0-1.0, "dimensions": {name: {"score": float|None, "weight": float}}}.
    Weights of dimensions with a None score are excluded and the remainder
    renormalized to sum to 1.0, so missing data never deflates a match's
    score relative to one where every dimension happened to be computable.
    """
    raw: dict[str, float | None] = {
        "niche_match":           _score_niche(db, creator, brand),
        "sponsorship_activity":  _score_sponsorship_activity(db, creator, brand, profile),
        "creator_tier_fit":      _score_creator_tier_fit(creator, profile),
        "semantic_similarity":   _score_semantic_similarity(cosine_distance),
        "platform_match":        _score_platform_match(creator, profile),
    }

    available_weight = sum(WEIGHTS[k] for k, v in raw.items() if v is not None)
    if available_weight > 0:
        total = sum(raw[k] * WEIGHTS[k] for k in raw if raw[k] is not None) / available_weight
    else:
        total = 0.0

    # The weighted score is a normalized 0..1 signal, but some component
    # formulas (notably the follower-fit subscore) can temporarily exceed 1.0
    # before the weighted average is taken. Clamp the final total so the UI and
    # downstream logic never display a >100% match score.
    total = max(0.0, min(1.0, total))

    return {
        "total_score": total,
        "dimensions": {k: {"score": raw[k], "weight": WEIGHTS[k]} for k in raw},
    }
