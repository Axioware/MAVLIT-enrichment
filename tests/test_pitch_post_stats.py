"""creator_post_stats: highest / average views and engagement of the creator's latest saved posts."""
import pytest

from pipeline.pitching import creator_post_stats



class FakeQuery:
    def __init__(self, rows): self.rows = rows
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def limit(self, n): self.rows = self.rows[:n]; return self
    def all(self): return self.rows


class FakeDb:
    def __init__(self, rows): self.rows = rows
    def query(self, *a): return FakeQuery(self.rows)


class Creator:
    id = 1
    instagram_handle = "@VinylGoneViral"


def test_views_and_engagement_figures():
    rows = [(100, 10, 1000), (200, 20, 3000), (300, 0, None), (400, 40, 2000), (500, 50, 6000), (-1, 5, 9999)]
    text = creator_post_stats(FakeDb(rows), Creator())
    assert text.startswith("across their latest 6 posts")
    # views: 1000, 3000, 2000, 6000, 9999
    assert "views per reel/video (5 with views): highest 9,999, average 4,400" in text
    # engagement (the hidden-likes post is left out): 110, 220, 300, 440, 550
    assert "engagement per post, likes + comments (5 posts): highest 550, average 324" in text


def test_unknown_without_enough_posts_or_handle():
    assert creator_post_stats(FakeDb([(1, 1, 1)] * 4), Creator()) == "unknown"
    nohandle = Creator(); nohandle.instagram_handle = ""
    assert creator_post_stats(FakeDb([(1, 1, 1)] * 10), nohandle) == "unknown"


def test_images_only_gives_engagement_only():
    text = creator_post_stats(FakeDb([(10, 1, None)] * 6), Creator())
    assert "views per" not in text and "engagement per post" in text
