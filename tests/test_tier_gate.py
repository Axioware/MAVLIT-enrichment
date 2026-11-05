"""Enrichment is blocked for 'extreme' brands (and unclassified ones) only; higher-range continues."""
from pipeline.enrichment import instagram_posts
from pipeline.enrichment.geo_reach import geo_reach
from pipeline.enrichment_re import brand_instagram_profile

ALLOWED = {"lower-range", "midlower-range", "midhigher-range", "higher-range"}


def test_all_three_steps_use_the_same_tiers():
    for module in (instagram_posts, geo_reach, brand_instagram_profile):
        tiers = set(module._ELIGIBLE_BRAND_TIERS)
        assert tiers == ALLOWED, module.__name__
        assert "extreme" not in tiers and None not in tiers and "" not in tiers
