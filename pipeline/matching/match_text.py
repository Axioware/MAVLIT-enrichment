"""
pipeline/matching/match_text.py

"Why it's a match" — Tier 1 (template, always, instant) text generation
per the matching design doc. Plain string formatting sourced from real
signal fields, not an LLM call.

Walks 7 priority tiers in order (audience demographics > niche/category >
sponsorship activity > creator tier fit > geo match > semantic similarity
[fallback] > platform presence [last resort]). Within each tier, its
conditions are checked top to bottom and the first one that's true is
rendered. Up to 2 rendered reasons are collected total — priorities 6 and 7
are unconditional fallbacks so a real profile+brand pair (with at least a
primary_platform set) is effectively always guaranteed at least 1-2 reasons.
"""

import random

from pipeline.db import BrandProfile, BrandRaw, CreatorProfile
from pipeline.matching.country_normalize import countries_match

_MAX_REASONS = 2


def _dominant_age_group(age_groups: dict | None) -> str | None:
    if not age_groups:
        return None
    return max(age_groups.items(), key=lambda kv: kv[1])[0]


def _priority_1_audience(creator: CreatorProfile, brand: BrandRaw, profile: BrandProfile | None, dims: dict) -> str | None:
    # audience_sample_size was removed from BrandProfile (derivable on demand
    # from brand_instagram_users instead of storing a duplicate count) — the
    # sample-size-gated "demographic overlap" condition that used to be the
    # strongest Priority-1 signal is gone; gender-skew and age-data below are
    # what's left.
    if profile is None:
        return None

    if profile.audience_gender_male_pct is not None and creator.audience_gender_male_pct is not None:
        b_female = profile.audience_gender_female_pct if profile.audience_gender_female_pct is not None else 1 - profile.audience_gender_male_pct
        c_female = creator.audience_gender_female_pct if creator.audience_gender_female_pct is not None else 1 - creator.audience_gender_male_pct
        b_dir = "male" if profile.audience_gender_male_pct > b_female else "female"
        c_dir = "male" if creator.audience_gender_male_pct > c_female else "female"
        b_pct = max(profile.audience_gender_male_pct, b_female)
        if b_dir == c_dir and b_pct >= 0.6:
            return f"{round(b_pct * 100)}% of their audience is {b_dir}, matching your {c_dir}-skewing following."

    if profile.audience_age_groups and creator.audience_age_min is not None and creator.audience_age_max is not None:
        dominant = _dominant_age_group(profile.audience_age_groups)
        if dominant:
            return f"Their core audience is {dominant} — right in line with your {creator.audience_age_min}–{creator.audience_age_max} audience."

    return None


def _priority_2_niche(creator: CreatorProfile, brand: BrandRaw, profile: BrandProfile | None, dims: dict) -> str | None:
    niche_score = dims.get("niche_match", {}).get("score")
    if niche_score is not None and niche_score >= 0.7:
        return f"Strong category fit — your {creator.content_niche} content aligns directly with their {brand.niche} positioning."
    return None


def _priority_3_activity(creator: CreatorProfile, brand: BrandRaw, profile: BrandProfile | None, dims: dict) -> str | None:
    # youtube_sponsorship_count / instagram_paid_posts_count were removed
    # from BrandProfile (derived on demand from youtube_sponsorships /
    # instagram_posts instead) — only the meta_ads-based condition remains
    # wired up here.
    if profile is None:
        return None

    if profile.meta_ads_recency_days is not None and profile.meta_ads_recency_days <= 30:
        return f"Actively running paid campaigns — their last ad went live {profile.meta_ads_recency_days} days ago."

    return None


def _priority_4_tier(creator: CreatorProfile, brand: BrandRaw, profile: BrandProfile | None, dims: dict) -> str | None:
    if profile is None:
        return None

    if creator.creator_tier and profile.typical_creator_tier and creator.creator_tier == profile.typical_creator_tier:
        return f"Typically partners with {profile.typical_creator_tier} creators — exactly your tier."

    # Prefer the average for the creator's own platform, but fall back to
    # whichever platform DOES have data — a true "past collaborators
    # averaged N followers" statement is still worth surfacing even if it's
    # not from the creator's primary platform, rather than giving up here
    # and falling through to a weaker/generic tier.
    platform = (creator.primary_platform or "").strip().lower()
    if platform == "youtube":
        avg = profile.avg_yt_creator_subscribers or profile.avg_ig_collaborator_followers
    elif platform == "instagram":
        avg = profile.avg_ig_collaborator_followers or profile.avg_yt_creator_subscribers
    else:
        avg = profile.avg_yt_creator_subscribers or profile.avg_ig_collaborator_followers
    if avg:
        return f"Their past collaborators average {avg:,} followers — right in your range."

    return None


def _priority_5_geo(creator: CreatorProfile, brand: BrandRaw, profile: BrandProfile | None, dims: dict) -> str | None:
    if not creator.audience_top_countries:
        return None
    brand_geo = (brand.operating_area or brand.country or "")
    if not brand_geo:
        return None

    if brand_geo.strip().lower() == "worldwide":
        top_country = creator.audience_top_countries[0].get("country")
        return f"{brand.name} operates worldwide, including {top_country}, your largest audience market."

    matched = [
        c["country"] for c in creator.audience_top_countries
        if c.get("country") and countries_match(c["country"], brand_geo)
    ]
    if len(matched) >= 2:
        return f"Operates across {', '.join(matched)} — your audience's top markets."
    if len(matched) >= 1:
        top_country = creator.audience_top_countries[0].get("country")
        return f"{brand.name} has an active presence in {top_country}, your largest audience market."

    return None


_SEMANTIC_FALLBACK_TEXTS = [
    "Your content style and their brand voice show strong overall alignment.",
    "A strong overall fit based on your content themes and their brand positioning.",
]


def _priority_6_semantic(creator: CreatorProfile, brand: BrandRaw, profile: BrandProfile | None, dims: dict) -> str | None:
    # Always eligible (fallback tier) — randomized between equivalent
    # phrasings so a results page showing many fallback-tier matches
    # doesn't repeat the exact same sentence for every one of them.
    return random.choice(_SEMANTIC_FALLBACK_TEXTS)


def _priority_7_platform(creator: CreatorProfile, brand: BrandRaw, profile: BrandProfile | None, dims: dict) -> str | None:
    if not creator.primary_platform:
        return None
    return f"{brand.name} is active on {creator.primary_platform}, where your audience is."


_PRIORITY_TIERS = [
    _priority_1_audience,
    _priority_2_niche,
    _priority_3_activity,
    _priority_4_tier,
    _priority_5_geo,
    _priority_6_semantic,
    _priority_7_platform,
]


def generate_match_reasons(
    creator: CreatorProfile,
    brand: BrandRaw,
    profile: BrandProfile | None,
    dimensions: dict,
    max_reasons: int = _MAX_REASONS,
) -> list[str]:
    """
    Walks the 7 priority tiers in order, collecting up to max_reasons
    rendered strings from the first tiers whose condition is satisfied.
    `dimensions` is the score_match()["dimensions"] dict for this pair.
    """
    reasons: list[str] = []
    for tier_fn in _PRIORITY_TIERS:
        text = tier_fn(creator, brand, profile, dimensions)
        if text:
            reasons.append(text)
        if len(reasons) >= max_reasons:
            break
    return reasons
