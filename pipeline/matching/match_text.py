"""
pipeline/matching/match_text.py

"Why it's a match" — Tier 1 (template, always, instant) text generation
per the matching design doc. Plain string formatting sourced from real
signal fields, not an LLM call.

Returns one recent-sponsorship reason selected from the three sponsorship
conditions, with the highest-scoring applicable condition taking precedence.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from pipeline.db import (
    BrandContact,
    BrandNiche,
    BrandProfile,
    BrandRaw,
    BrandInstagramUser,
    CreatorNiche,
    CreatorProfile,
    InstagramPost,
    InstagramUser,
    ContentCreatorRE,
    TestCreatorBrandPartnershipPost,
    YoutubeSponsorship,
)

_MAX_REASONS = 5


def _similarity_match_label(similarity: float) -> str:
    if similarity < 0.86:
        return "80-90%"
    return "90-100%"


def _recent_sponsorship_candidates(
    creator: CreatorProfile,
    brand: BrandRaw,
    db=None,
) -> list[tuple[int, str]]:
    """Build the scored candidates for the nine sponsorship conditions."""
    if db is None or creator.embedding is None or brand is None:
        return []

    now = datetime.now(timezone.utc)
    recent_paid_rows = (
        db.query(InstagramPost)
        .filter(
            InstagramPost.brand_raw_id == brand.id,
            InstagramPost.paid_partnership.is_(True),
        )
        .all()
    )

    sponsorships: list[tuple[InstagramPost, int, dict[str, float]]] = []

    for post in recent_paid_rows:
        age_days = _post_age_days(post.timestamp, now)
        if age_days is None or age_days > 180:
            continue

        usernames = _post_usernames(post)
        similar_usernames: dict[str, float] = {}
        for username in usernames:
            match_row = (
                db.query(
                    CreatorNiche.username,
                    (1.0 - CreatorNiche.embedding.cosine_distance(creator.embedding)).label("similarity"),
                )
                .filter(
                    func.lower(CreatorNiche.username) == username,
                    CreatorNiche.embedding.isnot(None),
                )
                .first()
            )
            if match_row is None:
                continue
            similarity = float(match_row.similarity)
            if similarity >= 0.70:
                similar_usernames[username] = max(similar_usernames.get(username, 0.0), similarity)
        sponsorships.append((post, age_days, similar_usernames))

    if not sponsorships:
        return []

    latest_age = min(age_days for _, age_days, _ in sponsorships)
    latest_similar_usernames = {
        username
        for _, age_days, usernames in sponsorships
        if age_days == latest_age
        for username in usernames
    }
    best_similarity = max(
        (similarity for _, age_days, usernames in sponsorships if age_days == latest_age for similarity in usernames.values()),
        default=0.70,
    )
    similarity_label = _similarity_match_label(best_similarity)
    if latest_age <= 30:
        window = "last month"
        scores = (100, 95, 82)
    elif latest_age <= 90:
        window = "last 3 months"
        scores = (92, 90, 75)
    else:
        window = "last 6 months"
        scores = (85, 81, 71)

    candidates: list[tuple[int, str]] = []
    if len(latest_similar_usernames) >= 2:
        candidates.append((scores[0], f"{brand.name} ran a paid partnership with multiple creators whose content {similarity_label} matches yours within the {window}."))
    elif len(latest_similar_usernames) == 1:
        candidates.append((scores[1], f"{brand.name} ran a paid partnership within the {window} with the creator whose content {similarity_label} matches yours."))
    else:
        candidates.append((scores[2], f"{brand.name} ran a paid partnership within the {window}."))

    return candidates


def _recent_sponsorship_priority_reason(
    creator: CreatorProfile,
    brand: BrandRaw,
    db=None,
) -> str | None:
    candidates = _recent_sponsorship_candidates(creator, brand, db)
    return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


def _post_age_days(timestamp: str | None, now: datetime) -> int | None:
    if not timestamp:
        return None
    try:
        normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        post_time = datetime.fromisoformat(normalized)
        if post_time.tzinfo is None:
            post_time = post_time.replace(tzinfo=timezone.utc)
        return max(0, (now - post_time).days)
    except (TypeError, ValueError):
        return None


def _post_usernames(post: InstagramPost) -> set[str]:
    usernames: set[str] = set()
    for field in ("sponsors", "tagged_users", "mentions", "coauthor_producers"):
        value = getattr(post, field, None)
        values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
        usernames.update(str(item).strip().lower() for item in values if str(item).strip())
    return usernames


def _creator_niche_names(creator: CreatorProfile) -> set[str]:
    values = (
        creator.instagram_primary_niche,
        creator.youtube_primary_niche,
        creator.content_niche,
    )
    return {
        niche.strip().lower()
        for value in values
        for niche in (value or "").split(",")
        if niche.strip()
    }


def _creator_sub_niche_names(creator: CreatorProfile) -> set[str]:
    return {
        str(niche).strip().lower()
        for values in (creator.instagram_sub_niches, creator.youtube_sub_niches)
        for niche in (values or [])
        if str(niche).strip()
    }


def _brand_niche_bridge_reason(creator: CreatorProfile, brand: BrandRaw, db) -> str | None:
    creator_niches = _creator_niche_names(creator)
    brand_niche = (brand.niche or "").strip().lower()
    if not creator_niches or not brand_niche or brand_niche in creator_niches:
        return None

    collaborator_niche = (
        db.query(InstagramUser.niche)
        .join(BrandInstagramUser, BrandInstagramUser.instagram_user_id == InstagramUser.id)
        .filter(
            BrandInstagramUser.brand_raw_id == brand.id,
            InstagramUser.user_type != "commenter",
            func.lower(InstagramUser.niche).in_(creator_niches),
        )
        .first()
    )
    matched_niche = collaborator_niche[0] if collaborator_niche else None

    if matched_niche is None:
        reverse_niche = (
            db.query(ContentCreatorRE.niche)
            .join(
                TestCreatorBrandPartnershipPost,
                TestCreatorBrandPartnershipPost.content_creator_re_id == ContentCreatorRE.id,
            )
            .filter(
                TestCreatorBrandPartnershipPost.brand_raw_id == brand.id,
                func.lower(ContentCreatorRE.niche).in_(creator_niches),
            )
            .first()
        )
        matched_niche = reverse_niche[0] if reverse_niche else None

    if matched_niche is None:
        return None
    return f"Although {brand.name} is a {brand.niche} brand, it also sponsors creators in the {matched_niche} niche, the same niche as you."


def _additional_priority_reasons(creator: CreatorProfile, brand: BrandRaw, db) -> list[tuple[int, str]]:
    reasons: list[tuple[int, str]] = []
    avg_followers = None
    profile = db.query(BrandProfile).filter(BrandProfile.brand_raw_id == brand.id).first()
    if profile:
        avg_followers = profile.avg_ig_collaborator_followers or profile.avg_yt_creator_subscribers

    if avg_followers and creator.follower_count:
        within_size = abs(creator.follower_count - avg_followers) <= avg_followers * 0.25
        similar_partners = _similar_partner_usernames(creator, brand, db)
        similar_partner = bool(similar_partners) or _same_niche_partner(creator, brand, db)
        if within_size and similar_partner:
            if similar_partners:
                similarity_label = _similarity_match_label(max(similar_partners.values()))
                reasons.append((80, f"{brand.name} has partnered with creators average ({avg_followers:,} followers), creators close to your size with content {similarity_label} similar to yours."))
            else:
                reasons.append((80, f"{brand.name} has partnered with creators average ({avg_followers:,} followers), creators close to your size with content type same as yours."))
        elif within_size:
            reasons.append((50, f"{brand.name} has partnered with creators average ({avg_followers:,} followers), creators close to your size."))

    contact = db.query(BrandContact).filter(
        BrandContact.brand_raw_id == brand.id,
        BrandContact.email_status.ilike("verified"),
        BrandContact.sponsorship_contact_confidence >= 70,
        BrandContact.still_at_brand.is_(True),
    ).first()
    if contact:
        reasons.append((75, f"MAVLIT has a verified contact for {brand.name}'s partnerships team."))

    similar = _similar_partner_usernames(creator, brand, db)
    if len(similar) >= 3:
        similarity_label = _similarity_match_label(max(similar.values()))
        reasons.append((78, f"{len(similar)} creators with content {similarity_label} similar to yours have partnered with {brand.name}."))
    elif _same_niche_high_confidence_partner(creator, brand, db):
        reasons.append((70, f"{brand.name} has partnered with a creator in the {creator.content_niche} niche, the same as yours."))
    elif similar:
        similarity_label = _similarity_match_label(max(similar.values()))
        reasons.append((65, f"{brand.name} has partnered with a creator whose content {similarity_label} matches yours."))

    youtube = db.query(YoutubeSponsorship).filter(
        YoutubeSponsorship.brand_raw_id == brand.id,
        YoutubeSponsorship.confidence >= 0.7,
    ).first()
    if youtube:
        reasons.append((45, f"{brand.name} also sponsors YouTube creators."))

    platform = (creator.primary_platform or "").strip().lower()
    if platform == "youtube":
        follower_min = profile.youtube_lowest if profile else None
        follower_max = profile.youtube_highest if profile else None
        creator_followers = creator.youtube_followers
    else:
        follower_min = profile.insta_lowest if profile else None
        follower_max = profile.insta_highest if profile else None
        creator_followers = creator.follower_count
    if (
        follower_min is not None
        and follower_max is not None
        and creator_followers is not None
        and follower_min <= creator_followers <= follower_max
    ):
        reasons.append((69, f"{brand.name} works with creators that has followers range same as yours."))

    if brand.brand_tier == "lower-range":
        reasons.append((40, f"{brand.name} is a smaller brand, so creators can typically reach decision-makers directly."))
    elif brand.brand_tier == "midlower-range":
        reasons.append((35, f"{brand.name} is a growing brand where creator outreach is still realistic."))

    brand_tags = _brand_tags(brand, db)
    creator_tags = _creator_sub_niche_names(creator)
    for brand_tag in brand_tags:
        for creator_tag in creator_tags:
            if _shorter_tag_words_match(brand_tag, creator_tag):
                reasons.append((33, f"{brand.name} focuses on {brand_tag}, which overlaps with your content ({creator_tag})."))
                break
        if reasons and reasons[-1][0] == 33:
            break
    if _creator_niche_names(creator) & {str(brand.niche or "").lower()}:
        reasons.append((30, f"{brand.name} is a {brand.niche} brand, the same niche as you."))

    return reasons



def _similar_partner_usernames(creator: CreatorProfile, brand: BrandRaw, db) -> dict[str, float]:
    if creator.embedding is None:
        return {}
    usernames: dict[str, float] = {}
    posts = db.query(InstagramPost).filter(
        InstagramPost.brand_raw_id == brand.id,
        InstagramPost.paid_partnership.is_(True),
    ).all()
    for post in posts:
        for username in _post_usernames(post):
            match_row = (
                db.query(
                    CreatorNiche.username,
                    (1.0 - CreatorNiche.embedding.cosine_distance(creator.embedding)).label("similarity"),
                )
                .filter(
                    func.lower(CreatorNiche.username) == username,
                    CreatorNiche.embedding.isnot(None),
                )
                .first()
            )
            if match_row is not None and float(match_row.similarity) >= 0.70:
                similarity = float(match_row.similarity)
                usernames[username] = max(usernames.get(username, 0.0), similarity)
    return usernames


def _same_niche_high_confidence_partner(creator: CreatorProfile, brand: BrandRaw, db) -> bool:
    niches = _creator_niche_names(creator)
    if not niches:
        return False
    rows = (
        db.query(InstagramUser.username)
        .join(BrandInstagramUser, BrandInstagramUser.instagram_user_id == InstagramUser.id)
        .join(InstagramPost, InstagramPost.post_id == InstagramUser.post_id)
        .filter(
            BrandInstagramUser.brand_raw_id == brand.id,
            InstagramPost.sponsorship_confidence >= 90,
            InstagramUser.user_type != "commenter",
            func.lower(InstagramUser.niche).in_(niches),
        )
        .distinct()
        .all()
    )
    return len(rows) >= 5


def _same_niche_partner(creator: CreatorProfile, brand: BrandRaw, db) -> bool:
    niches = _creator_niche_names(creator)
    if not niches:
        return False
    return db.query(InstagramUser.id).join(
        BrandInstagramUser, BrandInstagramUser.instagram_user_id == InstagramUser.id
    ).filter(
        BrandInstagramUser.brand_raw_id == brand.id,
        InstagramUser.user_type != "commenter",
        func.lower(InstagramUser.niche).in_(niches),
    ).first() is not None


def _brand_tags(brand: BrandRaw, db) -> set[str]:
    rows = db.query(BrandNiche.tags).filter(
        BrandNiche.brand_raw_id == brand.id,
        BrandNiche.tags.isnot(None),
    ).all()
    return {
        str(tag).strip().lower()
        for (tags,) in rows
        if isinstance(tags, list)
        for tag in tags
        if isinstance(tag, str) and tag.strip()
    }


def _shorter_tag_words_match(first: str, second: str) -> bool:
    first_words = set(first.lower().split())
    second_words = set(second.lower().split())
    shorter, longer = sorted((first_words, second_words), key=len)
    return bool(shorter) and shorter.issubset(longer)


def _priority_1_recent_sponsorship_similarity(
    creator: CreatorProfile,
    brand: BrandRaw,
    profile: BrandProfile | None,
    dims: dict,
    db=None,
) -> str | None:
    if db is None or creator.embedding is None or brand is None:
        return None

    return _recent_sponsorship_priority_reason(creator, brand, db)


_PRIORITY_TIERS = [
    _priority_1_recent_sponsorship_similarity,
]


def generate_match_reasons(
    creator: CreatorProfile,
    brand: BrandRaw,
    profile: BrandProfile | None,
    dimensions: dict,
    max_reasons: int = _MAX_REASONS,
    db=None,
) -> list[str]:
    """
    Returns one winner for each exclusive group and each applicable separate
    signal, ordered by descending requested score.
    `dimensions` is the score_match()["dimensions"] dict for this pair.
    """
    reasons: list[tuple[int, str]] = []
    bridge_reason = _brand_niche_bridge_reason(creator, brand, db)
    if bridge_reason:
        reasons.append((10_000, bridge_reason))
    recent_reason = _priority_1_recent_sponsorship_similarity(creator, brand, profile, dimensions, db)
    if recent_reason:
        recent_candidates = _recent_sponsorship_candidates(creator, brand, db)
        recent_score = max(score for score, text in recent_candidates if text == recent_reason)
        reasons.append((recent_score, recent_reason))
    reasons.extend(_additional_priority_reasons(creator, brand, db))
    reasons.sort(key=lambda reason: reason[0], reverse=True)
    return [text for _, text in reasons[:max_reasons]]
