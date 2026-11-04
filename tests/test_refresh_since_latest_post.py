"""Refresh scrapes only posts newer than the latest stored post (no DB, no Apify)."""
from pipeline.enrichment import instagram_users
from pipeline.enrichment_re import content_creator_re as ccre


def test_scrape_posts_passes_cutoff(monkeypatch):
    seen = {}

    def fake_run(actor_id, run_input, **kw):
        seen.update(run_input)
        return [{"id": str(i)} for i in range(100)]

    monkeypatch.setattr(instagram_users, "run_apify_actor", fake_run)
    out = instagram_users._scrape_posts("bob", 60, since="2026-11-01")
    assert seen["onlyPostsNewerThan"] == "2026-11-01"
    assert seen["resultsLimit"] == 60
    assert len(out) == 60


def test_scrape_posts_without_cutoff_is_unchanged(monkeypatch):
    seen = {}
    monkeypatch.setattr(instagram_users, "run_apify_actor", lambda a, i, **k: seen.update(i) or [])
    instagram_users._scrape_posts("bob", 40)
    assert "onlyPostsNewerThan" not in seen
    assert seen["resultsLimit"] == 40


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a):
        return self

    def group_by(self, *a):
        return self

    def all(self):
        return self.rows


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def query(self, *a):
        return _Query(self.rows)


def test_latest_post_dates_takes_the_day_and_skips_bad_values():
    db = _Db([
        ("a", "2026-11-02T16:37:45.000Z"),
        ("b", "garbage"),
        ("c", None),
    ])
    assert ccre._latest_post_dates(db, ["a", "b", "c", "d"]) == {"a": "2026-11-02"}


def test_latest_post_dates_empty_input_skips_query():
    assert ccre._latest_post_dates(None, []) == {}


def test_limits():
    assert ccre.POSTS_FIRST_SCRAPE == 40
    assert ccre.POSTS_REFRESH_MAX == 60
