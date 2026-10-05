"""
pipeline/matching/scoring.py

Stage 3 Step C — match score built from the "why it's a match" taglines
(pipeline/matching/match_text.py, documented in taglines.md).

Every brand starts at 55%. Each tagline group can add up to its
GROUP_MAX_POINTS (they sum to 45, so 1 point = +1% and a brand that maxes
every group scores 100%). Only one option per tagline group can apply; it
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

One extra signal that has no tagline of its own:
    niche_creators  the brand's niche is one of the creator's niches AND
                    1 of the brand's creators (Instagram collaborator or RE
                    creator) is in one of them too -> 2/3 of its points;
                    2+ such creators -> all of its points

    total = 0.55 + sum(points) / 100      (returned as 0.55-1.0)
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from pipeline.db import (
    BrandInstagramUser,
    BrandRaw,
    ContentCreatorRE,
    CreatorProfile,
    InstagramUser,
    TestCreatorBrandPartnershipPost,
)
from pipeline.matching.match_text import _creator_niche_names

SCORE_BASE  = 0.55   # score with no applicable signals
SCORE_RANGE = 0.45   # SCORE_BASE + SCORE_RANGE = 1.0 when every group is maxed

# Most points (= % of the final score) each group can add. Sums to 45
# (= SCORE_RANGE * 100).
GROUP_MAX_POINTS: dict[str, float] = {
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


def score_match(
    db: Session,
    creator: CreatorProfile,
    brand: BrandRaw,
    reasons: list[tuple[float, str, str]],
) -> dict:
    """
    `reasons` is match_text.collect_match_reasons() for this pair.

    Returns {"total_score": 0.55-1.0, "dimensions": {name: {"score", "weight"}}}
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

    total = SCORE_BASE + SCORE_RANGE * sum(earned.values()) / _MAX_POINTS

    dimensions = {"base": {"score": 1.0, "weight": SCORE_BASE}}
    for key, max_points in GROUP_MAX_POINTS.items():
        dimensions[key] = {
            "score":  earned[key] / max_points,
            "weight": SCORE_RANGE * max_points / _MAX_POINTS,
        }

    return {"total_score": max(SCORE_BASE, min(1.0, total)), "dimensions": dimensions}
