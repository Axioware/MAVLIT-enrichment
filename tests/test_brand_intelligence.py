"""Feed > Brand Intelligence: the matched brands with what their partnership posts (scored >= 90, instagram_posts or the test table) say.
Needs MAVLIT_TEST_PG_URL for the database part."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402


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
            conn.execute(text("TRUNCATE creator_niches, test_creator_brand_partnership_posts, instagram_posts, instagram_users, creator_brand_llm_v3_rankings, brands_raw, creator_profiles RESTART IDENTITY CASCADE"))
            conn.execute(text("INSERT INTO brands_raw (id, name, niche, refferls, geo_reach_score) VALUES (1, 'Acme', 'Music', false, 10), (2, 'Globex', 'Music', false, 10), (3, 'Initech', 'Music', false, 10)"))
            conn.execute(text("""INSERT INTO instagram_posts (brand_raw_id, instagram_handle, post_id, post_url, post_type, sponsorship_confidence) VALUES
                (1, 'acme', 'pa', 'https://www.instagram.com/reel/AAA/', 'Video', 95), (1, 'acme', 'pb', 'https://www.instagram.com/p/BBB/', 'Sidecar', 92),
                (1, 'acme', 'pc', 'https://www.instagram.com/p/CCC/', 'Image', 70), (1, 'acme', 'pd', 'https://www.instagram.com/p/DDD/', 'Image', 91),
                (2, 'globex', 'pg', 'https://www.instagram.com/p/GGG/', NULL, 89)"""))
            conn.execute(text("""INSERT INTO instagram_users (username, user_type, post_id, followers_count, likes_count, comments_count) VALUES
                ('Alice', 'mention', 'pa', 1000, 100, 10), ('bob', 'mention', 'pb', 50000, 500, 50), ('dave', 'commenter', 'pd', 5, 1, 1),
                ('carol', 'contentcreatorRE', 'cz', 20000, 200, 20)"""))
            conn.execute(text("""INSERT INTO test_creator_brand_partnership_posts (brand_raw_id, brand_name, creator_username, post_url, sponsorship_confidence) VALUES
                (1, 'Acme', 'carol', 'https://www.instagram.com/reel/EEE/', 93), (1, 'Acme', 'carol', 'https://www.instagram.com/p/DDD', 91),
                (1, 'Acme', 'carol', 'https://www.instagram.com/p/FFF/', 80), (2, 'Globex', 'erin', 'https://www.instagram.com/p/HHH/', 99)"""))

    def stats(self, ids):
        from pipeline.brand_intelligence import brand_stats
        with self.Session() as db:
            return brand_stats(db, ids)

    def test_partnership_posts_are_counted_once_across_both_tables_at_90_or_more(self):
        acme = self.stats([1])[1]
        # reel AAA, BBB, DDD (in both tables, the second time without its trailing slash) and reel EEE: four posts; CCC (70) and FFF (80) are too weak
        self.assertEqual(acme["partnerships"], 4)

    def test_typical_creator_size_is_the_smallest_to_the_biggest_creator(self):
        acme = self.stats([1])[1]
        self.assertEqual((acme["creators"], acme["min_followers"], acme["max_followers"]), (3, 1000, 50000))   # alice, bob, carol; dave only commented

    def test_formats_are_the_share_of_the_sponsored_posts_that_are_a_reel_a_carousel_and_so_on(self):
        acme = self.stats([1])[1]
        self.assertEqual(acme["formats"], [{"name": "Reel", "pct": 50}, {"name": "Carousel", "pct": 25}, {"name": "Photo", "pct": 25}])

    def test_the_average_engagement_is_the_average_of_the_creators_rates(self):
        acme = self.stats([1])[1]
        self.assertEqual(acme["avg_engagement"], 4.4)   # alice (100 + 10) / 1000 = 11.0, bob (500 + 50) / 50000 = 1.1, carol (200 + 20) / 20000 = 1.1
        with self.engine.begin() as conn:                 # a rate saved in creator_niches wins over the one worked out from the posts
            conn.execute(text("INSERT INTO instagram_users (id, username, user_type, followers_count) VALUES (900, 'carol', 'contentcreatorRE', 20000)"))
            conn.execute(text("INSERT INTO creator_niches (instagram_user_id, username, niche, engagement_rate) VALUES (900, 'carol', 'Music', 4.4)"))
        self.assertEqual(self.stats([1])[1]["avg_engagement"], 5.5)   # (11.0 + 1.1 + 4.4) / 3

    def test_a_brand_with_nothing_has_zeros_and_no_numbers(self):
        stats = self.stats([2, 3])
        self.assertEqual(stats[3], {"partnerships": 0, "creators": 0, "min_followers": None, "max_followers": None, "formats": [], "avg_engagement": None})
        # Globex: one post at 99 from a creator we know nothing about (the 89 one does not count)
        self.assertEqual((stats[2]["partnerships"], stats[2]["creators"], stats[2]["min_followers"], stats[2]["avg_engagement"]), (1, 1, None, None))
        self.assertEqual(stats[2]["formats"], [{"name": "Post", "pct": 100}])
        self.assertEqual(self.stats([]), {})

    def test_the_unique_creators_of_a_brand_most_posts_first(self):
        from pipeline.brand_intelligence import brand_creators
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO instagram_users (id, username, user_type, followers_count, full_name) VALUES (901, 'Alice', 'mention', 1200, 'Alice A')"))
            conn.execute(text("INSERT INTO creator_niches (instagram_user_id, username, niche) VALUES (901, 'Alice', 'Beauty')"))
        with self.Session() as db:
            rows = brand_creators(db, 1)
        self.assertEqual([r["username"] for r in rows], ["carol", "bob", "alice"])   # carol made 2 of the posts; then by followers
        carol, bob, alice = rows
        self.assertEqual((carol["partnership_posts"], carol["followers"], carol["engagement_rate"]), (2, 20000, 1.1))
        self.assertEqual((bob["partnership_posts"], bob["followers"], bob["engagement_rate"]), (1, 50000, 1.1))
        self.assertEqual((alice["followers"], alice["niche"], alice["full_name"], alice["profile_url"]), (1200, "Beauty", "Alice A", "https://www.instagram.com/alice"))
        with self.Session() as db:
            self.assertEqual(brand_creators(db, 3), [])                                  # a brand with no partnership posts
            self.assertEqual(len(brand_creators(db, 1, limit=2)), 2)

    def test_every_unique_creator_is_listed_not_just_the_first_fifty(self):
        from pipeline.brand_intelligence import brand_creators
        with self.engine.begin() as conn:
            conn.execute(text("""INSERT INTO test_creator_brand_partnership_posts (brand_raw_id, brand_name, creator_username, post_url, sponsorship_confidence)
                SELECT 3, 'Initech', 'creator' || g, 'https://www.instagram.com/p/post' || g || '/', 95 FROM generate_series(1, 120) g"""))
            conn.execute(text("""INSERT INTO test_creator_brand_partnership_posts (brand_raw_id, brand_name, creator_username, post_url, sponsorship_confidence)
                VALUES (3, 'Initech', 'Creator7', 'https://www.instagram.com/p/another7/', 95)"""))   # the same creator again, in another case: still one creator
        with self.Session() as db:
            rows = brand_creators(db, 3)
        self.assertEqual(len(rows), 120)
        self.assertEqual(rows[0]["username"], "creator7")      # the one with 2 posts first
        self.assertEqual(rows[0]["partnership_posts"], 2)
        self.assertEqual(len({r["username"] for r in rows}), 120)

    def test_the_endpoint_lists_the_matched_brands_best_first_with_the_numbers(self):
        from fastapi.testclient import TestClient
        import api.app as api_app
        from api.auth import get_current_user
        from pipeline.db import CreatorProfile, get_db
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO creator_profiles (id, email) VALUES (1, 'u@example.test')"))
            conn.execute(text("INSERT INTO creator_brand_llm_v3_rankings (creator_profile_id, brand_raw_id, confidence, rank) VALUES (1, 1, 91, 1), (1, 3, 70, 2), (1, 2, 80, 3)"))

        def override_db():
            with self.Session() as session:
                yield session
        api_app.app.dependency_overrides[get_db] = override_db
        api_app.app.dependency_overrides[get_current_user] = lambda: CreatorProfile(id=1, email="u@example.test")
        self.addCleanup(api_app.app.dependency_overrides.clear)
        client = TestClient(api_app.app, raise_server_exceptions=False)
        body = client.get("/feed/brand-intelligence").json()
        # the rank order of the Brand Matches page; Initech (rank 2) is not shown there either: a matched brand needs a partnership post scored 90 or more
        self.assertEqual([b["brand_name"] for b in body["brands"]], ["Acme", "Globex"])
        acme = body["brands"][0]
        self.assertEqual((acme["confidence"], acme["partnerships"], acme["min_followers"], acme["max_followers"], acme["avg_engagement"]), (91, 4, 1000, 50000, 4.4))
        self.assertEqual(client.get("/feed/brand-intelligence?limit=1").json()["brands"][0]["brand_name"], "Acme")
        self.assertEqual(len(client.get("/feed/brand-intelligence?limit=1").json()["brands"]), 1)
        self.assertEqual(client.get("/feed/brand-intelligence?limit=0").status_code, 422)
        creators = client.get("/feed/brand-intelligence/1/creators").json()
        self.assertEqual((creators["brand_name"], [c["username"] for c in creators["creators"]]), ("Acme", ["carol", "bob", "alice"]))
        self.assertEqual(client.get("/feed/brand-intelligence/999/creators").status_code, 404)


if __name__ == "__main__":
    unittest.main()
