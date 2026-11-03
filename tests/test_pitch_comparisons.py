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
    release1 = (PITCH_DEFAULT_PROMPT.replace(P.PITCH_CLOSING_RULE, "").replace(P.PITCH_LAYOUT_RULE, "").replace(P.PITCH_POSTS_RULE, P._R1_POSTS_RULE)
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


def test_every_post_gets_its_figure_in_words_before_its_url_and_the_previous_layout_still_upgrades():
    from pipeline.helpers import prompts as P
    assert "never in parentheses" in P.PITCH_LAYOUT_RULE and "never a bare\n  number" in P.PITCH_LAYOUT_RULE
    assert PITCH_DEFAULT_PROMPT.replace(P.PITCH_CLOSING_RULE, "").replace(P.PITCH_LAYOUT_RULE, P._R2_LAYOUT_RULE) in PITCH_PREVIOUS_DEFAULT_PROMPTS


PAST = "https://www.instagram.com/p/De97ECxtg-V/, https://www.instagram.com/p/De7BmS1ug5N/"
CONTENT = "https://www.instagram.com/p/DfN-oawPdzj/, https://www.instagram.com/p/DfI8oucBjYl/"
MODEL_ANSWER = """Hi,

I make content about vinyl collecting and anniversary releases.

A couple of examples of my music posts: a look back at The Killers' Hot Fuss got about 6,400 likes and comments (https://www.instagram.com/p/DfN-oawPdzj/), and an Outkast post got about 26,000 (https://www.instagram.com/p/DfI8oucBjYl/). Across my latest 20 posts, I averaged about 2,400 likes and comments per post.

I've also made sponsored content for the Goo Goo Dolls, featuring the Blue Iris pressing (461 likes and comments: https://www.instagram.com/p/De97ECxtg-V/), and Qobuz, revisiting 2006 albums (700 likes and comments: https://www.instagram.com/p/De7BmS1ug5N/).

Would you be open to discussing a paid partnership?

Thanks,
Brett"""


def test_tidy_pitch_removes_brackets_around_figures_and_puts_paid_posts_first():
    from pipeline.pitch_posts import tidy_pitch
    out = tidy_pitch(MODEL_ANSWER, PAST, CONTENT)
    assert "(" not in out and ")" not in out
    assert "pressing, with 461 likes and comments: https://www.instagram.com/p/De97ECxtg-V/" in out
    assert "Hot Fuss got about 6,400 likes and comments: https://www.instagram.com/p/DfN-oawPdzj/, and" in out
    paragraphs = out.split("\n\n")
    paid = next(i for i, p in enumerate(paragraphs) if "De97ECxtg-V" in p)
    content = next(i for i, p in enumerate(paragraphs) if "DfN-oawPdzj" in p)
    assert paid < content and paragraphs[0] == "Hi," and paragraphs[-1].endswith("Thanks,\nBrett")
    assert tidy_pitch(out, PAST, CONTENT) == out                       # nothing left to fix


def test_tidy_pitch_leaves_a_pitch_that_mixes_both_kinds_in_one_paragraph_in_order():
    from pipeline.pitch_posts import tidy_pitch
    mixed = "Hi,\n\nSee https://www.instagram.com/p/DfN-oawPdzj/ and https://www.instagram.com/p/De97ECxtg-V/.\n\nThanks,\nBrett"
    assert tidy_pitch(mixed, PAST, CONTENT) == mixed
    assert tidy_pitch("Plain (2006) text.", None, None) == "Plain (2006) text."


def test_tidy_pitch_drops_the_median_clause_and_rewrites_an_old_closing_question():
    from pipeline.pitch_posts import tidy_pitch
    text = ("Hi,\n\nAcross my latest 20 posts, engagement averaged about 2,400 likes and comments per post, with a median of about 600.\n\n"
            "Would you be open to discussing a paid partnership?\n\nBrett")
    out = tidy_pitch(text, None, None, "Vertigo Vinyl")
    assert "median" not in out and "per post.\n\n" in out
    assert out.endswith("Would you be open to talking about how we could feature Vertigo Vinyl's products with my audience?\n\nBrett")
    keep = "Hi,\n\nWould you be open to talking about how we could feature Vertigo Vinyl's records with my audience?\n\nBrett"
    assert tidy_pitch(keep, None, None, "Vertigo Vinyl") == keep          # the model's own noun is kept
    assert tidy_pitch(text, None, None) .count("paid partnership") == 1    # no brand name known: left as written


def test_closing_rule_is_in_the_default_and_the_previous_release_still_upgrades():
    from pipeline.helpers import prompts as P
    assert P.PITCH_CLOSING_RULE in P.PITCH_DEFAULT_PROMPT and "feature {brand_name}'s" in P.PITCH_CLOSING_RULE
    assert P.PITCH_DEFAULT_PROMPT.replace(P.PITCH_CLOSING_RULE, "") in P.PITCH_PREVIOUS_DEFAULT_PROMPTS
