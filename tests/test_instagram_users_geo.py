"""enrich_instagram_users only picks posts of brands with a geo reach score of 0-40 (an explicit row_id is the exception). No database."""
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query, Session

from pipeline.enrichment import instagram_users as iu


def _sql(**kwargs) -> str:
    seen = []

    def fake_all(self):
        seen.append(self)
        return []

    db = Session(bind=create_engine("postgresql+psycopg2://nobody@localhost/none"))   # never connects: nothing is executed
    with mock.patch.object(iu, "APIFY_TOKEN", "token"), mock.patch.object(iu, "_require_post_collaborators_column"), \
            mock.patch.object(Query, "all", fake_all):
        assert iu.enrich_instagram_users(db, limit=1, **kwargs) == 0
    return str(seen[0].statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_only_brands_with_a_geo_reach_score_of_0_to_40_are_processed():
    for kwargs in ({}, {"brand_raw_id": 7}, {"beach_brands_only": True}):
        sql = _sql(**kwargs)
        assert "geo_reach_score BETWEEN 0 AND 40" in sql, kwargs
        assert "sponsorship_confidence >= 90" in sql


def test_an_explicit_row_is_still_processed_whatever_the_geo_score():
    sql = _sql(row_id=5)
    assert "geo_reach_score" not in sql and "instagram_posts.id = 5" in sql
