"""The brand's social pages shown as logos in the contact panel: only real https links to the platform itself."""
from types import SimpleNamespace

from pipeline.brand_detail import _social_url, brand_social_links


def test_stored_urls_are_kept_and_cleaned():
    assert _social_url("instagram", "https://www.instagram.com/shopbop") == "https://www.instagram.com/shopbop"
    assert _social_url("instagram", "http://instagram.com/shopbop/") == "https://instagram.com/shopbop"
    assert _social_url("facebook", "https://www.facebook.com/GlassesUSA") == "https://www.facebook.com/GlassesUSA"
    assert _social_url("youtube", "https://www.youtube.com/channel/UC855jsZ_UwY0zq7YzhUZR6A") == "https://www.youtube.com/channel/UC855jsZ_UwY0zq7YzhUZR6A"
    assert _social_url("youtube", "https://www.youtube.com/@beneunder4088") == "https://www.youtube.com/@beneunder4088"


def test_bare_handles_and_channel_ids_become_links():
    assert _social_url("instagram", "@shopbop") == "https://www.instagram.com/shopbop"
    assert _social_url("instagram", "shopbop") == "https://www.instagram.com/shopbop"
    assert _social_url("facebook", "GlassesUSA") == "https://www.facebook.com/GlassesUSA"
    assert _social_url("youtube", "UC855jsZ_UwY0zq7YzhUZR6A") == "https://www.youtube.com/channel/UC855jsZ_UwY0zq7YzhUZR6A"
    assert _social_url("youtube", "@beneunder4088") == "https://www.youtube.com/@beneunder4088"


def test_anything_that_is_not_the_platform_or_not_a_link_is_dropped():
    for platform, bad in [("instagram", "javascript:alert(1)"), ("instagram", "https://evil.example/instagram.com/x"),
                          ("instagram", "https://instagram.com.evil.example/x"), ("facebook", "https://www.instagram.com/x"),
                          ("youtube", "not a link at all"), ("instagram", ""), ("instagram", None), ("youtube", "UCshort")]:
        assert _social_url(platform, bad) is None, (platform, bad)


def test_the_three_links():
    brand = SimpleNamespace(instagram_handle="https://www.instagram.com/shopbop", facebook_page=None, youtube_channel_id="https://www.youtube.com/@x1")
    assert brand_social_links(brand) == {"instagram": "https://www.instagram.com/shopbop", "facebook": None, "youtube": "https://www.youtube.com/@x1"}
