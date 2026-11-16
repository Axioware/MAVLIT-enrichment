"""When MAVLIT generates a creator's Instagram profile it also estimates the content mix: the share of product-focused posts and the five biggest
content categories, in percent. The same single LLM call returns them; they are saved on the creator's profile."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.creator_content import instagram_description as d  # noqa: E402
from pipeline.helpers import prompts  # noqa: E402


class ParsingTheContentMix(unittest.TestCase):
    def mix(self, **result):
        return d.content_mix_from_result(result)

    def test_the_five_biggest_categories_in_percent_biggest_first(self):
        out = self.mix(product_focused_pct=35, content_categories={"Tutorial": 32, "Product review": 18, "Lifestyle": 15, "Entertainment": 12, "Educational": 9, "Storytelling": 8, "Promotional": 6})
        self.assertEqual(out["product_focused_pct"], 35)
        self.assertEqual(out["content_categories"], [{"name": "Tutorial", "pct": 32}, {"name": "Product review", "pct": 18}, {"name": "Lifestyle", "pct": 15},
                                                    {"name": "Entertainment", "pct": 12}, {"name": "Educational", "pct": 9}])

    def test_names_are_matched_ignoring_case_and_unknown_names_are_dropped(self):
        out = self.mix(content_categories={"tutorial": "40%", "PRODUCT REVIEW": 30, "Memes": 30})
        self.assertEqual(out["content_categories"], [{"name": "Tutorial", "pct": 40}, {"name": "Product review", "pct": 30}])

    def test_a_list_of_name_and_pct_is_accepted_too(self):
        out = self.mix(content_categories=[{"name": "Lifestyle", "pct": 60}, {"name": "Tutorial", "pct": 40}, "junk"])
        self.assertEqual(out["content_categories"], [{"name": "Lifestyle", "pct": 60}, {"name": "Tutorial", "pct": 40}])

    def test_percentages_over_100_are_scaled_down(self):
        out = self.mix(content_categories={"Tutorial": 80, "Lifestyle": 80})
        self.assertEqual(sum(c["pct"] for c in out["content_categories"]), 100)
        self.assertEqual([c["name"] for c in out["content_categories"]], ["Lifestyle", "Tutorial"])   # equal shares: alphabetical

    def test_unusable_values_become_none(self):
        self.assertEqual(self.mix(), {"product_focused_pct": None, "content_categories": None})
        self.assertEqual(self.mix(product_focused_pct=150, content_categories="Tutorial"), {"product_focused_pct": None, "content_categories": None})
        self.assertEqual(self.mix(product_focused_pct=-5)["product_focused_pct"], None)
        self.assertEqual(self.mix(product_focused_pct=True)["product_focused_pct"], None)
        self.assertEqual(self.mix(product_focused_pct="42.4%")["product_focused_pct"], 42)
        self.assertEqual(self.mix(product_focused_pct=0)["product_focused_pct"], 0)                  # zero is a real answer
        self.assertIsNone(self.mix(content_categories={"Tutorial": 0, "Lifestyle": "x"})["content_categories"])


class ThePrompt(unittest.TestCase):
    def test_the_default_prompt_and_the_note_ask_for_the_two_keys(self):
        for text in (prompts.CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT, prompts.CREATOR_CONTENT_MIX_NOTE):
            self.assertIn("product_focused_pct", text)
            self.assertIn("content_categories", text)
            for category in prompts.CREATOR_CONTENT_CATEGORIES:
                self.assertIn(category, text)

    def test_a_stored_prompt_copy_without_the_keys_gets_the_note(self):
        db = mock.Mock()
        db.query.return_value.filter.return_value.first.return_value = mock.Mock(content="old prompt audience_gender audience_description sub_niches")
        self.assertIn("ADDITIONAL REQUIRED OUTPUT - CONTENT MIX", d._get_description_prompt(db))
        db.query.return_value.filter.return_value.first.return_value = mock.Mock(content=prompts.CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT)
        self.assertNotIn("ADDITIONAL REQUIRED OUTPUT - CONTENT MIX", d._get_description_prompt(db))   # a current copy is left alone

    def test_the_filled_prompt_keeps_the_json_example_and_fills_the_placeholders(self):
        from pipeline.helpers.gpt_llm import fill_template
        filled = fill_template(prompts.CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT + prompts.CREATOR_CONTENT_MIX_NOTE, niche="Music", bio="b", captions="c", followers="10", sub_niche_options="none")
        self.assertNotIn("{niche}", filled)
        self.assertIn('"Tutorial": 32', filled)


class GenerationReturnsTheMix(unittest.TestCase):
    def test_the_generated_profile_carries_the_content_mix(self):
        items = [{"id": str(i), "caption": f"c{i}", "likesCount": 10, "commentsCount": 1, "metaData": {"followersCount": 1000, "biography": "bio"}} for i in range(3)]
        reply = {"description": "a creator", "product_focused_pct": 35, "content_categories": {"Tutorial": 32, "Product review": 18, "Lifestyle": 50}}
        with mock.patch.object(d, "_saved_items", return_value=items), mock.patch.object(d, "_POSTS_LIMIT", 3), mock.patch.object(d, "sub_niche_options", return_value=[]), \
                mock.patch.object(d, "_get_description_prompt", return_value="{niche}{bio}{captions}{sub_niche_options}{followers}"), mock.patch.object(d, "call_gpt_json", return_value=reply):
            result = d.generate_instagram_description(mock.Mock(), "someone", "Music")
        self.assertEqual(result["product_focused_pct"], 35)
        self.assertEqual(result["content_categories"], [{"name": "Lifestyle", "pct": 50}, {"name": "Tutorial", "pct": 32}, {"name": "Product review", "pct": 18}])

    def test_a_reply_without_the_mix_still_generates_the_profile(self):
        items = [{"id": "1", "caption": "c", "likesCount": 10, "commentsCount": 1, "metaData": {"followersCount": 1000, "biography": "bio"}}]
        with mock.patch.object(d, "_saved_items", return_value=items), mock.patch.object(d, "_POSTS_LIMIT", 1), mock.patch.object(d, "sub_niche_options", return_value=[]), \
                mock.patch.object(d, "_get_description_prompt", return_value="{niche}"), mock.patch.object(d, "call_gpt_json", return_value={"description": "a creator"}):
            result = d.generate_instagram_description(mock.Mock(), "someone", "Music")
        self.assertEqual((result["product_focused_pct"], result["content_categories"]), (None, None))


if __name__ == "__main__":
    unittest.main()
