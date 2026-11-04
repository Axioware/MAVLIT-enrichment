"""The early-access form: one or more of four niches, no Substack. No database."""
import pytest
from pydantic import ValidationError

from api.early_access import EarlyAccessRequest

BASE = {"email": "A@b.co", "handle": "@h", "platform": "instagram", "followers": "under10k"}


def test_several_niches_are_accepted_and_a_single_string_still_works():
    assert EarlyAccessRequest(**BASE, niche=["beauty", "music"]).niche == ["beauty", "music"]
    assert EarlyAccessRequest(**BASE, niche="health").niche == ["health"]


@pytest.mark.parametrize("niche", [[], ["food"], ["other"], ["music", "cars"], "other", ["music", "health", "fitness", "beauty", "music"]])
def test_other_niches_and_an_empty_choice_are_refused(niche):
    with pytest.raises(ValidationError):
        EarlyAccessRequest(**BASE, niche=niche)


def test_substack_is_no_longer_a_platform():
    with pytest.raises(ValidationError):
        EarlyAccessRequest(**{**BASE, "platform": "substack"}, niche=["music"])
