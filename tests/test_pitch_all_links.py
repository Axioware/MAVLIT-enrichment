"""Every content link and past partnership the creator selected must reach the pitch, and be required by the prompt."""
from pipeline.helpers import prompts as P
from pipeline.pitch_posts import describe_entries, split_entries

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
