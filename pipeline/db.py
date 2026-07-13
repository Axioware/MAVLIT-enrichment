from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, Text, TIMESTAMP, UniqueConstraint, create_engine
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


class BrandNiche(Base):
    """
    Many-to-many mirror of brands_raw.niche — a relational join table so a
    brand can eventually carry more than one niche without changing
    brands_raw's schema. Kept in sync automatically: insert_brand() and
    insert_brands_batch() write a matching row here every time a brand is
    newly inserted into brands_raw. brands_raw.niche itself is untouched and
    still the single source of truth for existing code that reads it.
    """
    __tablename__ = "brands_niches"
    __table_args__ = (
        UniqueConstraint("brand_raw_id", "niche", name="uq_brand_niche"),
    )

    id           = Column(Integer, primary_key=True)
    brand_raw_id = Column(Integer, ForeignKey("brands_raw.id"), nullable=False, index=True)
    niche        = Column(Text, nullable=False, index=True)

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])

    def __str__(self) -> str:
        return f"{self.niche} — brand={self.brand_raw_id}"


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
    tier_fit         = Column(Text)   # nano / micro / macro / mega, bucketed from subscriber_count
    published_at     = Column(Text)
    view_count       = Column(Integer)
    like_count       = Column(Integer)
    description_snippet = Column(Text)
    sponsorship_type    = Column(Text)
    confidence          = Column(Float)
    matched_keywords    = Column(JSONB)
    comments            = Column(JSONB)   # up to 200 top-level comments: [{"author":.., "text":.., "likes":..}, ...]
    male_pct            = Column(Float)   # gender split of commenters, inferred via Mistral from display names — excludes "unknown"
    female_pct          = Column(Float)
    fetched_at          = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])


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
    tier_fit            = Column(Text)   # nano / micro / macro / mega, bucketed from followers_count
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
    captions            = Column(JSONB)   # flat list of caption strings from top_posts — creators only, NULL for commenters

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


class BrandProfile(Base):
    __tablename__ = "brand_match_profile"

    brand_raw_id = Column(Integer, ForeignKey("brands_raw.id"), primary_key=True)

    #  Sponsorship activity
    sponsorship_activity_score = Column(Float)   # 0-100 composite, used in scoring — NULL until computed
    meta_ads_active            = Column(Boolean)              # True if any ad is currently running
    meta_ads_recency_days      = Column(Integer)               # days since last ad started; 0 if currently running
    meta_ads_no_end_date       = Column(Boolean)               # True if any ad has no end_date (still live)
    meta_ads_count             = Column(Integer)
    youtube_last_sponsorship   = Column(TIMESTAMP(timezone=True))   # publish date of the most recent sponsored video
    tiktok_sponsored_count     = Column(Integer)
    twitter_sponsored_count    = Column(Integer)

    #  Creator tier fit
    avg_yt_creator_subscribers    = Column(Integer)
    avg_ig_collaborator_followers = Column(Integer)
    typical_creator_tier          = Column(Text)   # nano / micro / macro / mega
    youtube_highest = Column(Integer)   # max subscriber count among YouTube collaborators
    youtube_lowest  = Column(Integer)   # min subscriber count among YouTube collaborators
    insta_highest   = Column(Integer)   # max follower count among Instagram content creators
    insta_lowest    = Column(Integer)   # min follower count among Instagram content creators

    #  Audience demographics
    audience_gender_male_pct   = Column(Float)
    audience_gender_female_pct = Column(Float)
    audience_top_countries     = Column(JSONB)   # [{"country": "US", "pct": 0.45}, ...]
    audience_age_groups        = Column(JSONB)   # {"12_16": 0.1, "17_22": 0.3, "23_28": 0.4, ...} — bucket names from instagram_users.py's LLM classifier

    #  Platform presence
    has_instagram = Column(Boolean)
    has_youtube   = Column(Boolean)
    has_facebook  = Column(Boolean)
    has_tiktok    = Column(Boolean)   # reserved — not scored in V1
    has_twitter   = Column(Boolean)   # reserved — not scored in V1

    #  Contact / outreach routing signal (NOT used in scoring)
    has_marketing_contact    = Column(Boolean)   # True if Apollo returned a marketing/partnerships title
    contact_mode             = Column(Text)      # 'in_house' | 'outsourced_likely' | 'none'
    best_contact_title_score = Column(Integer)

    #  Embedding — mistral-embed (Mistral API), 1024 dims
    embedding      = Column(Vector(1024))
    embedding_text = Column(Text)

    computed_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])

    def __str__(self) -> str:
        return f"BrandProfile brand={self.brand_raw_id} tier={self.typical_creator_tier}"


