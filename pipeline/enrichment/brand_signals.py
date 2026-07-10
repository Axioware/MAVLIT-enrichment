"""
pipeline/enrichment/brand_signals.py

Stage 1 offline brand-signal computation for the creator-brand matching
algorithm. Writes to brand_match_profile (BrandProfile).

Covers:
  compute_creator_tier_profile(db, brand_raw_id)  — avg collaborator follower/subscriber size, bucketed
  compute_audience_demographics(db, brand_raw_id) — gender/country/age aggregate + sample size
  compute_platform_presence(db, brand_raw_id)     — has_instagram/youtube/facebook/tiktok/twitter booleans
  compute_brand_embedding(db, brand_raw_id)       — prose text + Mistral embedding

NOT covered here:
  sponsorship_activity_score — already computed in initial_brand_scoring.py (writes to initial_brand_score)
  contact signal              — blocked, no Apollo contact-fetch pipeline exists yet

Embedding model: Mistral's "mistral-embed" (1024 dims, fixed — this model
doesn't support a custom output_dimension). Both brand_match_profile.embedding
and creator_profiles.embedding are Vector(1024) to match — they must use
the same model/dimension to be comparable via cosine similarity in Stage 3
matching. embed_text() lives in pipeline/helpers/llm.py so it can be reused
for the creator-side embedding once that's built.

Scope notes:
  Creator tier fit counts Instagram user_type IN ('coauthor_producer',
  'tagged_user', 'mention') as the brand's content creators — everyone
  except 'commenter', which is excluded since commenters are audience, not
  creators the brand worked with.

  Audience demographics counts EVERY Instagram user linked to the brand
  regardless of user_type (mention, coauthor_producer, tagged_user,
  commenter — all of them) since this signal is about the brand's total
  observed social audience, not just its collaborators.
"""

import logging
from collections import Counter

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pipeline.db import (
    BrandInstagramUser,
    BrandProfile,
    BrandRaw,
    InitialBrandScore,
    InstagramUser,
    YoutubeSponsorship,
)
from pipeline.helpers.creator_tier import bucket_creator_tier
from pipeline.helpers.llm import embed_text

logger = logging.getLogger(__name__)

_COLLABORATOR_TYPES = ("coauthor_producer", "tagged_user", "mention")


def _is_known(value: str | None) -> bool:
    return value is not None and value != "unknown"


def _upsert_brand_profile(db: Session, brand_raw_id: int, values: dict) -> None:
    """UPSERT a partial set of fields into brand_match_profile, creating the row if needed."""
    stmt = (
        pg_insert(BrandProfile)
        .values(brand_raw_id=brand_raw_id, **values)
        .on_conflict_do_update(
            index_elements=["brand_raw_id"],
            set_=values,
        )
    )
    db.execute(stmt)
    db.commit()


def compute_creator_tier_profile(db: Session, brand_raw_id: int) -> dict | None:
    """
    Average the follower/subscriber size of creators this brand has actually
    worked with (YouTube sponsorships + Instagram coauthor/tagged/mentioned
    users, excluding commenters), bucketed into nano/micro/macro/mega. Also
    records the highest and lowest follower/subscriber count seen on each
    platform. Writes to brand_match_profile. Returns None if there's no
    creator data at all for this brand.
    """
    yt_counts = [
        c for (c,) in db.query(YoutubeSponsorship.subscriber_count)
        .filter(
            YoutubeSponsorship.brand_raw_id == brand_raw_id,
            YoutubeSponsorship.subscriber_count.isnot(None),
        )
        .all()
    ]

    ig_counts = [
        c for (c,) in db.query(InstagramUser.followers_count)
        .join(BrandInstagramUser, BrandInstagramUser.instagram_user_id == InstagramUser.id)
        .filter(
            BrandInstagramUser.brand_raw_id == brand_raw_id,
            InstagramUser.user_type.in_(_COLLABORATOR_TYPES),
            InstagramUser.followers_count.isnot(None),
        )
        .all()
    ]

    pooled = yt_counts + ig_counts
    if not pooled:
        logger.info("Creator tier profile: brand_raw_id=%d has no collaborator data", brand_raw_id)
        return None

    avg_yt = round(sum(yt_counts) / len(yt_counts)) if yt_counts else None
    avg_ig = round(sum(ig_counts) / len(ig_counts)) if ig_counts else None
    typical_tier = bucket_creator_tier(round(sum(pooled) / len(pooled)))

    values = {
        "avg_yt_creator_subscribers":    avg_yt,
        "avg_ig_collaborator_followers": avg_ig,
        "typical_creator_tier":          typical_tier,
        "youtube_highest": max(yt_counts) if yt_counts else None,
        "youtube_lowest":  min(yt_counts) if yt_counts else None,
        "insta_highest":   max(ig_counts) if ig_counts else None,
        "insta_lowest":    min(ig_counts) if ig_counts else None,
    }
    _upsert_brand_profile(db, brand_raw_id, values)

    logger.info(
        "Creator tier profile: brand_raw_id=%d avg_yt=%s avg_ig=%s tier=%s "
        "yt_range=[%s, %s] ig_range=[%s, %s] (n_yt=%d n_ig=%d)",
        brand_raw_id, avg_yt, avg_ig, typical_tier,
        values["youtube_lowest"], values["youtube_highest"],
        values["insta_lowest"], values["insta_highest"],
        len(yt_counts), len(ig_counts),
    )
    return values


