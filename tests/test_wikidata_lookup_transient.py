"""A Wikidata timeout must not mark brands as checked (no DB, no network)."""
import httpx
import pytest

from pipeline.enrichment_re import brand_wikidata_lookup as w


def test_transient_classification():
    req = httpx.Request("GET", "https://x")
    assert w._is_transient(httpx.ReadTimeout("t"))
    assert w._is_transient(httpx.ConnectError("c"))
    assert w._is_transient(httpx.HTTPStatusError("e", request=req, response=httpx.Response(503, request=req)))
    assert w._is_transient(httpx.HTTPStatusError("e", request=req, response=httpx.Response(429, request=req)))
    assert not w._is_transient(httpx.HTTPStatusError("e", request=req, response=httpx.Response(400, request=req)))
    assert not w._is_transient(ValueError("bad"))


def test_timeout_is_retried_then_raised(monkeypatch):
    calls = []
    def boom(*a, **k):
        calls.append(1); raise httpx.ReadTimeout("t")
    monkeypatch.setattr(w.httpx, "get", boom)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    with pytest.raises(httpx.ReadTimeout):
        w._fetch_by_instagram_handles(["a"])
    assert len(calls) == 3


def test_timeout_leaves_brands_unchecked(monkeypatch):
    class Brand:
        instagram_handle = "https://www.instagram.com/acme"
        instagram_wikidata_checked = False
    brand = Brand()

    class Q:
        def filter(self, *a): return self
        def limit(self, n): return self
        def all(self): return [brand]

    class Db:
        def query(self, *a): return Q()
        def commit(self): pass

    monkeypatch.setattr(w, "_fetch_by_instagram_handles", lambda b: (_ for _ in ()).throw(httpx.ReadTimeout("t")))
    w.enrich_brand_wikidata_lookup(Db(), limit=5)
    assert brand.instagram_wikidata_checked is False


def test_other_errors_still_mark_skipped(monkeypatch):
    class Brand:
        instagram_handle = "https://www.instagram.com/acme"
        instagram_wikidata_checked = False
    brand = Brand()

    class Q:
        def filter(self, *a): return self
        def limit(self, n): return self
        def all(self): return [brand]

    class Db:
        def query(self, *a): return Q()
        def commit(self): pass

    monkeypatch.setattr(w, "_fetch_by_instagram_handles", lambda b: (_ for _ in ()).throw(ValueError("bad")))
    w.enrich_brand_wikidata_lookup(Db(), limit=5)
    assert brand.instagram_wikidata_checked is True
