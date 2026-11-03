"""Every content link and past partnership the creator selected must reach the pitch, and be required by the prompt."""
from pipeline.helpers import prompts as P
from pipeline.pitch_posts import describe_entries, split_entries

# the default as it was before the comparison rule and the content/number examples were added
BEFORE_EXAMPLES = P.PITCH_DEFAULT_PROMPT.replace(P.PITCH_COMPARISON_RULE, "").replace(P.PITCH_METRIC_EXAMPLES, "")
LINKS = ("https://www.instagram.com/p/DeuON3BpcmB/,https://www.instagram.com/p/DeiF93ZOr4J/,"
         "https://www.instagram.com/p/DeenHsmOZJc/")


def test_all_three_links_are_given_to_the_llm():
    text = describe_entries(None, None, LINKS, {})
    assert len(split_entries(LINKS)) == 3
    for code in ("DeuON3BpcmB", "DeiF93ZOr4J", "DeenHsmOZJc"):
        assert code in text


def test_prompt_requires_every_content_link():
    prompt = P.PITCH_DEFAULT_PROMPT
    assert "Include every supplied content link" in prompt
    assert "do not drop any" in prompt
    assert "Keep every supplied post URL" in prompt
    assert "when available and relevant" not in prompt


def test_previous_wording_is_still_recognised_as_an_unedited_default():
    assert any("when available and relevant" in old for old in P.PITCH_PREVIOUS_DEFAULT_PROMPTS)
    assert P.PITCH_DEFAULT_PROMPT not in P.PITCH_PREVIOUS_DEFAULT_PROMPTS


def test_a_dropped_content_link_is_added_before_the_sign_off():
    from pipeline.pitch_posts import ensure_content_links
    pitch = ("Hello team,\n\nI love your pressings: https://www.instagram.com/p/DeuON3BpcmB/\n\n"
             "Would you be open to a partnership?\n\nThanks,\nAlex")
    out = ensure_content_links(pitch, LINKS)
    for code in ("DeuON3BpcmB", "DeiF93ZOr4J", "DeenHsmOZJc"):
        assert out.count(code) == 1
    assert out.endswith("Would you be open to a partnership?\n\nThanks,\nAlex")
    assert out.index("DeiF93ZOr4J") < out.index("Would you be open")


def test_nothing_is_added_when_all_links_are_present_or_none_given():
    from pipeline.pitch_posts import ensure_content_links
    full = "Hi\n\n" + " ".join(["https://www.instagram.com/p/DeuON3BpcmB/", "https://www.instagram.com/p/DeiF93ZOr4J/",
                                "https://www.instagram.com/p/DeenHsmOZJc/"]) + "\n\nThanks,\nAlex"
    assert ensure_content_links(full, LINKS) == full
    assert ensure_content_links("Hi\n\nThanks", None) == "Hi\n\nThanks"
    assert ensure_content_links("Hi\n\nThanks", "my website, https://example.com") == "Hi\n\nThanks"


def test_post_statistics_are_in_the_prompt_and_the_previous_release_is_recognised():
    d = P.PITCH_DEFAULT_PROMPT
    assert "{creator_post_stats}" in d and "median" in P.PITCH_STATS_RULE
    old = next(x for x in P.PITCH_PREVIOUS_DEFAULT_PROMPTS if "when available and relevant" in x)
    assert "{creator_post_stats}" not in old
    # the release just before the statistics (links already required) is recognised too
    assert BEFORE_EXAMPLES.replace(P.PITCH_STATS_LINE, "").replace(P.PITCH_STATS_RULE, "") in P.PITCH_PREVIOUS_DEFAULT_PROMPTS


def test_word_range_follows_the_number_of_content_links():
    from pipeline.pitching import pitch_word_range
    one = "https://www.instagram.com/p/DeuON3BpcmB/"
    two = one + ", https://www.instagram.com/p/DeiF93ZOr4J/"
    assert pitch_word_range(None) == pitch_word_range("") == pitch_word_range(one) == "120 to 190"
    assert pitch_word_range(two) == "150 to 210"
    assert pitch_word_range(LINKS) == "180 to 250"
    assert pitch_word_range(LINKS + ", https://example.com") == "180 to 250"   # at most 3 entries count


def test_prompt_uses_the_range_placeholder_and_the_fixed_length_release_is_recognised():
    assert "approximately {pitch_word_range} words" in P.PITCH_DEFAULT_PROMPT
    assert "120 to 190 words" not in P.PITCH_DEFAULT_PROMPT
    current_with_fixed = BEFORE_EXAMPLES.replace("{pitch_word_range}", "120 to 190")
    assert current_with_fixed in P.PITCH_PREVIOUS_DEFAULT_PROMPTS


def test_per_post_figures_are_given_and_expected():
    from pipeline.pitch_posts import describe_entries
    url = "https://www.instagram.com/p/DeuON3BpcmB/"
    text = describe_entries(None, None, url, {"DeuON3BpcmB": {"caption": "x", "views": 45000, "likes": 3200, "comments": 150}})
    assert "views: 45,000" in text and "likes: 3,200" in text and "comments: 150" in text
    assert "engagement (likes + comments): 3,350" in text
    hidden = describe_entries(None, None, url, {"DeuON3BpcmB": {"caption": "x", "views": None, "likes": -1, "comments": 5}})
    assert "likes" not in hidden and "engagement" not in hidden
    assert "For every supplied post that comes with figures" in P.PITCH_POSTS_RULE
    assert "include one short sentence" in P.PITCH_STATS_RULE


def test_the_release_before_is_recognised_as_an_unedited_default():
    prior = BEFORE_EXAMPLES.replace(P.PITCH_POSTS_RULE, P._PRIOR_PITCH_POSTS_RULE).replace(P.PITCH_STATS_RULE, P._PRIOR_PITCH_STATS_RULE)
    assert prior in P.PITCH_PREVIOUS_DEFAULT_PROMPTS and prior != P.PITCH_DEFAULT_PROMPT