def compute_audience_demographics(db: Session, brand_raw_id: int) -> dict | None:
    """
    Aggregate gender/country/age_group across every Instagram user linked to
    this brand, regardless of user_type (mention, coauthor_producer,
    tagged_user, commenter — all count). Writes to brand_match_profile.
    Returns None if the brand has no linked Instagram users.
    """
    users = (
        db.query(InstagramUser.gender, InstagramUser.country, InstagramUser.age_group)
        .join(BrandInstagramUser, BrandInstagramUser.instagram_user_id == InstagramUser.id)
        .filter(BrandInstagramUser.brand_raw_id == brand_raw_id)
        .all()
    )

    sample_size = len(users)
    if sample_size == 0:
        logger.info("Audience demographics: brand_raw_id=%d has no matching linked users", brand_raw_id)
        return None

    genders = [g for g, _, _ in users if _is_known(g)]
    male_pct   = round(genders.count("male")   / len(genders), 3) if genders else None
    female_pct = round(genders.count("female") / len(genders), 3) if genders else None

    countries = [c for _, c, _ in users if _is_known(c)]
    country_counts = Counter(countries)
    audience_top_countries = [
        {"country": country, "pct": round(count / len(countries), 3)}
        for country, count in country_counts.most_common(5)
    ] if countries else None

    ages = [a for _, _, a in users if _is_known(a)]
    age_counts = Counter(ages)
    audience_age_groups = (
        {age: round(count / len(ages), 3) for age, count in age_counts.items()}
        if ages else None
    )

    values = {
        "audience_gender_male_pct":   male_pct,
        "audience_gender_female_pct": female_pct,
        "audience_top_countries":     audience_top_countries,
        "audience_age_groups":        audience_age_groups,
        "audience_sample_size":       sample_size,
    }
    _upsert_brand_profile(db, brand_raw_id, values)

    logger.info(
        "Audience demographics: brand_raw_id=%d sample_size=%d male_pct=%s top_country=%s",
        brand_raw_id, sample_size, male_pct,
        audience_top_countries[0]["country"] if audience_top_countries else None,
    )
    return values


def compute_platform_presence(db: Session, brand_raw_id: int) -> dict | None:
    """
    Simple booleans for whether the brand has a known handle on each
    platform (from brands_raw). Denormalized onto brand_match_profile so
    matching doesn't need a join back to brands_raw for this check.
    has_tiktok/has_twitter are stored but not scored in V1. Returns None if
    the brand doesn't exist.
    """
    brand = db.query(BrandRaw).filter(BrandRaw.id == brand_raw_id).first()
    if not brand:
        logger.warning("Platform presence: brand_raw_id=%d not found", brand_raw_id)
        return None

    values = {
        "has_instagram": bool(brand.instagram_handle),
        "has_youtube":   bool(brand.youtube_channel_id),
        "has_facebook":  bool(brand.facebook_page or brand.facebook_page_id),
        "has_tiktok":    bool(brand.tiktok_handle),
        "has_twitter":   bool(brand.twitter_handle),
    }
    _upsert_brand_profile(db, brand_raw_id, values)

    logger.info(
        "Platform presence: brand_raw_id=%d instagram=%s youtube=%s facebook=%s tiktok=%s twitter=%s",
        brand_raw_id, values["has_instagram"], values["has_youtube"],
        values["has_facebook"], values["has_tiktok"], values["has_twitter"],
    )
    return values


#  Brand embedding


