"""
pipeline/matching/scoring.py

Stage 3 Step C — weighted scoring across 7 dimensions, per the matching
design doc. Each dimension is normalized to 0.0-1.0; missing data yields
None (not 0) so it can be excluded and its weight redistributed among the
dimensions that ARE available, rather than unfairly penalizing a brand or
creator just because a signal hasn't been computed yet.

Weights (sum to 1.0):
  niche_match           0.25  — brands_raw.niche vs creator_profiles.content_niche
  sponsorship_activity  0.20  — live per creator-brand formula, see _score_sponsorship_activity()
  audience_demographics 0.20  — gender/age/country overlap
  creator_tier_fit      0.15  — typical_creator_tier vs creator_tier
  semantic_similarity   0.10  — cosine similarity from the Stage 3B pgvector search
  platform_match        0.05  — brand_match_profile.has_* vs creator's primary_platform
  geo_match             0.05  — brand operating_area/country vs creator audience_top_countries

Tranco rank, HQ country, and website traffic tier are deliberately excluded
— per the design doc, they don't measure fit.

sponsorship_activity is computed live per creator-brand pair (NOT read from
the static brand_match_profile.sponsorship_activity_score column — that
column is still computed by brand_signals.py's compute_sponsorship_activity
and is only used by the Stage 3 activity-floor hard filter in matcher.py,
which runs before this scoring step and is unaffected by this function).
Its components (meta ads, most-recent-post recency window, follower/
gender/age-group diff scores) are an uncapped sum by formula, but the
final value is clamped to 0.0-1.0 like every other dimension, so a strong
showing here can't push a match's overall total_score past 100%.
"""

from sqlalchemy.orm import Session

from pipeline.db import BrandProfile, BrandRaw, CreatorProfile, InstagramPost, YoutubeSponsorship
from pipeline.enrichment.initial_brand_scoring import _days_since
from pipeline.matching.country_normalize import countries_match, normalize_country
from pipeline.matching.niche_compatibility import niche_compatibility

WEIGHTS: dict[str, float] = {
    "niche_match":           0.25,
    "sponsorship_activity":  0.20,
    "audience_demographics": 0.20,
    "creator_tier_fit":      0.15,
    "semantic_similarity":   0.10,
    "platform_match":        0.05,
    "geo_match":             0.05,
}

# instagram_users.py's LLM demographics classifier buckets ages into these —
# the label is already the numeric range; "60_plus" is open-ended so it's
# given an arbitrary upper bound just for overlap-fraction math against a
# creator's age_min/max.
_AGE_BUCKET_RANGES: dict[str, tuple[int, int]] = {
    "12_16":   (12, 16),
    "17_22":   (17, 22),
    "23_28":   (23, 28),
    "29_35":   (29, 35),
    "36_45":   (36, 45),
    "46_60":   (46, 60),
    "60_plus": (60, 100),
}

_TIER_ORDER = ["nano", "micro", "macro", "mega"]

_PLATFORM_FLAG_ATTR = {
    "instagram": "has_instagram",
    "youtube":   "has_youtube",
    "facebook":  "has_facebook",
}


def _score_niche(creator: CreatorProfile, brand: BrandRaw) -> float | None:
    return niche_compatibility(creator.content_niche, brand.niche)


# --- sponsorship_activity components ---

_RECENCY_BUCKETS = [(7, 1.0), (14, 0.8), (30, 0.6), (90, 0.3), (180, 0.15)]

_AGE_BUCKET_ORDER = list(_AGE_BUCKET_RANGES.keys())


def _meta_ads_component(profile: BrandProfile) -> float:
    score = 0.0
    if profile.meta_ads_active:
        score += 0.1
    if profile.meta_ads_recency_days is not None:
        days = profile.meta_ads_recency_days or 1   # 0 days old -> treat as 1, so score is 0.3
        score += (1.0 / days) * 0.3
    if profile.meta_ads_no_end_date:
        score += 0.1
    if profile.meta_ads_count is not None and profile.meta_ads_count > 5:
        score += 0.1
    return score


