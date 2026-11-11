"""Every run of the Instagram scraper is recorded in apify_usage with an estimated cost (results x $2.30 per 1,000); other actors are not."""
import os
from unittest import mock

import pytest

from pipeline.helpers import apify as apify_module
from pipeline.helpers.apify import INSTAGRAM_ACTOR_ID, _handle_from_input, instagram_cost_usd, run_apify_actor


def test_the_cost_of_a_run_is_results_times_the_price_per_thousand():
    assert instagram_cost_usd(40) == pytest.approx(0.092)
    assert instagram_cost_usd(20) == pytest.approx(0.046)
    assert instagram_cost_usd(5) == pytest.approx(0.0115)
    assert instagram_cost_usd(1) == pytest.approx(0.0023)
    assert instagram_cost_usd(1000) == pytest.approx(2.30)
    assert instagram_cost_usd(0) == 0 and instagram_cost_usd(-3) == 0


def test_the_price_can_be_changed_and_a_bad_value_falls_back_to_the_default():
    with mock.patch.dict(os.environ, {"APIFY_INSTAGRAM_USD_PER_1000": "3"}):
        assert instagram_cost_usd(1000) == pytest.approx(3.0)
    for bad in ("cheap", "-1"):
        with mock.patch.dict(os.environ, {"APIFY_INSTAGRAM_USD_PER_1000": bad}):
            assert instagram_cost_usd(1000) == pytest.approx(2.30)


def test_the_handle_comes_from_the_profile_url_else_the_label():
    assert _handle_from_input({"directUrls": ["https://www.instagram.com/some.name/"]}, "IGUsers x") == "some.name"
    assert _handle_from_input({"directUrls": ["https://www.instagram.com/p/ABC123/"]}, "pitch posts (1)") == "pitch posts (1)"
    assert _handle_from_input({}, "") == "instagram"


class _FakeClient:
    items: list = []
    fail = False

    def __init__(self, token):
        pass

    def actor(self, actor_id):
        outer = self

        class Actor:
            def call(self, **kwargs):
                if outer.fail:
                    raise RuntimeError("actor crashed")
                return {"status": "SUCCEEDED", "defaultDatasetId": "d1"}

        return Actor()

    def dataset(self, dataset_id):
        outer = self

        class Dataset:
            def iterate_items(self):
                return iter(outer.items)

        return Dataset()


@pytest.fixture
def fake_apify(monkeypatch):
    _FakeClient.items, _FakeClient.fail = [], False
    monkeypatch.setattr(apify_module, "ApifyClient", _FakeClient)
    return _FakeClient


def test_an_instagram_run_is_recorded_with_its_result_count_and_cost(fake_apify):
    fake_apify.items = [{"id": str(i)} for i in range(40)]
    with mock.patch("pipeline.helpers.gpt_llm.record_apify_scrape") as record:
        items = run_apify_actor(INSTAGRAM_ACTOR_ID, {"directUrls": ["https://www.instagram.com/nike/"], "resultsLimit": 40}, label="Instagram @nike")
    assert len(items) == 40
    record.assert_called_once_with("instagram", "nike", 40, 40, actor_id=INSTAGRAM_ACTOR_ID, label="Instagram @nike")


def test_the_cost_counts_the_results_returned_not_the_ones_asked_for(fake_apify):
    fake_apify.items = [{"id": "a"}] * 12
    with mock.patch("pipeline.helpers.gpt_llm.record_apify_scrape") as record:
        run_apify_actor(INSTAGRAM_ACTOR_ID, {"directUrls": ["https://www.instagram.com/small/"], "resultsLimit": 40}, label="x")
    assert record.call_args.args[2:4] == (40, 12)   # asked for 40, returned 12


def test_other_actors_and_failed_runs_are_not_recorded(fake_apify):
    fake_apify.items = [{"x": 1}]
    with mock.patch("pipeline.helpers.gpt_llm.record_apify_scrape") as record:
        run_apify_actor("G3EZsXsaGaLDcc4uc", {"profiles": ["https://www.linkedin.com/in/x"]}, label="LinkedIn employment verify")   # the LinkedIn actor
        fake_apify.fail = True
        assert run_apify_actor(INSTAGRAM_ACTOR_ID, {"resultsLimit": 5}, label="x", require_success=True) is None
    record.assert_not_called()


def test_a_recording_problem_never_breaks_the_scrape(fake_apify):
    fake_apify.items = [{"id": "a"}]
    with mock.patch("pipeline.helpers.gpt_llm.record_apify_scrape", side_effect=RuntimeError("database down")):
        assert run_apify_actor(INSTAGRAM_ACTOR_ID, {"resultsLimit": 1}, label="x") == [{"id": "a"}]


def test_the_row_carries_the_user_and_feature_inside_a_scope_and_none_outside():
    from pipeline.helpers import gpt_llm
    added = []
    session = mock.MagicMock()
    session.__enter__.return_value = session
    session.add.side_effect = added.append
    with mock.patch("pipeline.db.SessionLocal", return_value=session):
        gpt_llm.record_apify_scrape("instagram", "script_run", 40, 40, actor_id=INSTAGRAM_ACTOR_ID, label="IGUsers @script_run")   # enrichment script
        with gpt_llm.llm_usage_scope(7, "description"):
            gpt_llm.record_apify_scrape("instagram", "mine", 20, 20, actor_id=INSTAGRAM_ACTOR_ID)                                  # a user's action
    script, user = added
    assert (script.creator_profile_id, script.feature) == (None, None)
    assert float(script.cost_usd) == pytest.approx(0.092) and script.actor_id == INSTAGRAM_ACTOR_ID and script.label == "IGUsers @script_run"
    assert (user.creator_profile_id, user.feature) == (7, "description") and float(user.cost_usd) == pytest.approx(0.046)
