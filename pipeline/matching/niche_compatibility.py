"""
pipeline/matching/niche_compatibility.py

Niche match dimension (25% weight in Stage 3 scoring) — "category
compatibility via a niche-to-niche lookup table" per the matching design
doc. Creators now pick their primary niche(s) directly from the same
distinct-niche vocabulary brands are seeded with (see GET /brand-niches and
creator-profile.html), so both sides of the comparison are drawn from the
same free-form string space — no curated keyword table is needed to bridge
a fixed creator-niche enum against arbitrary brand niches anymore.

creator_niche may be a single niche or a comma-separated list (a creator can
select more than one primary niche). Each selected niche is compared against
the brand niche and the best score wins:
  1. Exact match (case-insensitive) -> 1.0
  2. Otherwise, rapidfuzz string similarity as a graceful fallback so a
     near-miss (e.g. "3d printing" vs "3D Printing & Design") still gets a
     reasonable, non-zero estimate instead of scoring 0.
"""

from rapidfuzz import fuzz


def niche_compatibility(creator_niche: str | None, brand_niche: str | None) -> float | None:
    """
    Returns a 0.0-1.0 compatibility score, or None if either side is
    unknown (dimension gets skipped/reweighted upstream, not scored as 0).
    """
    if not creator_niche or not brand_niche:
        return None

    b = brand_niche.strip().lower()
    creator_niches = [n.strip().lower() for n in creator_niche.split(",") if n.strip()]
    if not creator_niches:
        return None

    best = 0.0
    for c in creator_niches:
        if c == b:
            return 1.0
        best = max(best, fuzz.token_sort_ratio(c, b) / 100.0)

    return best
