"""
pipeline/matching/niche_compatibility.py

Niche match dimension (25% weight in Stage 3 scoring) — "category
compatibility via a niche-to-niche lookup table" per the matching design
doc. In practice, brand niches are free-form strings typed at seed time
(confirmed against real data: 'electric bike', '3d printing', 'petroleum',
'Food & Beverage', ...) — NOT a fixed taxonomy, unlike creator_niche which
is a small fixed enum (music/podcast_audio/photography/gaming/education/
fitness/food_cooking/finance/substack_newsletter). A literal static lookup
table can't cover arbitrary brand niches, so this is a hybrid:
  1. Exact match (case-insensitive) -> 1.0
  2. Brand niche contains one of the creator niche's curated keywords -> 0.85
  3. Otherwise, rapidfuzz string similarity as a graceful fallback so an
     unmapped/unexpected brand niche still gets a reasonable, non-zero
     estimate instead of silently scoring 0.
"""

from rapidfuzz import fuzz

# Curated keyword sets per known creator niche — deliberately keyword-based
# (not exact brand-niche strings) so new brand niches seeded later still
# match sensibly without needing table maintenance.
_NICHE_KEYWORDS: dict[str, list[str]] = {
    "music":               ["music", "audio", "entertainment", "streaming"],
    "podcast_audio":       ["audio", "music", "tech", "entertainment", "streaming"],
    "photography":         ["tech", "fashion", "beauty", "camera", "creative"],
    "gaming":              ["gaming", "tech", "electronics", "3d printing", "esports"],
    "tech":                ["tech", "electronics", "gadget", "software", "gaming", "3d printing", "ecommerce"],
    "education":           ["tech", "family", "parenting", "books", "learning"],
    "fitness":             ["fitness", "health", "mental health", "food & beverage", "electric bike", "sport"],
    "food_cooking":        ["food", "beverage", "health", "fitness", "kitchen", "nutrition"],
    "finance":             ["ecommerce", "tech", "electric vehicle", "fintech", "banking", "insurance"],
    "substack_newsletter": ["media", "publishing", "news", "writing", "tech"],
}


def niche_compatibility(creator_niche: str | None, brand_niche: str | None) -> float | None:
    """
    Returns a 0.0-1.0 compatibility score, or None if either side is
    unknown (dimension gets skipped/reweighted upstream, not scored as 0).
    """
    if not creator_niche or not brand_niche:
        return None

    c = creator_niche.strip().lower()
    b = brand_niche.strip().lower()

    if c == b:
        return 1.0

    keywords = _NICHE_KEYWORDS.get(c, [])
    if any(kw in b for kw in keywords):
        return 0.85

    return fuzz.token_sort_ratio(c, b) / 100.0
