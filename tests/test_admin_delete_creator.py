"""Admin > Creator Profiles > Delete removes the creator and everything of theirs, and nothing of anyone else's.

Needs MAVLIT_TEST_PG_URL (a disposable LOCAL database whose name contains 'test' or 'scratch'); skipped otherwise.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402


class FlagsTest(unittest.TestCase):
    def test_the_admin_view_allows_delete(self):
        from api import app as api_app
        self.assertTrue(api_app.CreatorProfileAdmin.can_delete)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class DeleteAgainstPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        host = url.query.get("host") or url.host or ""
        if not ((host in ("", "localhost", "127.0.0.1") or str(host).startswith("/")) and any(w in (url.database or "") for w in ("test", "scratch"))):
            raise RuntimeError("MAVLIT_TEST_PG_URL must be a LOCAL database whose name contains 'test' or 'scratch'; refusing to run")
        from pipeline import migrations
        cls.engine = create_engine(TEST_URL, poolclass=NullPool)
        with cls.engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        migrations.run_migrations(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def seed(self, session, email):
        from pipeline.db import (ContractReview, CreatorBrandLlmRanking, CreatorBrandLlmV3Ranking, CreatorInstagramPost, CreatorProfile,
                                 LlmUsage, OwnBrandJob, Pitch, RateEstimate, SavedBrand)
        creator = CreatorProfile(email=email, password_hash="x", is_active=True)
        session.add(creator)
        session.flush()
        cid = creator.id
        brand = self.brand
        session.add_all([
            Pitch(creator_profile_id=cid, brand_name="B", story="s"),
            RateEstimate(creator_profile_id=cid, platform="instagram", deliverable_type="reel"),
            ContractReview(creator_profile_id=cid, contract_text="c"),
            SavedBrand(creator_profile_id=cid, brand_raw_id=brand),
            CreatorBrandLlmRanking(creator_profile_id=cid, brand_raw_id=brand, confidence=1, rank=1),
            CreatorBrandLlmV3Ranking(creator_profile_id=cid, brand_raw_id=brand, confidence=1, rank=1),
            CreatorInstagramPost(creator_profile_id=cid, instagram_handle="h", post_id=f"p{cid}"),
            OwnBrandJob(id=f"job{cid}", creator_profile_id=cid, instagram_handle="h"),
            LlmUsage(model="m", endpoint="e", prompt_tokens=0, cached_tokens=0, completion_tokens=0, reasoning_tokens=0, creator_profile_id=cid),
        ])
        session.flush()
        return cid

    def test_deletes_the_creator_and_their_data_only(self):
        from api import app as api_app
        from pipeline.db import (BrandRaw, ContractReview, CreatorBrandLlmRanking, CreatorBrandLlmV3Ranking, CreatorInstagramPost,
                                 CreatorProfile, LlmUsage, OwnBrandJob, Pitch, RateEstimate, SavedBrand)
        maker = sessionmaker(self.engine)
        with maker() as s:
            s.execute(text("TRUNCATE creator_profiles, brands_raw, llm_usage RESTART IDENTITY CASCADE"))
            b = BrandRaw(name="Brand")
            s.add(b)
            s.flush()
            self.brand = b.id
            gone, kept = self.seed(s, "gone@example.test"), self.seed(s, "kept@example.test")
            s.commit()

        view = next(v for v in api_app.admin.views if isinstance(v, api_app.CreatorProfileAdmin))
        with mock.patch.object(type(view), "session_maker", maker, create=True), mock.patch.object(view, "session_maker", maker, create=True):
            view._delete_creator(str(gone))
            view._delete_creator("999999")   # unknown id: nothing happens

        with maker() as s:
            for model in (Pitch, RateEstimate, ContractReview, SavedBrand, CreatorBrandLlmRanking, CreatorBrandLlmV3Ranking,
                          CreatorInstagramPost, OwnBrandJob):
                self.assertEqual(s.scalar(select(func.count()).select_from(model).where(model.creator_profile_id == gone)), 0, model.__name__)
                self.assertEqual(s.scalar(select(func.count()).select_from(model).where(model.creator_profile_id == kept)), 1, model.__name__)
            self.assertIsNone(s.get(CreatorProfile, gone))
            self.assertIsNotNone(s.get(CreatorProfile, kept))
            # cost rows have no foreign key and stay for reporting
            self.assertEqual(s.scalar(select(func.count()).select_from(LlmUsage).where(LlmUsage.creator_profile_id == gone)), 1)


if __name__ == "__main__":
    unittest.main()
