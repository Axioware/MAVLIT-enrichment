from pydantic import BaseModel

from pipeline.db import BrandRaw, ContractReview, CreatorProfile, Pitch, RateEstimate


class CreatorProfileResponse(BaseModel):
    id:         int
    email:      str
    is_active:  bool

    full_name:        str | None = None
    creator_handle:   str | None = None
    location_city:    str | None = None
    location_country: str | None = None
    bio_tagline:      str | None = None

    content_niche:       str | None = None
    sub_niches:          list[str] | None = None
    content_description: str | None = None
    excluded_categories:  list[str] | None = None

    instagram_handle: str | None = None
    youtube_channel:   str | None = None
    facebook_page:     str | None = None
    substack_url:      str | None = None
    substack_subscribers: int | None = None

    instagram_followers: int | None = None
    instagram_following: int | None = None
    youtube_followers:   int | None = None
    facebook_followers:  int | None = None
    facebook_following:  int | None = None

    primary_platform: str | None = None
    follower_count:   int | None = None

    audience_gender_male_pct:   float | None = None
    audience_gender_female_pct: float | None = None
    audience_age_bracket:       str | None = None
    audience_age_min:           int | None = None
    audience_age_max:           int | None = None
    audience_top_countries:     list[dict] | None = None

    creator_tier: str | None = None
    content_tags: list[str] | None = None
    created_at: str
    updated_at: str | None = None


def profile_to_response(row: CreatorProfile) -> CreatorProfileResponse:
    return CreatorProfileResponse(
        id=row.id,
        email=row.email,
        is_active=row.is_active,
        full_name=row.full_name,
        creator_handle=row.creator_handle,
        location_city=row.location_city,
        location_country=row.location_country,
        bio_tagline=row.bio_tagline,
        content_niche=row.content_niche,
        sub_niches=row.sub_niches,
        content_description=row.content_description,
        excluded_categories=row.excluded_categories,
        instagram_handle=row.instagram_handle,
        youtube_channel=row.youtube_channel,
        facebook_page=row.facebook_page,
        substack_url=row.substack_url,
        substack_subscribers=row.substack_subscribers,
        instagram_followers=row.instagram_followers,
        instagram_following=row.instagram_following,
        youtube_followers=row.youtube_followers,
        facebook_followers=row.facebook_followers,
        facebook_following=row.facebook_following,
        primary_platform=row.primary_platform,
        follower_count=row.follower_count,
        audience_gender_male_pct=row.audience_gender_male_pct,
        audience_gender_female_pct=row.audience_gender_female_pct,
        audience_age_bracket=row.audience_age_bracket,
        audience_age_min=row.audience_age_min,
        audience_age_max=row.audience_age_max,
        audience_top_countries=row.audience_top_countries,
        creator_tier=row.creator_tier,
        content_tags=row.content_tags,
        created_at=str(row.created_at) if row.created_at else "",
        updated_at=str(row.updated_at) if row.updated_at else None,
    )


#  Brand detail (pipeline/brand_detail.py -> api/brands.py)

class PlatformSignals(BaseModel):
    instagram: str
    youtube:   str


class CreatorTierFit(BaseModel):
    typical_tier:   str | None = None
    followers_low:  int | None = None
    followers_high: int | None = None


class TopContact(BaseModel):
    name:  str | None = None
    email: str | None = None
    title: str | None = None


class BrandDetailResponse(BaseModel):
    brand_id: int
    name: str | None = None
    short_bio: str | None = None
    description: str | None = None
    niche: str | None = None
    creator_tier_fit: CreatorTierFit
    last_partnership_date: str | None = None
    meta_ads_active: bool | None = None
    platform_signals: PlatformSignals
    sponsorship_activity_score: float | None = None
    top_contact: TopContact | None = None