def _activity_bucket(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 70:
        return "highly active"
    if score >= 30:
        return "moderate"
    return "limited"


def _gender_skew_phrase(male_pct: float | None, female_pct: float | None) -> str | None:
    if male_pct is not None and male_pct >= 0.6:
        return "audience skews mostly male"
    if female_pct is not None and female_pct >= 0.6:
        return "audience skews mostly female"
    if male_pct is not None or female_pct is not None:
        return "audience is gender-balanced"
    return None


def _dominant_age_group(age_groups: dict | None) -> str | None:
    if not age_groups:
        return None
    return max(age_groups.items(), key=lambda kv: kv[1])[0]


def _top_countries_phrase(top_countries: list | None, n: int = 3) -> str | None:
    if not top_countries:
        return None
    names = [c["country"] for c in top_countries[:n] if c.get("country")]
    return ", ".join(names) if names else None


def _active_platforms_phrase(profile: "BrandProfile | None") -> str | None:
    if not profile:
        return None
    platforms = []
    if profile.has_instagram:
        platforms.append("Instagram")
    if profile.has_youtube:
        platforms.append("YouTube")
    if profile.has_facebook:
        platforms.append("Facebook")
    return ", ".join(platforms) if platforms else None


def build_brand_embedding_text(brand: BrandRaw, profile: "BrandProfile | None") -> str:
    """
    Build the prose text that gets embedded for semantic brand matching.
    Only includes a clause for fields that are actually known — never
    fabricates a value for missing data (e.g. omits the activity bucket
    entirely until sponsorship_activity_score has actually been computed).
    """
    parts = [brand.name]

    if brand.niche:
        parts.append(f"in the {brand.niche} space")
    if brand.entity_type:
        parts.append(f"a {brand.entity_type}")

    location = brand.operating_area or brand.country
    if location:
        parts.append(f"operating in {location}")

    if brand.description:
        parts.append(brand.description.strip())

    if profile:
        activity = _activity_bucket(profile.sponsorship_activity_score)
        if activity:
            parts.append(f"{activity} in sponsoring content creators")

        gender_phrase = _gender_skew_phrase(profile.audience_gender_male_pct, profile.audience_gender_female_pct)
        if gender_phrase:
            parts.append(gender_phrase)

        dominant_age = _dominant_age_group(profile.audience_age_groups)
        if dominant_age:
            parts.append(f"dominant audience age group is {dominant_age}")

        countries = _top_countries_phrase(profile.audience_top_countries)
        if countries:
            parts.append(f"top audience countries: {countries}")

        if profile.typical_creator_tier:
            parts.append(f"typically works with {profile.typical_creator_tier}-tier creators")

        platforms = _active_platforms_phrase(profile)
        if platforms:
            parts.append(f"active on {platforms}")

    return ". ".join(parts) + "."


def compute_brand_embedding(db: Session, brand_raw_id: int) -> dict | None:
    """
    Build the brand's embedding_text prose from brands_raw + whatever
    brand_match_profile signals are already computed, then embed it via
    Mistral (mistral-embed). Writes embedding + embedding_text to
    brand_match_profile. Returns None if the brand doesn't exist, or if the
    embed call fails (embedding_text is still worth having on its own, but
    a partial/empty vector would break cosine similarity downstream, so
    nothing is written rather than storing a bad embedding).
    """
    brand = db.query(BrandRaw).filter(BrandRaw.id == brand_raw_id).first()
    if not brand:
        logger.warning("Brand embedding: brand_raw_id=%d not found", brand_raw_id)
        return None

    profile = db.query(BrandProfile).filter(BrandProfile.brand_raw_id == brand_raw_id).first()

    text = build_brand_embedding_text(brand, profile)
    vector = embed_text(text, context=f"brand embedding for brand_raw_id={brand_raw_id}")
    if not vector:
        logger.warning("Brand embedding: brand_raw_id=%d — embed call failed, not writing", brand_raw_id)
        return None

    values = {
        "embedding":      vector,
        "embedding_text": text,
    }
    _upsert_brand_profile(db, brand_raw_id, values)

    logger.info("Brand embedding: brand_raw_id=%d text=%r", brand_raw_id, text[:150])
    return values


def run_brand_signals(db: Session, limit: int = 500, brand_id: int | None = None) -> int:
    """
    Compute creator-tier-fit, audience-demographics, platform-presence, and
    embedding signals for brands that have been scored (initial_brand_score)
    with total_score >= 50.

    NOTE: does not yet gate on Apollo contact discovery — that pipeline
    doesn't exist yet. Runs directly off the score, the same brand set the
    design doc says should be sent to Apollo. Once the Apollo step is built,
    it can run before or after this; both compute functions UPSERT so order
    doesn't matter.

    Pass brand_id to target one specific brand directly (bypasses the score
    filter, for testing).

    Returns number of brands processed.
    """
    query = db.query(BrandRaw.id).join(InitialBrandScore, InitialBrandScore.brand_raw_id == BrandRaw.id)
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    else:
        query = query.filter(InitialBrandScore.total_score >= 50)

    brand_ids = [row.id for row in query.limit(limit).all()]

    if not brand_ids:
        logger.info("Brand signals: no qualifying brands (total_score >= 50)")
        return 0

    logger.info("Brand signals: processing %d brands", len(brand_ids))
    for bid in brand_ids:
        compute_creator_tier_profile(db, bid)
        compute_audience_demographics(db, bid)
        compute_platform_presence(db, bid)
        # embedding runs last — its prose pulls from the three signals above
        compute_brand_embedding(db, bid)

    logger.info("Brand signals: %d brands processed", len(brand_ids))
    return len(brand_ids)
