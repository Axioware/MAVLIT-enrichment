from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, Text, TIMESTAMP, create_engine
from sqlalchemy.dialects.postgresql import insert, JSONB
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, relationship
from sqlalchemy.sql import func

# Source trust scores (higher = more authoritative)
SOURCE_CONFIDENCE = {
    "wikidata":  100,
    "wikipedia": 80,
    "google":    50,
}

from config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


class BrandRaw(Base):
    __tablename__ = "brands_raw"

    id                = Column(Integer, primary_key=True)
    name              = Column(Text, nullable=False)
    name_normalized   = Column(Text, unique=True, nullable=False)
    # Wikidata identity — unique index managed via migration (allows multiple NULLs)
    wikidata_id       = Column(Text)
    entity_type       = Column(Text)
    description       = Column(Text)
    wikipedia_url     = Column(Text)
    niche             = Column(Text, nullable=False)
    source            = Column(Text, nullable=False)
    source_confidence = Column(Integer)
    source_url        = Column(Text)
    # Official website (P856) resolved at seed time
    website           = Column(Text)
    domain            = Column(Text)
    enriched          = Column(Boolean, nullable=False, server_default="false", default=False)
    enrichment_failed = Column(Boolean, nullable=False, server_default="false", default=False)
    country           = Column(Text)
    headquarters      = Column(Text)
    location          = Column(Text)
    operating_area    = Column(Text)
    created_at        = Column(TIMESTAMP(timezone=True), server_default=func.now())

    #  Social media handles (from Wikidata Layer 1 enrichment) 
    instagram_handle    = Column(Text)
    youtube_channel_id  = Column(Text)
    twitter_handle      = Column(Text)
    facebook_page       = Column(Text)
    facebook_page_id    = Column(Text)   # numeric Page ID resolved via Graph API
    tiktok_handle       = Column(Text)
    linkedin_id         = Column(Text)

    #  Website enrichment 
    has_official_website     = Column(Boolean)
    website_source           = Column(Text)   # 'wikidata' | 'google' | 'none'
    google_discovered_website = Column(Text)

    #  Signal detection 
    is_shopify      = Column(Boolean)
    is_woocommerce  = Column(Boolean)
    in_tranco_list  = Column(Boolean)
    tranco_rank     = Column(Integer)

    #  Per-step tracking flags 
    wikidata_enriched      = Column(Boolean, nullable=False, server_default="false", default=False)
    shopify_checked        = Column(Boolean, nullable=False, server_default="false", default=False)
    google_social_checked  = Column(Boolean, nullable=False, server_default="false", default=False)
    tranco_checked         = Column(Boolean, nullable=False, server_default="false", default=False)
    meta_ads_fetched       = Column(Boolean, nullable=False, server_default="false", default=False)
    youtube_checked        = Column(Boolean, nullable=False, server_default="false", default=False)
    instagram_checked      = Column(Boolean, nullable=False, server_default="false", default=False)
    tiktok_checked         = Column(Boolean, nullable=False, server_default="false", default=False)
    twitter_checked        = Column(Boolean, nullable=False, server_default="false", default=False)
    initial_brand_scored   = Column(Boolean, nullable=False, server_default="false", default=False)

    def __str__(self) -> str:
        return self.name or f"Brand #{self.id}"


class YoutubeSponsorship(Base):
    __tablename__ = "youtube_sponsorships"

    id               = Column(Integer, primary_key=True)
    brand_raw_id     = Column(Integer, ForeignKey("brands_raw.id"), nullable=False, index=True)
    video_id         = Column(Text, unique=True, nullable=False)
    video_title      = Column(Text)
    video_url        = Column(Text)
    channel_id       = Column(Text)
    channel_name     = Column(Text)
    subscriber_count = Column(Integer)
    published_at     = Column(Text)
    view_count       = Column(Integer)
    like_count       = Column(Integer)
    description_snippet = Column(Text)
    sponsorship_type    = Column(Text)
    confidence          = Column(Float)
    matched_keywords    = Column(JSONB)
    fetched_at          = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])