def brand_detail_to_response(detail: dict) -> BrandDetailResponse:
    return BrandDetailResponse(
        brand_id=detail["brand_id"],
        name=detail["name"],
        short_bio=detail["short_bio"],
        description=detail["description"],
        niche=detail["niche"],
        creator_tier_fit=CreatorTierFit(**detail["creator_tier_fit"]),
        last_partnership_date=detail["last_partnership_date"],
        meta_ads_active=detail["meta_ads_active"],
        platform_signals=PlatformSignals(**detail["platform_signals"]),
        sponsorship_activity_score=detail["sponsorship_activity_score"],
        top_contact=TopContact(**detail["top_contact"]) if detail["top_contact"] else None,
    )


#  Saved brands (api/saved_brands.py)

class SavedBrandResponse(BaseModel):
    brand_id: int
    name:      str | None = None
    niche:     str | None = None
    short_bio: str | None = None
    saved_at:  str


def saved_brand_to_response(brand: BrandRaw, saved_at) -> SavedBrandResponse:
    return SavedBrandResponse(
        brand_id=brand.id,
        name=brand.name,
        niche=brand.niche,
        short_bio=brand.description,
        saved_at=str(saved_at) if saved_at else "",
    )


#  Pitches (pipeline/pitching.py -> api/pitches.py)

class PitchRequest(BaseModel):
    is_custom: bool
    brand_id: int | None = None
    custom_brand_name: str | None = None
    story: str
    product_reference: str | None = None
    past_brand_partnership: str | None = None
    content_link: str | None = None


class PitchResponse(BaseModel):
    id: int
    brand_id: int | None = None
    brand_name: str
    is_custom: bool
    story: str
    product_reference: str | None = None
    past_brand_partnership: str | None = None
    content_link: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    pitch_text: str | None = None
    status: str
    created_at: str


def pitch_to_response(pitch: Pitch) -> PitchResponse:
    return PitchResponse(
        id=pitch.id,
        brand_id=pitch.brand_raw_id,
        brand_name=pitch.brand_name,
        is_custom=pitch.is_custom,
        story=pitch.story,
        product_reference=pitch.product_reference,
        past_brand_partnership=pitch.past_brand_partnership,
        content_link=pitch.content_link,
        contact_name=pitch.contact_name,
        contact_email=pitch.contact_email,
        pitch_text=pitch.pitch_text,
        status=pitch.status,
        created_at=str(pitch.created_at) if pitch.created_at else "",
    )


#  Dashboard (pipeline/dashboard.py -> api/dashboard.py)

class DashboardResponse(BaseModel):
    brand_matches:     int
    active_deals:      int
    saved_brands:      int
    verified_contacts: int


#  Rate intelligence (pipeline/rate_intelligence.py -> api/advisory.py)

class RateIntelligenceRequest(BaseModel):
    brand_id: int
    platform: str
    deliverable_type: str
    exclusivity: str | None = None
    usage: str | None = None
    duration_months: int | None = None


class RateIntelligenceResponse(BaseModel):
    id: int
    rate_min:  int | None = None
    rate_max:  int | None = None
    currency:  str | None = None
    reasoning: str | None = None
    created_at: str


def rate_estimate_to_response(estimate: RateEstimate) -> RateIntelligenceResponse:
    return RateIntelligenceResponse(
        id=estimate.id,
        rate_min=estimate.rate_min,
        rate_max=estimate.rate_max,
        currency=estimate.currency,
        reasoning=estimate.reasoning,
        created_at=str(estimate.created_at) if estimate.created_at else "",
    )


#  Contract advice (pipeline/contract_advice.py -> api/advisory.py)

class ContractAdviceRequest(BaseModel):
    contract_text: str


class ContractAdviceResponse(BaseModel):
    id: int
    looks_good: bool | None = None
    issues: list[str] = []
    summary: str | None = None
    created_at: str


def contract_review_to_response(review: ContractReview) -> ContractAdviceResponse:
    return ContractAdviceResponse(
        id=review.id,
        looks_good=review.looks_good,
        issues=review.issues or [],
        summary=review.summary,
        created_at=str(review.created_at) if review.created_at else "",
    )