class BrandContact(Base):  
    __tablename__ = "brand_contacts"
    __table_args__ = (
        UniqueConstraint("brand_raw_id", "apollo_person_id", name="uq_brand_contact_person"),
    )

    id           = Column(Integer, primary_key=True)
    brand_raw_id = Column(Integer, ForeignKey("brands_raw.id"), nullable=False, index=True)

    rank = Column(Integer)   # 1-50, Mistral's priority order among this brand's contacts (1 = best)
    is_enriched = Column(Boolean, nullable=False, server_default="false", default=False)   # True only for the paid-enriched top 5

    full_name      = Column(Text)
    title          = Column(Text)
    departments    = Column(Text)   # Apollo taxonomy, e.g. "master_marketing; master_sales"
    subdepartments = Column(Text)   # e.g. "marketing; partnerships"
    functions      = Column(Text)   # e.g. "marketing; media_and_commmunication"
    seniority      = Column(Text)   # e.g. "manager", "director", "vp", "c_suite"
    email          = Column(Text)
    email_status   = Column(Text)   # Apollo's own status, e.g. "verified"
    phone          = Column(Text)
    linkedin_url   = Column(Text)
    city           = Column(Text)
    state          = Column(Text)
    country        = Column(Text)

    llm_reason       = Column(Text)   # Mistral's one-line justification for picking this person
    apollo_person_id = Column(Text)

    fetched_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_raw = relationship("BrandRaw", lazy="selectin", foreign_keys=[brand_raw_id])

    def __str__(self) -> str:
        return f"{self.full_name or 'No contact found'} ({self.title or '-'}) — brand={self.brand_raw_id}"


class CreatorProfile(Base):
    __tablename__ = "creator_profiles"

    id = Column(Integer, primary_key=True)

    #  Account / auth identity (formerly the separate `users` table)
    email      = Column(Text, unique=True, nullable=False)
    google_id  = Column(Text, unique=True)
    avatar_url = Column(Text)
    is_active  = Column(Boolean, nullable=False, server_default="true", default=True)

    #  Identity
    full_name        = Column(Text)
    creator_handle   = Column(Text)
    location_city    = Column(Text)
    location_country = Column(Text)
    bio_tagline      = Column(Text)

    #  Content niche
    content_niche = Column(Text)
    # 'music', 'podcast_audio', 'photography', 'gaming', 'education',
    # 'fitness', 'food_cooking', 'finance', 'substack_newsletter', 'tech'
    sub_niches           = Column(JSONB)   # e.g. ["vegan cooking", "meal prep"] — creator-provided
    content_description  = Column(Text)    # free-text creator-written description, source text for LLM tag extraction
    excluded_categories   = Column(JSONB)   # brand niches/categories this creator refuses to work with — Stage 3 hard filter

    #  Platform links
    instagram_handle = Column(Text)
    tiktok_handle     = Column(Text)
    youtube_channel   = Column(Text)
    facebook_page     = Column(Text)
    substack_url      = Column(Text)

    #  Platform stats
    instagram_stats = Column(JSONB)
    tiktok_stats     = Column(JSONB)
    youtube_stats    = Column(JSONB)
    facebook_stats   = Column(JSONB)
    substack_subscribers = Column(Integer)

    primary_platform = Column(Text)
    follower_count   = Column(Integer)   # creator-provided; drives creator_tier bucketing

    #  Audience demographics
    # Same shape as BrandMatchProfile's audience fields, so the two sides
    # compare directly in score_audience_demographics().
    audience_gender_male_pct    = Column(Float)      # 0.0 – 1.0
    audience_gender_female_pct  = Column(Float)      # 0.0 – 1.0
    audience_age_bracket        = Column(Text)       # e.g. "17_22", "23_28", "29_35" — legacy single-bracket field, kept for compatibility
    audience_age_min            = Column(Integer)    # e.g. 18 — used for range-based age overlap in Stage 3
    audience_age_max            = Column(Integer)    # e.g. 34
    audience_top_countries      = Column(JSONB)      # [{"country": "US", "pct": 0.45}, ...]

    #  Derived / computed fields
    creator_tier  = Column(Text)
    content_tags  = Column(JSONB)   # LLM-extracted content tags + audience-value keywords, merged into one list
    embedding     = Column(Vector(1024))   # mistral-embed (Mistral API), must match BrandProfile.embedding
    embedding_text = Column(Text)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), onupdate=func.now())

    def __str__(self) -> str:
        return self.creator_handle or self.email or f"Creator #{self.id}"


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


def _insert_brand_niches(db: Session, id_niche_pairs: list[tuple[int, str]]) -> None:
    """Mirror each newly-inserted brand's niche into brands_niches."""
    rows = [{"brand_raw_id": bid, "niche": niche} for bid, niche in id_niche_pairs if niche]
    if not rows:
        return
    stmt = (
        insert(BrandNiche)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["brand_raw_id", "niche"])
    )
    db.execute(stmt)


def insert_brand(db: Session, brand: dict) -> bool:
    stmt = (
        insert(BrandRaw)
        .values(**_row_values(brand))
        # on_conflict_do_nothing() with no args handles all unique constraints
        .on_conflict_do_nothing()
        .returning(BrandRaw.id, BrandRaw.niche)
    )
    row = db.execute(stmt).first()
    if row:
        _insert_brand_niches(db, [(row.id, row.niche)])
    db.commit()
    return row is not None


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
    inserted_niches: list[tuple[int, str]] = []

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
            .returning(BrandRaw.id, BrandRaw.niche)
        )
        rows = db.execute(stmt).all()
        inserted_niches.extend((r.id, r.niche) for r in rows)
        total += len(rows)

    for i in range(0, max(len(without_qid), 1), _CHUNK):
        chunk = without_qid[i : i + _CHUNK]
        if not chunk:
            break
        stmt = (
            insert(BrandRaw)
            .values([_row_values(b) for b in chunk])
            .on_conflict_do_nothing(index_elements=["name_normalized"])
            .returning(BrandRaw.id, BrandRaw.niche)
        )
        rows = db.execute(stmt).all()
        inserted_niches.extend((r.id, r.niche) for r in rows)
        total += len(rows)

    _insert_brand_niches(db, inserted_niches)
    db.commit()
    return total
