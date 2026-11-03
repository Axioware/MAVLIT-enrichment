"""Per-post comparisons (multiples of followers / usual views, engagement rate) and the prompt examples. No OpenAI, no database."""
from types import SimpleNamespace

from pipeline.helpers.prompts import (
    PITCH_COMPARISON_RULE,
    PITCH_DEFAULT_PROMPT,
    PITCH_METRIC_EXAMPLES,
    PITCH_PREVIOUS_DEFAULT_PROMPTS,
)
from pipeline.pitch_posts import describe_entries, multiple_label

URL = "https://www.instagram.com/p/DeAAAAAAAA1/"


def _describe(views, likes, comments, followers, usual):
    details = {"DeAAAAAAAA1": {"caption": "Unboxing", "views": views, "likes": likes, "comments": comments}}
    return describe_entries(None, SimpleNamespace(), URL, details, followers, usual)


def test_multiples_round_down_to_the_nearest_half_and_start_at_one_and_a_half():
    assert multiple_label(8240, 4000) == "about 2x"
    assert multiple_label(12400, 4000) == "about 3x"
    assert multiple_label(6500, 4000) == "about 1.5x"
    assert multiple_label(5900, 4000) is None
    assert multiple_label(100, 0) is None and multiple_label(100, None) is None and multiple_label(0, 100) is None


def test_post_line_carries_the_comparisons_and_the_engagement_rate():
    line = _describe(8240, 300, 40, 4000, 2700)
    assert "views: 8,240" in line
    assert "views are about 2x their follower count" in line
    assert "views are about 3x their usual views per reel" in line
    assert "engagement rate (likes + comments, as % of followers): 8.5%" in line


def test_no_comparison_without_a_base_or_with_hidden_likes():
    line = _describe(8240, -1, 5, None, None)
    assert "views: 8,240" in line and "about" not in line and "engagement" not in line


def test_default_prompt_has_the_examples_and_the_rule_and_the_old_default_still_upgrades():
    assert PITCH_METRIC_EXAMPLES in PITCH_DEFAULT_PROMPT and PITCH_COMPARISON_RULE in PITCH_DEFAULT_PROMPT
    assert "Goo Goo Dolls anniversary pressing" in PITCH_DEFAULT_PROMPT
    from pipeline.helpers import prompts as P
    release1 = (PITCH_DEFAULT_PROMPT.replace(P.PITCH_LAYOUT_RULE, "").replace(P.PITCH_POSTS_RULE, P._R1_POSTS_RULE)
                .replace(PITCH_COMPARISON_RULE, P._R1_COMPARISON_RULE).replace(PITCH_METRIC_EXAMPLES, P._R1_METRIC_EXAMPLES))
    base = release1.replace(P._R1_COMPARISON_RULE, "").replace(P._R1_METRIC_EXAMPLES, "")
    assert base in PITCH_PREVIOUS_DEFAULT_PROMPTS and release1 in PITCH_PREVIOUS_DEFAULT_PROMPTS
    assert PITCH_DEFAULT_PROMPT not in PITCH_PREVIOUS_DEFAULT_PROMPTS


def test_layout_rule_orders_paid_before_content_and_bans_bracketed_figures():
    from pipeline.helpers.prompts import PITCH_LAYOUT_RULE
    assert PITCH_LAYOUT_RULE in PITCH_DEFAULT_PROMPT
    assert PITCH_LAYOUT_RULE.index("Paid partnership paragraph") < PITCH_LAYOUT_RULE.index("Relevant content paragraph")
    assert "Never put figures in\n  parentheses or brackets" in PITCH_LAYOUT_RULE
    assert "(about twice" not in PITCH_DEFAULT_PROMPT and '"(about 45K views)"' not in PITCH_DEFAULT_PROMPT
