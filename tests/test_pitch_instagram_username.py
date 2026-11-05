"""The pitch always names the creator's Instagram username, never with an @."""
from types import SimpleNamespace

from pipeline import pitching
from pipeline.pitching import PITCH_HANDLE_RULE, creator_instagram_username, ensure_instagram_username


def test_username_is_bare():
    for raw in ("@vinyl", "vinyl", "https://www.instagram.com/vinyl/"):
        assert creator_instagram_username(SimpleNamespace(instagram_handle=raw, creator_handle=None)) == "vinyl"
    assert creator_instagram_username(SimpleNamespace(instagram_handle=None, creator_handle="@fallback")) == "fallback"
    assert creator_instagram_username(SimpleNamespace(instagram_handle=None, creator_handle=None)) == ""


def test_at_sign_is_removed_and_a_missing_username_is_added():
    assert ensure_instagram_username("I post as @vinyl daily.", "vinyl") == "I post as vinyl daily."
    assert ensure_instagram_username("Hi @Vinyl and @@vinyl", "vinyl") == "Hi Vinyl and vinyl"
    added = ensure_instagram_username("Hi there.\n\nBest,\nAnna", "vinyl")
    assert added.endswith("\n\nInstagram: vinyl") and "@" not in added
    assert ensure_instagram_username("Hi there.", "") == "Hi there."


def test_a_domain_that_starts_with_the_username_does_not_count_as_mentioning_it():
    assert ensure_instagram_username("see vinyl.com", "vinyl").endswith("Instagram: vinyl")


def test_the_prompt_template_uses_the_bare_username():
    assert "@{creator_handle}" not in PITCH_HANDLE_RULE and "{creator_handle}" in PITCH_HANDLE_RULE
    template = "Instagram: @{creator_handle}\nrest".replace("@{creator_handle}", "{creator_handle}")
    assert template.startswith("Instagram: {creator_handle}")
