"""
Which gender the matching uses for a creator.

The creator's audience gender (the dropdown on creator-profile.html: male /
female / both) is what brands are matched against; the creator's own
profile gender is only the fallback when no audience gender is set.

Order: instagram_audience_gender, then youtube_audience_gender, then gender.
"""

from pipeline.db import CreatorProfile

_VALID = ("male", "female", "both")


def creator_match_gender(creator: CreatorProfile) -> str:
    """
    "male", "female" or "both" - or "" when nothing usable is set. "both" (and
    "") mean the matching ignores gender: no hard filter, no opposite-gender
    penalty, no gender tagline.
    """
    for value in (creator.instagram_audience_gender, creator.youtube_audience_gender, creator.gender):
        gender = (value or "").strip().lower()
        if gender in _VALID:
            return gender
    return ""
