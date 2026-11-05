"""No DB transaction may stay open across the Apify scrape (SessionLocal kills idle transactions at 60 s)."""
from types import SimpleNamespace

from pipeline.enrichment_re import brand_instagram_profile as b


class _Query:
    def __init__(self, rows): self.rows = rows
    def filter(self, *a): return self
    def limit(self, n): return self
    def all(self): return self.rows


class _Db:
    def __init__(self, rows): self.rows = rows; self.events = []
    def query(self, *a): return _Query(self.rows)
    def commit(self): self.events.append("commit")


def test_commits_before_each_scrape(monkeypatch):
    brand = SimpleNamespace(instagram_handle="https://www.instagram.com/acme", name=None, website=None, instagram_profile_checked=False)
    db = _Db([brand])

    def fake_scrape(handle):
        db.events.append("scrape")
        return None   # scrape failed -> loop continues, no further DB work

    monkeypatch.setattr(b, "_scrape_profile", fake_scrape)
    b.enrich_brand_instagram_profile(db, limit=5)
    assert db.events.index("commit") < db.events.index("scrape")
    assert db.events[db.events.index("scrape") - 1] == "commit"   # the commit is right before the scrape
