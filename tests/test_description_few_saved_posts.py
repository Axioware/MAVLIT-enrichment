"""A username with only a few saved posts is scraped again; a full saved set is reused without Apify."""
import pytest

from pipeline.creator_content import instagram_description as d


def _item(i):
    return {"id": f"p{i}", "caption": f"caption {i}", "metaData": {"biography": "bio", "followersCount": 10}}


@pytest.fixture
def stubs(monkeypatch):
    calls = {"scrape": 0}
    monkeypatch.setattr(d, "save_creator_posts", lambda *a, **k: 0)
    monkeypatch.setattr(d, "record_apify_scrape", lambda *a, **k: None)
    monkeypatch.setattr(d, "sub_niche_options", lambda *a, **k: [])
    monkeypatch.setattr(d, "_get_description_prompt", lambda db: "{niche}{bio}{captions}{sub_niche_options}{followers}")
    monkeypatch.setattr(d, "call_gpt_json", lambda *a, **k: {"description": "ok"})
    monkeypatch.setattr(d, "sub_niches_from_result", lambda *a, **k: [])
    monkeypatch.setattr(d, "audience_from_result", lambda *a, **k: {})
    return calls


def test_few_saved_posts_trigger_a_scrape(monkeypatch, stubs):
    monkeypatch.setattr(d, "_saved_items", lambda db, handle: [_item(0)])
    def scrape(handle, n):
        stubs["scrape"] += 1
        return [_item(i) for i in range(20)]
    monkeypatch.setattr(d, "_scrape_handle", scrape)
    result = d.generate_instagram_description(None, "vinylgoneviral", "Music")
    assert stubs["scrape"] == 1 and result["posts_analyzed"] == 20


def test_full_saved_set_skips_apify(monkeypatch, stubs):
    monkeypatch.setattr(d, "_saved_items", lambda db, handle: [_item(i) for i in range(20)])
    monkeypatch.setattr(d, "_scrape_handle", lambda *a: pytest.fail("Apify must not run"))
    assert d.generate_instagram_description(None, "vinylgoneviral", "Music")["posts_analyzed"] == 20


def test_failed_scrape_falls_back_to_the_saved_posts(monkeypatch, stubs):
    monkeypatch.setattr(d, "_saved_items", lambda db, handle: [_item(0)])
    monkeypatch.setattr(d, "_scrape_handle", lambda *a: None)
    assert d.generate_instagram_description(None, "vinylgoneviral", "Music")["posts_analyzed"] == 1


def test_failed_scrape_with_nothing_saved_raises(monkeypatch, stubs):
    monkeypatch.setattr(d, "_saved_items", lambda db, handle: [])
    monkeypatch.setattr(d, "_scrape_handle", lambda *a: None)
    with pytest.raises(RuntimeError):
        d.generate_instagram_description(None, "vinylgoneviral", "Music")
