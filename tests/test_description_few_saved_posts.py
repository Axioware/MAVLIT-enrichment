"""A username with only a few saved posts is scraped again; a full saved set is reused without Apify."""
import pytest
from types import SimpleNamespace

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


# --- a username that does not exist on Instagram is reported as such, in words the creator can act on ---------------------------------

def test_a_username_that_does_not_exist_is_reported_as_not_found(monkeypatch, stubs):
    monkeypatch.setattr(d, "_saved_items", lambda db, handle: [])
    for placeholder in ([], [{"error": "no_items", "errorDescription": "Empty or private data for provided input", "url": "https://www.instagram.com/x/"}]):
        monkeypatch.setattr(d, "_scrape_handle", lambda *a, p=placeholder: p)
        with pytest.raises(d.InstagramUsernameNotFound) as raised:
            d.generate_instagram_description(None, "vinylgoneviral1234", "Music")
        assert raised.value.public_message.startswith("Instagram username not found")
        assert isinstance(raised.value, RuntimeError)


def test_a_real_account_is_not_reported_as_missing(monkeypatch, stubs):
    monkeypatch.setattr(d, "_saved_items", lambda db, handle: [])
    monkeypatch.setattr(d, "_scrape_handle", lambda *a: [_item(i) for i in range(5)])
    assert d.generate_instagram_description(None, "realaccount", "Music")["posts_analyzed"] == 5


def test_the_other_failures_have_their_own_readable_messages(monkeypatch, stubs):
    monkeypatch.setattr(d, "_saved_items", lambda db, handle: [])
    monkeypatch.setattr(d, "_scrape_handle", lambda *a: None)          # the scraper itself failed
    with pytest.raises(d.DescriptionError) as raised:
        d.generate_instagram_description(None, "someone", "Music")
    assert "try again" in raised.value.public_message and not isinstance(raised.value, d.InstagramUsernameNotFound)
    monkeypatch.setattr(d, "_scrape_handle", lambda *a: [{"id": "p1", "caption": "", "metaData": {"biography": ""}}])   # posts, but no bio or captions
    with pytest.raises(d.DescriptionError) as raised:
        d.generate_instagram_description(None, "someone", "Music")
    assert "no bio or captions" in raised.value.public_message


def test_the_stored_error_is_the_readable_message_not_the_class_name():
    import api.app as api_app
    assert api_app._safe_error(d.InstagramUsernameNotFound()).startswith("Instagram username not found")
    assert api_app._safe_error(d.DescriptionError("Try again")) == "Try again"
    assert api_app._safe_error(ValueError("secret detail")) == "ValueError"   # anything else still shows only its type


@pytest.mark.parametrize("handle,valid", [("vinylgoneviral", True), ("a.b_c9", True), ("A" * 30, True), ("vinylgone virall", False), ("", False),
                                          ("a" * 31, False), ("bad-name", False), ("emoji😀", False), ("@x", False)])
def test_instagram_username_format(handle, valid):
    assert d.is_valid_instagram_username(handle) is valid


def test_the_generate_endpoint_refuses_a_username_with_a_space_before_anything_runs():
    from fastapi.testclient import TestClient
    import api.app as api_app
    from api.auth import get_current_user
    api_app.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    try:
        client = TestClient(api_app.app, raise_server_exceptions=False)
        for username in ("vinylgone virall", "bad-name", "x" * 40):
            response = client.post("/creator-profile/me/generate-description", json={"platform": "instagram", "username": username, "niche": "Music"})
            assert response.status_code == 422, username
            assert "no spaces" in response.json()["detail"]
    finally:
        api_app.app.dependency_overrides.pop(get_current_user, None)
