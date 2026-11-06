"""The "Why it's a match" sentence speaks to the creator ("your size"), not about "the creator"."""
from pipeline.helpers.prompts import LLM_BRAND_RANKING_V3_DEFAULT_PROMPT as DEFAULT, LLM_BRAND_RANKING_V3_OLD_REASON_FORMAT as OLD
from pipeline.matching.llm_ranking_v3 import _with_reason_voice
from pipeline.matching.match_text import address_creator as to_you


def test_the_example_from_arctic_fox():
    before = ("Its DIY-ready vivid hair color and conditioning formula directly match the creator’s dye transformations; "
              "the strongest signal is partnerships with similar creators near the creator’s size.")
    after = ("Its DIY-ready vivid hair color and conditioning formula directly match your dye transformations; "
             "the strongest signal is partnerships with similar creators near your size.")
    assert to_you(before) == after


def test_other_third_person_forms():
    assert to_you("Fits the creator's niche.") == "Fits your niche."
    assert to_you("This creator's audience skews female.") == "Your audience skews female."
    assert to_you("The creator is a drummer.") == "You are a drummer."
    assert to_you("Great for the creator.") == "Great for you."
    assert to_you("Because the creator has a following of 20k.") == "Because you have a following of 20k."
    assert to_you("Brand works with the creators in your niche.") == "Brand works with the creators in your niche."   # plural: not about this creator


def test_text_already_addressed_to_you_is_unchanged_and_empty_is_safe():
    text = "Matches your dye transformations; partnerships near your size."
    assert to_you(text) == text
    assert to_you(None) is None and to_you("") == ""


def test_the_default_prompt_asks_for_the_second_person():
    assert "second person" in DEFAULT and "speak TO them" in DEFAULT
    assert OLD not in DEFAULT


def test_an_old_stored_prompt_gets_the_same_instruction():
    old_copy = f'Intro\n\nReply ONLY with this JSON object, no extra text:\n{{"rankings": [{{"id": 1, "confidence": 0, "reason": "{OLD}"}}]}}'
    fixed = _with_reason_voice(old_copy)
    assert OLD not in fixed and "second person" in fixed
    assert _with_reason_voice(fixed) == fixed   # idempotent
    no_format = "Intro\n\nReply ONLY with this JSON object: {}"
    assert "speak TO them" in _with_reason_voice(no_format)