class Brand(Base):
    __tablename__ = "brands"

    id               = Column(Integer, primary_key=True)
    raw_id           = Column(Integer, ForeignKey("brands_raw.id"))
    name             = Column(Text, nullable=False)
    domain           = Column(Text, unique=True)
    industry         = Column(Text)
    employee_count   = Column(Integer)
    linkedin_url     = Column(Text)
    hq_country       = Column(Text)
    email_pattern    = Column(Text)
    enrichment_source= Column(Text)
    contacts_fetched = Column(Boolean, nullable=False, server_default="false", default=False)
    contacts_fetch_failed = Column(Boolean, nullable=False, server_default="false", default=False)
    enriched_at      = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Contact(Base):
    __tablename__ = "contacts"

    id                  = Column(Integer, primary_key=True)
    brand_id            = Column(Integer, ForeignKey("brands.id"))
    full_name           = Column(Text, nullable=False)
    title               = Column(Text)
    title_score         = Column(Integer)
    email               = Column(Text, unique=True)
    linkedin_url        = Column(Text, unique=True)
    apollo_email_status = Column(Text)
    email_verified      = Column(Boolean, nullable=False, server_default="false", default=False)
    email_status        = Column(Text)
    email_score         = Column(Integer)
    email_guessed       = Column(Boolean, nullable=False, server_default="false", default=False)
    verified_at         = Column(TIMESTAMP(timezone=True))
    outreach_sent       = Column(Boolean, nullable=False, server_default="false", default=False)
    created_at          = Column(TIMESTAMP(timezone=True), server_default=func.now())


class MetaAd(Base):
    __tablename__ = "meta_ads"

    id                  = Column(Integer, primary_key=True)
    brand_raw_id        = Column(Integer, ForeignKey("brands_raw.id"), nullable=False, index=True)
    ad_archive_id       = Column(Text, unique=True)
    page_name           = Column(Text)
    page_id             = Column(Text)
    ad_creative_bodies  = Column(JSONB)
    publisher_platforms = Column(JSONB)
    start_date          = Column(Text)
    end_date            = Column(Text)
    impressions         = Column(JSONB)
    spend               = Column(JSONB)
    currency            = Column(Text)
    fetched_at          = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])


class InstagramPost(Base):
    __tablename__ = "instagram_posts"

    id                     = Column(Integer, primary_key=True)
    brand_raw_id           = Column(Integer, ForeignKey("brands_raw.id"), nullable=False, index=True)
    instagram_handle       = Column(Text, nullable=False)

    # Post identity
    post_id                = Column(Text, unique=True, nullable=False)
    short_code             = Column(Text)
    post_url               = Column(Text)
    post_type              = Column(Text)   # Image, Video, Sidecar, etc.
    timestamp              = Column(Text)

    # Content
    caption                = Column(Text)
    hashtags               = Column(JSONB)
    mentions               = Column(JSONB)

    # Collaboration signals
    tagged_users           = Column(JSONB)
    coauthor_producers     = Column(JSONB)
    paid_partnership       = Column(Boolean)
    sponsors               = Column(JSONB)

    # Engagement
    likes_count            = Column(Integer)
    comments_count         = Column(Integer)
    video_view_count       = Column(Integer)
    video_play_count       = Column(Integer)

    # LLM verification
    llm_checked        = Column(Boolean, nullable=False, server_default="false", default=False)

    # User enrichment tracking
    is_users_scraped   = Column(Boolean, nullable=False, server_default="false", default=False)

    # Profile snapshot (at time of scrape)
    followers_count        = Column(Integer)
    follows_count          = Column(Integer)
    posts_count            = Column(Integer)
    is_business_account    = Column(Boolean)
    verified               = Column(Boolean)
    biography              = Column(Text)
    external_url           = Column(Text)
    business_category_name = Column(Text)

    fetched_at             = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])


class InstagramUser(Base):
    __tablename__ = "instagram_users"

    id                  = Column(Integer, primary_key=True)
    username            = Column(Text, unique=True, nullable=False)
    profile_url         = Column(Text)

    # Profile snapshot
    full_name           = Column(Text)
    bio                 = Column(Text)
    external_url        = Column(Text)
    followers_count     = Column(Integer)
    follows_count       = Column(Integer)
    posts_count         = Column(Integer)
    is_verified         = Column(Boolean)
    is_business_account = Column(Boolean)

    # Source type: "coauthor_producer", "tagged_user", "mention", "commenter"
    user_type           = Column(Text)

    # LLM demographics
    gender              = Column(Text)
    country             = Column(Text)
    language            = Column(Text)
    location            = Column(Text)
    age_group           = Column(Text)

    # Top 5 posts with their top 5 comments each
    top_posts           = Column(JSONB)

    raw_profile         = Column(JSONB)
    fetched_at          = Column(TIMESTAMP(timezone=True), server_default=func.now())

    def __str__(self) -> str:
        return self.username or f"Instagram User #{self.id}"


