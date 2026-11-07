"""The pitch always names the creator's Instagram username, never with an @."""
from types import SimpleNamespace

from pipeline import pitching
from pipeline.pitching import PITCH_HANDLE_RULE, creator_instagram_username, ensure_instagram_username


def test_username_is_bare():
    for raw in ("@vinyl", "vinyl", "https://www.instagram.com/vinyl/"):
        assert creator_instagram_username(SimpleNamespace(instagram_handle=raw, creator_handle=None)) == "vinyl"
    assert creator_instagram_username(SimpleNamespace(instagram_handle=None, creator_handle="@fallback")) == "fallback"
    assert creator_instagram_username(SimpleNamespace(instagram_handle=None, creator_handle=None)) == ""


def test_channel_is_written_as_at_username_on_instagram():
    ok = "I'm Brett, and as @vinyl on Instagram I share surf."
    assert ensure_instagram_username(ok, "vinyl") == ok
    assert ensure_instagram_username("as @@Vinyl on Instagram", "vinyl") == "as @Vinyl on Instagram"
    assert ensure_instagram_username("and as vinyl I share surf.", "vinyl") == "and as @vinyl on Instagram I share surf."
    assert ensure_instagram_username("and as @vinyl I share surf.", "vinyl") == "and as @vinyl on Instagram I share surf."


def test_a_missing_username_is_added():
    added = ensure_instagram_username("Hi there.\n\nBest,\nAnna", "vinyl")
    assert added.endswith("\n\nFind me as @vinyl on Instagram.")
    assert ensure_instagram_username("Hi there.", "") == "Hi there."


def test_a_domain_that_starts_with_the_username_does_not_count_as_mentioning_it():
    assert ensure_instagram_username("see vinyl.com", "vinyl").endswith("as @vinyl on Instagram.")


def test_the_prompt_rule_asks_for_the_phrase():
    assert 'as @{creator_handle} on Instagram' in PITCH_HANDLE_RULE
