"""
pipeline/matching/scoring.py

Stage 3 Step C — match score built from the "why it's a match" taglines
(pipeline/matching/match_text.py, documented in taglines.md).

Every brand starts at 55%. Each group can add up to its GROUP_MAX_POINTS;
earned points are scaled onto the remaining 45% (a brand that maxes every
group scores 100%). Only one option per tagline group can apply; it
earns its share of the group's points in proportion to its tagline
priority, relative to the group's top priority (_GROUP_TOP_PRIORITY):

    points = GROUP_MAX_POINTS[group] * priority / _GROUP_TOP_PRIORITY[group]

e.g. "ran a paid partnership within the last 3 months" (2c, priority 75 of
100) earns 8 * 75/100 = 6 of recent_partnership's 8 points.

Creator size is graded by distance too: match_text.py multiplies its
priority by closeness to the brand's average collaborator followers (1.0
at the average, sliding to 0 at ±25%), so e.g. option 3a at 12.5% from the
average earns 5 * 80*0.5/80 = 2.5 of creator_size's 5 points.

Taglines whose group isn't in GROUP_MAX_POINTS still show as match text
but earn no points: Niche bridge (priority 10000, only orders the text),
and 12. Target audience gender / 13. Target audience age / 14. Product
audience gender.

Two extra signals that have no tagline of their own:
    niche_creators  the brand's niche is one of the creator's niches AND
                    1 of the brand's creators (Instagram collaborator or RE
                    creator) is in one of them too -> 2/3 of its points;
                    2+ such creators -> all of its points
    niche_tier      first of these that applies (see _niche_tier); also
                    the primary sort key on the matches page (matcher.py
                    sorts by niche_tier, then total_score):
                      tier 3  brand niche = creator's niche AND the brand
                              has a confidence >= 90 creator in that same
                              niche                            -> 10 pts
                      tier 2  brand niche = creator's niche     ->  5 pts
                      tier 1  brand niche != creator's niche, but it has
                              confidence >= 90 creators in the creator's
                              niche                -> 2 pts each (max 10)
                      tier 0  none of the above                 ->  0

    total = 0.55 + 0.45 * sum(points) / sum(GROUP_MAX_POINTS)
    (returned as 0.55-1.0)
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from pipeline.db import (
    BrandInstagramUser,
    BrandRaw,
    ContentCreatorRE,
    CreatorProfile,
    InstagramPost,
    InstagramUser,
    TestCreatorBrandPartnershipPost,
)
from pipeline.matching.match_text import _creator_niche_names

SCORE_BASE  = 0.55   # score with no applicable signals
SCORE_RANGE = 0.45   # SCORE_BASE + SCORE_RANGE = 1.0 when every group is maxed

# Most points each group can add. Scaled onto SCORE_RANGE by their sum.
GROUP_MAX_POINTS: dict[str, float] = {
    "niche_tier":         10,   # brand niche vs your niche + its confident creators (see _niche_tier)
    "recent_partnership": 8,    # 2. paid partnership in the last 6 months
    "niche_creators":     7,    # brand niche + its creators' niche match yours
    "similar_partners":   6,    # 5. partnered with similar / same-niche creators
    "same_niche":         5,    # 10. brand niche = your niche
    "creator_size":       5,    # 3. partners close to your follower size
    "follower_range":     4,    # 6. you're inside its collaborator follower range
    "verified_contact":   4,    # 4. MAVLIT has a verified partnerships contact
    "tag_overlap":        2,    # 9. brand tag overlaps your sub-niche
    "youtube_sponsor":    2,    # 7. also sponsors YouTube creators
    "brand_tier":         1,    # 8. smaller / growing brand
    "latest_product":     1,    # 11. has a latest product to pitch around
}
_MAX_POINTS = sum(GROUP_MAX_POINTS.values())

# Top tagline priority in each group (taglines.md / match_text.py) — an
# option earns GROUP_MAX_POINTS * its priority / this. Keep in sync with
# the priorities in match_text.py.
_GROUP_TOP_PRIORITY: dict[str, float] = {
    "recent_partnership": 100,   # a/b/c by window: 100/95/92/90/85/82/81/75/71
    "similar_partners":   78,    # a = 78, b = 70, c = 65
    "same_niche":         30,
    "creator_size":       80,    # a/b = 80, c = 50 — each × closeness to the brand's
                                 # average followers (1.0 at the average, 0 at ±25%)
    "follower_range":     69,
    "verified_contact":   75,
    "tag_overlap":        33,
    "youtube_sponsor":    45,
    "brand_tier":         40,    # a = 40, b = 35
    "latest_product":     29,
}

# Matching creators -> share of niche_creators' points (2 = "2 or more").
_NICHE_CREATOR_SHARE = {1: 2 / 3, 2: 1.0}

# niche_tier points: tier 3 / tier 2 fixed, tier 1 per matching creator.
_NICHE_TIER_POINTS = {3: 10.0, 2: 5.0}
_NICHE_TIER_POINTS_PER_CREATOR = 2.0
_CONFIDENT_SPONSORSHIP = 90


def _niche_creator_points(db: Session, creator: CreatorProfile, brand: BrandRaw) -> float:
    """
    2/3 of niche_creators' points if the brand's niche AND 1 of its
    creators' niches are among the creator's niches, all of them if 2+ of
    its creators are. Creators are the
    brand's non-commenter Instagram collaborators (instagram_users.niche)
    plus its reverse-engineered creators (content_creator_re.niche).
    """
    niches = _creator_niche_names(creator)
    brand_niche = (brand.niche or "").strip().lower()
    if not niches or brand_niche not in niches:
        return 0.0

    collaborators = {
        username.strip().lower()
        for (username,) in db.query(InstagramUser.username)
        .join(BrandInstagramUser, BrandInstagramUser.instagram_user_id == InstagramUser.id)
        .filter(
            BrandInstagramUser.brand_raw_id == brand.id,
            InstagramUser.user_type != "commenter",
            func.lower(func.trim(InstagramUser.niche)).in_(niches),
        )
        .distinct()
        if username
    }
    re_creators = {
        username.strip().lower()
        for (username,) in db.query(TestCreatorBrandPartnershipPost.creator_username)
        .join(ContentCreatorRE, TestCreatorBrandPartnershipPost.content_creator_re_id == ContentCreatorRE.id)
        .filter(
            TestCreatorBrandPartnershipPost.brand_raw_id == brand.id,
            func.lower(func.trim(ContentCreatorRE.niche)).in_(niches),
        )
        .distinct()
        if username
    }
    matching = len(collaborators | re_creators)
    if not matching:
        return 0.0
    return GROUP_MAX_POINTS["niche_creators"] * _NICHE_CREATOR_SHARE[min(matching, 2)]


def _confident_creator_niches(db: Session, brand: BrandRaw) -> set[tuple[str, str]]:
    """
    (username, niche) pairs, lowercased, for the brand's creators on a
    sponsorship_confidence >= 90 post: non-commenter Instagram collaborators
    on the brand's own posts (instagram_users.niche) and reverse-engineered
    creators on its test partnership posts (content_creator_re.niche).
    """
    collaborators = (
        db.query(InstagramUser.username, InstagramUser.niche)
        .join(BrandInstagramUser, BrandInstagramUser.instagram_user_id == InstagramUser.id)
        .join(InstagramPost, InstagramPost.post_id == InstagramUser.post_id)
        .filter(
            BrandInstagramUser.brand_raw_id == brand.id,
            InstagramPost.brand_raw_id == brand.id,
            InstagramPost.sponsorship_confidence >= _CONFIDENT_SPONSORSHIP,
            InstagramUser.user_type != "commenter",
            InstagramUser.niche.isnot(None),
        )
        .distinct()
        .all()
    )
    re_creators = (
        db.query(TestCreatorBrandPartnershipPost.creator_username, ContentCreatorRE.niche)
        .join(ContentCreatorRE, TestCreatorBrandPartnershipPost.content_creator_re_id == ContentCreatorRE.id)
        .filter(
            TestCreatorBrandPartnershipPost.brand_raw_id == brand.id,
            TestCreatorBrandPartnershipPost.sponsorship_confidence >= _CONFIDENT_SPONSORSHIP,
            ContentCreatorRE.niche.isnot(None),
        )
        .distinct()
        .all()
    )
    return {
        (username.strip().lower(), niche.strip().lower())
        for username, niche in [*collaborators, *re_creators]
        if username and username.strip() and niche and niche.strip()
    }


def _niche_tier(db: Session, creator: CreatorProfile, brand: BrandRaw) -> tuple[int, float]:
    """(tier, points) — the first condition that applies, see module docstring."""
    niches = _creator_niche_names(creator)
    if not niches:
        return 0, 0.0
    brand_niche = (brand.niche or "").strip().lower()
    creators = _confident_creator_niches(db, brand)

    if brand_niche and brand_niche in niches:
        if any(niche == brand_niche for _, niche in creators):
            return 3, _NICHE_TIER_POINTS[3]
        return 2, _NICHE_TIER_POINTS[2]

    matching = {username for username, niche in creators if niche in niches}
    if matching:
        points = min(_NICHE_TIER_POINTS_PER_CREATOR * len(matching), GROUP_MAX_POINTS["niche_tier"])
        return 1, points
    return 0, 0.0


def score_match(
    db: Session,
    creator: CreatorProfile,
    brand: BrandRaw,
    reasons: list[tuple[float, str, str]],
) -> dict:
    """
    `reasons` is match_text.collect_match_reasons() for this pair.

    Returns {"total_score": 0.55-1.0, "niche_tier": 0-3,
             "dimensions": {name: {"score", "weight"}}}
    where "base" is the fixed 55% floor and every other dimension is one
    tagline group: score = points earned / that group's max (0.0-1.0),
    weight = that group's share of the 55-100 range. So
    total_score == sum(score * weight) over all dimensions.
    """
    earned: dict[str, float] = {key: 0.0 for key in GROUP_MAX_POINTS}
    for priority, _text, key in reasons:
        top = _GROUP_TOP_PRIORITY.get(key)
        if top is None:   # niche_bridge, audience taglines — text only, no points
            continue
        points = GROUP_MAX_POINTS[key] * min(float(priority), top) / top
        earned[key] = max(earned[key], points)
    earned["niche_creators"] = _niche_creator_points(db, creator, brand)
    niche_tier, earned["niche_tier"] = _niche_tier(db, creator, brand)

    total = SCORE_BASE + SCORE_RANGE * sum(earned.values()) / _MAX_POINTS

    dimensions = {"base": {"score": 1.0, "weight": SCORE_BASE}}
    for key, max_points in GROUP_MAX_POINTS.items():
        dimensions[key] = {
            "score":  earned[key] / max_points,
            "weight": SCORE_RANGE * max_points / _MAX_POINTS,
        }

    return {
        "total_score": max(SCORE_BASE, min(1.0, total)),
        "niche_tier":  niche_tier,
        "dimensions":  dimensions,
    }