class BrandInstagramUser(Base):
    __tablename__ = "brand_instagram_users"

    brand_raw_id      = Column(Integer, ForeignKey("brands_raw.id"), primary_key=True)
    instagram_user_id = Column(Integer, ForeignKey("instagram_users.id"), primary_key=True)
    created_at        = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw      = relationship("BrandRaw",      lazy="selectin", foreign_keys=[brand_raw_id])
    instagram_user = relationship("InstagramUser", lazy="selectin", foreign_keys=[instagram_user_id])


class InstagramCreatorCommenter(Base):
    __tablename__ = "instagram_creator_commenters"

    creator_user_id   = Column(Integer, ForeignKey("instagram_users.id"), primary_key=True)
    commenter_user_id = Column(Integer, ForeignKey("instagram_users.id"), primary_key=True)
    brand_raw_id      = Column(Integer, ForeignKey("brands_raw.id"), primary_key=True)
    source_post_url   = Column(Text, primary_key=True, default="")
    comment_text      = Column(Text)
    comment_likes     = Column(Integer)
    created_at        = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])
    creator_user = relationship("InstagramUser", lazy="selectin", foreign_keys=[creator_user_id])
    commenter_user = relationship("InstagramUser", lazy="selectin", foreign_keys=[commenter_user_id])


class TiktokPost(Base):
    __tablename__ = "tiktok_posts"

    id             = Column(Integer, primary_key=True)
    brand_raw_id   = Column(Integer, ForeignKey("brands_raw.id"), nullable=False, index=True)
    tiktok_handle  = Column(Text, nullable=False)

    # Post identity
    video_id       = Column(Text, unique=True, nullable=False)
    video_url      = Column(Text)
    create_time    = Column(Text)

    # Engagement
    play_count     = Column(Integer)
    like_count     = Column(Integer)
    comment_count  = Column(Integer)
    share_count    = Column(Integer)
    collect_count  = Column(Integer)

    # Sponsorship signals
    is_sponsored   = Column(Boolean)
    is_ad          = Column(Boolean)
    mentions       = Column(JSONB)
    hashtags       = Column(JSONB)

    fetched_at     = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])


class TwitterPost(Base):
    __tablename__ = "twitter_posts"

    id              = Column(Integer, primary_key=True)
    brand_raw_id    = Column(Integer, ForeignKey("brands_raw.id"), nullable=False, index=True)
    twitter_handle  = Column(Text, nullable=False)

    # Tweet identity
    tweet_id        = Column(Text, unique=True, nullable=False)
    permalink       = Column(Text)
    created_at      = Column(Text)

    # Content
    text            = Column(Text)
    hashtags        = Column(JSONB)
    mentions        = Column(JSONB)

    # Sponsorship signals
    is_sponsored    = Column(Boolean)
    sponsor_signals = Column(JSONB)

    # Engagement
    likes           = Column(Integer)
    retweets        = Column(Integer)
    quotes          = Column(Integer)
    comments        = Column(Integer)
    has_media       = Column(Boolean)

    # Author snapshot
    username        = Column(Text)
    fullname        = Column(Text)
    verified        = Column(Boolean)

    fetched_at      = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])


class InitialBrandScore(Base):
    __tablename__ = "initial_brand_score"

    id                      = Column(Integer, primary_key=True)
    brand_raw_id            = Column(Integer, ForeignKey("brands_raw.id"), unique=True, nullable=False, index=True)

    influencer_score        = Column(Integer, nullable=False, default=0)
    ad_spend_score          = Column(Integer, nullable=False, default=0)
    legitimacy_score        = Column(Integer, nullable=False, default=0)
    reachability_score      = Column(Integer, nullable=False, default=0)

    total_score             = Column(Integer, nullable=False, default=0)
    score_band              = Column(Text, nullable=False, default="COLD")

    enrichment_completeness = Column(Integer, nullable=False, default=0)

    score_details           = Column(JSONB)
    scored_at               = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])

    def __str__(self) -> str:
        return f"Score#{self.id} brand={self.brand_raw_id} {self.score_band}({self.total_score})"


