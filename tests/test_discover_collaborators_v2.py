"""--v2: Facebook check + username LLM only; creators saved with an empty niche (no DB/Apify/LLM)."""
from types import SimpleNamespace

import pytest

from pipeline.enrichment_re import discover_collaborator_creators as d


def _summary():
    return {"usernames": 1, "brand_by_username": 0, "no_branded_content": 0,
            "brand_by_profile": 0, "saved": {}, "failed": 0, "rows_checked": 0, "failed_row_ids": set()}


@pytest.fixture
def calls(monkeypatch):
    c = {"saved_creator": [], "saved_brand": [], "scrape": 0, "niche_llm": 0}

    async def fake_check(username):
        return SimpleNamespace(result_count=3, instagram_id="1")

    monkeypatch.setattr(d, "check_branded_content", fake_check)
    monkeypatch.setattr(d, "_save_branded_content_check", lambda *a, **k: None)
    monkeypatch.setattr(d, "_save_creator", lambda db, u, n: c["saved_creator"].append((u, n)))
    monkeypatch.setattr(d, "_save_brand", lambda db, u: c["saved_brand"].append(u) or True)
    monkeypatch.setattr(d, "_scrape_posts", lambda *a, **k: c.__setitem__("scrape", c["scrape"] + 1) or [{"id": "1"}])
    monkeypatch.setattr(d, "_classify_niche", lambda *a, **k: c.__setitem__("niche_llm", c["niche_llm"] + 1) or ("creator", "Music"))
    return c


def test_v2_creator_saved_with_empty_niche_and_no_apify_or_niche_llm(monkeypatch, calls):
    monkeypatch.setattr(d, "_classify_username", lambda u: "creator")
    s = _summary()
    assert d._process_username(None, "bob", s, "t", v2=True) is True
    assert calls["saved_creator"] == [("bob", None)]
    assert calls["scrape"] == 0 and calls["niche_llm"] == 0
    assert s["saved"] == {d.NO_NICHE: 1}


def test_v2_brand_still_goes_to_brands_raw(monkeypatch, calls):
    monkeypatch.setattr(d, "_classify_username", lambda u: "brand")
    s = _summary()
    assert d._process_username(None, "acme", s, "t", v2=True) is True
    assert calls["saved_brand"] == ["acme"] and calls["saved_creator"] == []
    assert s["brand_by_username"] == 1


def test_v2_no_branded_content_stops_before_the_llm(monkeypatch, calls):
    async def none(username):
        return SimpleNamespace(result_count=0, instagram_id=None)
    monkeypatch.setattr(d, "check_branded_content", none)
    monkeypatch.setattr(d, "_classify_username", lambda u: pytest.fail("LLM must not run"))
    s = _summary()
    assert d._process_username(None, "x", s, "t", v2=True) is True
    assert s["no_branded_content"] == 1 and not calls["saved_creator"]


def test_default_flow_unchanged_scrapes_and_classifies_niche(monkeypatch, calls):
    monkeypatch.setattr(d, "_classify_username", lambda u: "creator")
    s = _summary()
    assert d._process_username(None, "bob", s, "t") is True
    assert calls["scrape"] == 1 and calls["niche_llm"] == 1
    assert calls["saved_creator"] == [("bob", "Music")]
