"""The "Highest Instagram reel / post views" value: the best view count, or the best play count when the account hides its views."""
from pipeline.creator_content.instagram_description import highest_views_from_items as best


def test_view_counts_win_when_they_exist():
    items = [{"videoViewCount": 1200, "videoPlayCount": 99999}, {"videoViewCount": 5000, "videoPlayCount": 100}, {"videoViewCount": None}]
    assert best(items) == 5000


def test_hidden_views_fall_back_to_the_highest_play_count():
    items = [{"videoViewCount": None, "videoPlayCount": 8000}, {"videoViewCount": -1, "videoPlayCount": 31000}, {"videoPlayCount": 400}]
    assert best(items) == 31000


def test_zero_views_count_as_hidden_too():
    assert best([{"videoViewCount": 0, "videoPlayCount": 2500}]) == 2500


def test_nothing_to_show_stays_none_so_a_typed_value_is_kept():
    assert best([{"caption": "photo"}, {"videoViewCount": None, "videoPlayCount": None}, {"videoPlayCount": True}]) is None
    assert best([]) is None