class User(Base):
    __tablename__ = "users"

    id         = Column(Integer, primary_key=True)
    email      = Column(Text, unique=True, nullable=False)
    name       = Column(Text)
    google_id  = Column(Text, unique=True)
    avatar_url = Column(Text)
    is_active  = Column(Boolean, nullable=False, server_default="true", default=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    def __str__(self) -> str:
        return self.email or f"User #{self.id}"


class Prompt(Base):
    __tablename__ = "prompts"

    id         = Column(Integer, primary_key=True)
    name       = Column(Text, unique=True, nullable=False)
    content    = Column(Text, nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _brand_raw_fields(b: dict) -> dict:
    """Extract all optional BrandRaw fields from a seed row dict."""
    website = b.get("website") or None
    return {
        "wikidata_id":           b.get("wikidata_id") or None,
        "entity_type":           b.get("entity_type") or None,
        "description":           b.get("description") or None,
        "wikipedia_url":         b.get("wikipedia_url") or None,
        "source_confidence":     b.get("source_confidence"),
        "website":               website,
        "domain":                b.get("domain") or None,
        "country":               b.get("country"),
        "headquarters":          b.get("headquarters"),
        "location":              b.get("location"),
        "operating_area":        b.get("operating_area"),
        "has_official_website":  True if website else None,
        "website_source":        "wikidata" if website else None,
    }


def _row_values(b: dict) -> dict:
    return {
        "name":            b["name"],
        "name_normalized": b["normalized"],
        "niche":           b["niche"],
        "source":          b["source"],
        "source_url":      b.get("source_url"),
        **_brand_raw_fields(b),
    }


def insert_brand(db: Session, brand: dict) -> bool:
    stmt = (
        insert(BrandRaw)
        .values(**_row_values(brand))
        # on_conflict_do_nothing() with no args handles all unique constraints
        .on_conflict_do_nothing()
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount == 1


def _dedup_by(rows: list[dict], key_fn) -> list[dict]:
    """Keep first occurrence of each key within the list."""
    seen: set = set()
    out: list[dict] = []
    for r in rows:
        k = key_fn(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


_CHUNK = 500   # max rows per INSERT to avoid oversized statements


def insert_brands_batch(db: Session, brands: list[dict]) -> int:
    if not brands:
        return 0

    # Split: records with a Wikidata QID conflict on wikidata_id (primary identity);
    # records without one fall back to name_normalized dedup.
    #
    # ON CONFLICT DO NOTHING only resolves conflicts against existing table rows.
    # Two rows within the same INSERT that share a unique key will still crash,
    # so we deduplicate each list before building the VALUES clause.
    with_qid = _dedup_by(
        [b for b in brands if b.get("wikidata_id")],
        lambda b: b["wikidata_id"],
    )
    without_qid = _dedup_by(
        [b for b in brands if not b.get("wikidata_id")],
        lambda b: b["normalized"],
    )
    total = 0

    for i in range(0, max(len(with_qid), 1), _CHUNK):
        chunk = with_qid[i : i + _CHUNK]
        if not chunk:
            break
        # Use no index_elements so DO NOTHING fires on ANY unique violation
        # (wikidata_id OR name_normalized) — prevents crash when the same
        # brand was previously seeded without a wikidata_id.
        stmt = (
            insert(BrandRaw)
            .values([_row_values(b) for b in chunk])
            .on_conflict_do_nothing()
        )
        total += db.execute(stmt).rowcount

    for i in range(0, max(len(without_qid), 1), _CHUNK):
        chunk = without_qid[i : i + _CHUNK]
        if not chunk:
            break
        stmt = (
            insert(BrandRaw)
            .values([_row_values(b) for b in chunk])
            .on_conflict_do_nothing(index_elements=["name_normalized"])
        )
        total += db.execute(stmt).rowcount

    db.commit()
    return total
