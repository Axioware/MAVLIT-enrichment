"""A homepage redirected to /de/ whose US version is a bare /us/ must not crash the locale swap."""
from types import SimpleNamespace

from pipeline.enrichment.geo_reach import geo_reach as g


def test_bare_us_segment_swap(monkeypatch):
    pages = {
        "https://www.zwilling.com": "https://www.zwilling.com/de/",
        "https://www.zwilling.com/us/": "https://www.zwilling.com/us/",
        "https://www.zwilling.com/us/shipping": "https://www.zwilling.com/us/shipping",
    }
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        if url == "https://www.zwilling.com/en-us/":
            return None, None            # 410 Gone
        landed = pages.get(url, url)
        return "<html><body>ship to US " + "x" * 400 + "</body></html>", landed

    monkeypatch.setattr(g, "_fetch_page_with_url", fake_fetch)
    monkeypatch.setattr(g, "is_link_page", lambda *a, **k: False)
    monkeypatch.setattr(g, "_discover_location_links", lambda html, origin: [(5, "https://www.zwilling.com/de/shipping")])
    monkeypatch.setattr(g, "_call_geo_llm_page", lambda *a, **k: {"locations_found": ["US"], "confidence": 95, "score": 20, "country_codes": ["US"]})
    monkeypatch.setattr(g, "_call_geo_llm_final", lambda *a, **k: {})

    brand = SimpleNamespace(id=1, name="Zwilling", instagram_handle="zwilling", website="https://www.zwilling.com")
    score, *_ = g._crawl_and_score(brand, "")
    assert score == 20
    assert "https://www.zwilling.com/us/shipping" in fetched or "https://www.zwilling.com/us/" in fetched
