"""Feed > Market Insights: you against your five most similar creators of the same audience size, and your four best matched brands you have not
generated a pitch for. Needs MAVLIT_TEST_PG_URL for the database part."""
import math
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402


def vec(cosine: float) -> list[float]:
    v = [0.0] * 1024
    v[0], v[1] = cosine, math.sqrt(max(0.0, 1 - cosine * cosine))
    return v


class PeerAverages(unittest.TestCase):
    def test_each_number_is_the_average_over_the_peers_that_have_it(self):
        from pipeline.similar_creators import peer_averages
        peers = [
            {"followers": 1000, "engagement_rate": 4.0, "avg_reel_views": 2000, "posts_per_week": 2.0, "partnership_count": 3},
            {"followers": 2000, "engagement_rate": 6.0, "avg_reel_views": None, "posts_per_week": None, "partnership_count": 4},
            {"followers": None, "engagement_rate": None, "avg_reel_views": 4000, "posts_per_week": 3.0, "partnership_count": 0},
        ]
        self.assertEqual(peer_averages(peers), {"avg_followers": 1500, "avg_engagement": 5.0, "avg_reel_views": 3000, "avg_posts_per_week": 2.5, "avg_partnerships": 2.3})
        self.assertEqual(peer_averages([]), {"avg_followers": None, "avg_engagement": None, "avg_reel_views": None, "avg_posts_per_week": None, "avg_partnerships": None})


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class AgainstPostgres(unittest.TestCase):
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
        cls.Session = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_niches, test_creator_brand_partnership_posts, instagram_posts, instagram_users, creator_brand_llm_v3_rankings, pitches, brands_raw, creator_profiles RESTART IDENTITY CASCADE"))
            for i in range(1, 7):
                conn.execute(text("INSERT INTO brands_raw (id, name, niche, refferls, geo_reach_score) VALUES (:i, :n, 'Music', false, 10)"), {"i": i, "n": f"Brand{i}"})
            # seven similar creators, c1 the closest; c7 is a very close match but far bigger than the user (outside the audience range)
            for i, (similarity, followers, rate) in enumerate([(0.99, 800, 4.0), (0.95, 1200, 6.0), (0.92, 1000, 5.0), (0.90, 1500, 2.0), (0.88, 900, 3.0), (0.80, 1100, 9.0), (0.97, 50000, 1.0)], start=1):
                conn.execute(text("INSERT INTO instagram_users (id, username, user_type, followers_count, post_id) VALUES (:i, :u, 'mention', :f, :p)"), {"i": i, "u": f"c{i}", "f": followers, "p": f"p{i}"})
                conn.execute(text("INSERT INTO creator_niches (instagram_user_id, username, niche, description, embedding, engagement_rate) VALUES (:i, :u, 'Music', 'd', :e, :r)"),
                             {"i": i, "u": f"c{i}", "e": str(vec(similarity)), "r": rate})
                conn.execute(text("""INSERT INTO test_creator_brand_partnership_posts (brand_raw_id, brand_name, creator_username, post_url, sponsorship_confidence)
                                     VALUES (:b, 'x', :u, :url, 95)"""), {"b": min(i, 6), "u": f"c{i}", "url": f"https://www.instagram.com/p/post{i}/"})
            conn.execute(text("""INSERT INTO creator_profiles (id, email, instagram_handle, instagram_followers, instagram_primary_niche, instagram_description, instagram_engagement_rate)
                                 VALUES (1, 'u@example.test', 'me', 1000, 'Music', 'A music creator', 4.2)"""))
            conn.execute(text("INSERT INTO creator_brand_llm_v3_rankings (creator_profile_id, brand_raw_id, confidence, rank) SELECT 1, g, 95 - g, g FROM generate_series(1, 6) g"))
            # a generated pitch for Brand1 and a deal the user only logged by hand for Brand2
            conn.execute(text("""INSERT INTO pitches (creator_profile_id, brand_raw_id, brand_name, story, status, is_manual) VALUES
                (1, 1, 'Brand1', 's', 'generated', false), (1, 2, 'Brand2', 's', 'generated', true)"""))

    def client(self):
        from fastapi.testclient import TestClient
        import api.app as api_app
        import api.feed_api as feed_api
        from api.auth import get_current_user
        from pipeline.db import CreatorProfile, get_db

        def override_db():
            with self.Session() as session:
                yield session

        def current():
            with self.Session() as session:
                return session.get(CreatorProfile, 1)
        api_app.app.dependency_overrides[get_db] = override_db
        api_app.app.dependency_overrides[get_current_user] = current
        self.addCleanup(api_app.app.dependency_overrides.clear)
        patcher = mock.patch.object(feed_api, "user_query_embedding", return_value=vec(1.0))
        patcher.start()
        self.addCleanup(patcher.stop)
        return TestClient(api_app.app, raise_server_exceptions=False)

    def test_the_peers_are_the_five_most_similar_creators_of_the_same_audience_size(self):
        body = self.client().get("/feed/market-insights").json()
        self.assertTrue(body["ready"])
        self.assertEqual(body["audience_range"], {"min": 334, "max": 2000})
        peers = body["peers"]
        # c7 (97%) is too big; c6 (80%) is the sixth and is left out
        self.assertEqual([c["username"] for c in peers["creators"]], ["c1", "c2", "c3", "c4", "c5"])
        self.assertEqual(peers["count"], 5)
        self.assertEqual(peers["avg_followers"], 1080)       # (800 + 1200 + 1000 + 1500 + 900) / 5
        self.assertEqual(peers["avg_engagement"], 4.0)       # (4 + 6 + 5 + 2 + 3) / 5
        self.assertEqual(peers["avg_partnerships"], 1.0)

    def test_you_are_returned_with_your_own_numbers(self):
        you = self.client().get("/feed/market-insights").json()["you"]
        self.assertEqual((you["handle"], you["followers"], you["engagement_rate"]), ("me", 1000, 4.2))

    def test_missed_brands_are_the_best_four_matches_without_a_generated_pitch(self):
        missed = self.client().get("/feed/market-insights").json()["missed_brands"]
        # Brand1 has a generated pitch (left out); Brand2's deal was logged by hand (kept); ranks 2 to 5 are the top four left
        self.assertEqual([b["brand_name"] for b in missed], ["Brand2", "Brand3", "Brand4", "Brand5"])
        self.assertEqual(missed[0]["confidence"], 93)
        self.assertEqual(missed[0]["partnerships"], 1)        # the brand's partnership posts scored 90 or more

    def test_nothing_is_returned_for_peers_without_a_follower_count_or_a_finished_profile(self):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET instagram_followers = NULL"))
        body = self.client().get("/feed/market-insights").json()
        self.assertEqual((body["peers"]["count"], body["peers"]["avg_followers"]), (0, None))     # the audience filter needs a follower count
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET instagram_followers = 1000, instagram_primary_niche = NULL, instagram_description = NULL"))
        body = self.client().get("/feed/market-insights").json()
        self.assertFalse(body["ready"])
        self.assertEqual(body["peers"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