def _bucketed_post_score(days_list: list[int]) -> float:
    """Score is just the matched window's weight, not multiplied by how
    many posts/videos fall in it — only how recent the MOST RECENT one is
    matters. Checks 7/14/30/90/180-day windows in that order and returns
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


def _dominant_bucket(age_groups: dict | None) -> str | None:
    if not age_groups:
        return None
    return max(age_groups.items(), key=lambda kv: kv[1])[0]


def _age_group_hop_score(creator_bucket: str | None, brand_age_groups: dict | None) -> float | None:
    """
    creator_bucket may be a single bracket or a comma-separated list (a
    creator can select more than one, e.g. "12_16, 17_22") — same
    multi-value convention as content_niche/niche_compatibility(). Each
    selected bracket is checked against the brand's dominant bucket and the
    best (lowest-hop) score wins.
    """
    brand_bucket = _dominant_bucket(brand_age_groups)
    if not creator_bucket or not brand_bucket:
        return None
    if brand_bucket not in _AGE_BUCKET_ORDER:
        return None
    brand_idx = _AGE_BUCKET_ORDER.index(brand_bucket)

    best: float | None = None
    for bucket in creator_bucket.split(","):
        bucket = bucket.strip()
        if bucket not in _AGE_BUCKET_ORDER:
            continue
        hop = abs(_AGE_BUCKET_ORDER.index(bucket) - brand_idx)
        score = 1.0 if hop == 0 else (0.5 if hop == 1 else 0.0)
        if best is None or score > best:
            best = score
    return best


def _score_sponsorship_activity(
    db: Session, creator: CreatorProfile, brand: BrandRaw, profile: BrandProfile | None
) -> float | None:
    """
    Live per creator-brand composite — see the module docstring for why
    this doesn't just read brand_match_profile.sponsorship_activity_score.
    Sum of:
      - meta ads component (0-0.6): meta_ads_active +0.1, meta_ads_recency_days
        -> (1/days)*0.3, meta_ads_no_end_date +0.1, meta_ads_count > 5 +0.1
      - youtube_post_score + instagram_post_score: the weight of whichever
        7/14/30/90/180-day window the MOST RECENT sponsorship/paid-
        partnership post falls into (not multiplied by how many fall in it)
      - instagram_tier_score: brand's avg IG collaborator followers vs the
        creator's own instagram_followers, via _diff_pct_score
      - gender diff score, per gender (male, female), via _diff_pct_score
      - age-group hop score: 1.0 exact match, 0.5 one bucket apart, else 0.0
    Returns None only if there's no brand profile at all to compare against.
    """
    if profile is None:
        return None

    score = _meta_ads_component(profile)

    yt_days = [
        d for (published_at,) in db.query(YoutubeSponsorship.published_at)
        .filter(YoutubeSponsorship.brand_raw_id == brand.id)
        .all()
        for d in [_days_since(published_at)] if d is not None
    ]
    score += _bucketed_post_score(yt_days)

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
    score += _bucketed_post_score(ig_days)

    if profile.avg_ig_collaborator_followers is not None and creator.instagram_followers is not None:
        score += _diff_pct_score(profile.avg_ig_collaborator_followers, creator.instagram_followers)

    if profile.audience_gender_male_pct is not None and creator.audience_gender_male_pct is not None:
        score += _diff_pct_score(profile.audience_gender_male_pct, creator.audience_gender_male_pct)
    if profile.audience_gender_female_pct is not None and creator.audience_gender_female_pct is not None:
        score += _diff_pct_score(profile.audience_gender_female_pct, creator.audience_gender_female_pct)

    age_score = _age_group_hop_score(creator.audience_age_bracket, profile.audience_age_groups)
    if age_score is not None:
        score += age_score

    # Capped like every other dimension (0.0-1.0) — the components above are
    # an uncapped sum by formula, but the overall match % can't exceed 100%.
    return max(0.0, min(1.0, score))


def _age_overlap_score(age_min: int | None, age_max: int | None, brand_age_groups: dict | None) -> float | None:
    if age_min is None or age_max is None or not brand_age_groups:
        return None
    total = 0.0
    overlap = 0.0
    for bucket, pct in brand_age_groups.items():
        rng = _AGE_BUCKET_RANGES.get(bucket)
        if not rng or not isinstance(pct, (int, float)):
            continue
        b_lo, b_hi = rng
        total += pct
        lo, hi = max(age_min, b_lo), min(age_max, b_hi)
        if hi >= lo:
            bucket_span = max(1, b_hi - b_lo)
            overlap += pct * min(1.0, (hi - lo + 1) / bucket_span)
    return (overlap / total) if total > 0 else None


def _country_overlap_score(creator_countries: list | None, brand_countries: list | None) -> float | None:
    if not creator_countries or not brand_countries:
        return None
    # Normalized so e.g. creator-side "US" and brand-side "America" are
    # recognized as the same country (see country_normalize.py docstring).
    c_map = {normalize_country(c["country"]): c.get("pct", 0) or 0 for c in creator_countries if c.get("country")}
    b_map = {normalize_country(c["country"]): c.get("pct", 0) or 0 for c in brand_countries if c.get("country")}
    if not c_map or not b_map:
        return None
    overlap = sum(min(c_map.get(k, 0), b_map.get(k, 0)) for k in set(c_map) | set(b_map))
    return max(0.0, min(1.0, overlap))


def _score_audience_demographics(creator: CreatorProfile, profile: BrandProfile | None) -> float | None:
    if profile is None:
        return None
    components = []
    if creator.audience_gender_male_pct is not None and profile.audience_gender_male_pct is not None:
        components.append(1.0 - abs(creator.audience_gender_male_pct - profile.audience_gender_male_pct))

    age_score = _age_overlap_score(creator.audience_age_min, creator.audience_age_max, profile.audience_age_groups)
    if age_score is not None:
        components.append(age_score)

    country_score = _country_overlap_score(creator.audience_top_countries, profile.audience_top_countries)
    if country_score is not None:
        components.append(country_score)

    return (sum(components) / len(components)) if components else None


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


def _score_geo_match(creator: CreatorProfile, brand: BrandRaw) -> float | None:
    if not creator.audience_top_countries:
        return None
    brand_geo = brand.operating_area or brand.country
    if not brand_geo:
        return None
    if brand_geo.strip().lower() == "worldwide":
        # Operates everywhere — full credit for wherever the creator's audience is.
        top = creator.audience_top_countries[0]
        return top.get("pct", 0) or 0
    best = 0.0
    for c in creator.audience_top_countries:
        if countries_match(c.get("country"), brand_geo):
            best = max(best, c.get("pct", 0) or 0)
    return best   # 0.0 is a real "no overlap found" answer here, not "unknown"


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
        "niche_match":           _score_niche(creator, brand),
        "sponsorship_activity":  _score_sponsorship_activity(db, creator, brand, profile),
        "audience_demographics": _score_audience_demographics(creator, profile),
        "creator_tier_fit":      _score_creator_tier_fit(creator, profile),
        "semantic_similarity":   _score_semantic_similarity(cosine_distance),
        "platform_match":        _score_platform_match(creator, profile),
        "geo_match":             _score_geo_match(creator, brand),
    }

    available_weight = sum(WEIGHTS[k] for k, v in raw.items() if v is not None)
    if available_weight > 0:
        total = sum(raw[k] * WEIGHTS[k] for k in raw if raw[k] is not None) / available_weight
    else:
        total = 0.0

    return {
        "total_score": total,
        "dimensions": {k: {"score": raw[k], "weight": WEIGHTS[k]} for k in raw},
    }
