"""A user adds a competitor creator to compare with: an account MAVLIT holds in instagram_users is worked out from those rows, any other gets its latest
20 posts read with Apify; partnerships, brands_raw rows and the content mix come with it. Needs MAVLIT_TEST_PG_URL for the database part."""
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

from pipeline import competitor_creators as cc  # noqa: E402


class Numbers(unittest.TestCase):
    def test_a_competitors_numbers_from_its_posts(self):
        posts = [
            {"likes": 100, "comments": 10, "views": 5000, "timestamp": "2026-05-10T10:00:00.000Z"},
            {"likes": 300, "comments": 30, "views": None, "timestamp": "2026-05-08T10:00:00.000Z"},
            {"likes": -1, "comments": -1, "views": 0, "timestamp": "2026-01-01T10:00:00.000Z"},   # hidden counts: left out; an old post
        ]
        out = cc.summarize_posts(posts, 2000)
        self.assertEqual((out["avg_likes"], out["avg_comments"]), (200, 20))
        self.assertEqual(out["engagement_rate"], 11.0)          # (200 + 20) / 2000 * 100
        self.assertEqual(out["avg_reel_views"], 5000)
        self.assertEqual(out["posts_per_week"], 1.0)             # 2 of the 3 posts are in the 14 days before the newest
        empty = cc.summarize_posts([], None)
        self.assertEqual(empty, {"avg_likes": None, "avg_comments": None, "engagement_rate": None, "avg_reel_views": None, "posts_per_week": None})

    def test_the_partnership_brands_most_posts_first(self):
        posts = [{"brand_raw_id": 2, "brand_handle": "adidas", "brand_name": None}, {"brand_raw_id": 1, "brand_handle": "nike", "brand_name": "Nike"},
                 {"brand_raw_id": 2, "brand_handle": "adidas", "brand_name": None}, {"brand_raw_id": None}]
        self.assertEqual(cc.partnership_brands(posts), [{"brand_raw_id": 2, "handle": "adidas", "name": None, "posts": 2}, {"brand_raw_id": 1, "handle": "nike", "name": "Nike", "posts": 1}])
        self.assertEqual(cc.partnership_brands([]), [])

    def test_which_competitors_need_a_job(self):
        from datetime import datetime, timedelta, timezone
        make = lambda status, age=0: mock.Mock(status=status, updated_at=datetime.now(timezone.utc) - timedelta(minutes=age), created_at=None)   # noqa: E731
        self.assertTrue(cc.needs_job(None))
        self.assertTrue(cc.needs_job(make("failed")))
        self.assertTrue(cc.needs_job(make("running", age=30)))      # stuck
        self.assertFalse(cc.needs_job(make("running", age=1)))
        self.assertFalse(cc.needs_job(make("queued", age=1)))
        self.assertFalse(cc.needs_job(make("ready", age=9999)))


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
            conn.execute(text("TRUNCATE competitor_creator_posts, creator_competitors, competitor_creators, feature_usage, feature_locks, creator_niches, test_creator_brand_partnership_posts, instagram_posts, instagram_users, brands_raw, creator_profiles RESTART IDENTITY CASCADE"))
            conn.execute(text("INSERT INTO creator_profiles (id, email) VALUES (1, 'u@example.test'), (2, 'v@example.test')"))
        patchers = [mock.patch.object(cc, "BatchSessionLocal", self.Session), mock.patch.object(cc, "APIFY_TOKEN", "x"), mock.patch.object(cc, "OPENAI_KEY", "x")]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def competitor(self, handle):
        with self.Session() as db:
            return db.query(cc.CompetitorCreator).filter_by(instagram_handle=handle).first()

    def add(self, handle, user=1):
        with self.Session() as db:
            comp, runs = cc.start_competitor(db, user, handle)
            return comp.id, runs

    # --- a creator MAVLIT already holds -------------------------------------------------------------------------------------------------------
    def seed_known_creator(self):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO brands_raw (id, name, instagram_handle) VALUES (1, 'Acme', 'https://www.instagram.com/acme')"))
            conn.execute(text("""INSERT INTO instagram_posts (brand_raw_id, instagram_handle, post_id, post_url, sponsorship_confidence) VALUES
                (1, 'acme', 'pa', 'https://www.instagram.com/p/AAA/', 95), (1, 'acme', 'pb', 'https://www.instagram.com/p/BBB/', 70)"""))
            conn.execute(text("""INSERT INTO instagram_users (username, user_type, post_id, full_name, bio, caption, followers_count, likes_count, comments_count, post_timestamp, video_view_count) VALUES
                ('Alice', 'mention', 'pa', 'Alice A', 'skincare', 'Skincare routine', 1000, 100, 10, '2026-05-10T10:00:00.000Z', 4000),
                ('alice', 'mention', 'pb', NULL, NULL, 'Another caption', 1000, 300, 30, '2026-05-08T10:00:00.000Z', NULL)"""))
            conn.execute(text("INSERT INTO test_creator_brand_partnership_posts (brand_raw_id, brand_name, creator_username, post_url, sponsorship_confidence) VALUES (1, 'Acme', 'alice', 'https://www.instagram.com/p/CCC/', 92)"))

    def test_an_account_in_instagram_users_is_worked_out_from_those_rows_without_apify(self):
        self.seed_known_creator()
        comp_id, runs = self.add("@Alice")
        self.assertTrue(runs)
        mix = {"product_focused_pct": 30, "content_categories": {"Tutorial": 70, "Lifestyle": 30}}
        with mock.patch("pipeline.enrichment.instagram_users._scrape_posts", side_effect=AssertionError("no Apify run for a known creator")), \
                mock.patch.object(cc, "call_gpt_json", return_value=mix) as llm:
            cc.run_competitor_job(comp_id, 1)
        comp = self.competitor("alice")
        self.assertEqual((comp.status, comp.source, comp.full_name, comp.followers_count, comp.posts_analyzed), ("ready", "instagram_users", "Alice A", 1000, 2))
        self.assertEqual((comp.avg_likes, comp.avg_comments, comp.engagement_rate, comp.avg_reel_views, comp.posts_per_week), (200, 20, 22.0, 4000, 1.0))
        self.assertEqual(comp.partnership_count, 2)       # the scored >= 90 post of instagram_posts and the one of the test table (the 70 is too weak)
        self.assertEqual(comp.partnership_brands, [{"brand_raw_id": 1, "handle": "acme", "name": "Acme", "posts": 2}])
        self.assertEqual((comp.product_focused_pct, comp.content_categories), (30, [{"name": "Tutorial", "pct": 70}, {"name": "Lifestyle", "pct": 30}]))
        self.assertEqual(llm.call_count, 1)               # the mix came from the creators' LLM prompt, from the captions of those rows
        self.assertIn("Skincare routine", llm.call_args[0][0])

    def test_a_mix_already_estimated_for_the_creator_is_used_and_the_stored_rate_wins(self):
        self.seed_known_creator()
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO instagram_users (id, username, user_type) VALUES (900, 'alice', 'mention')"))
            conn.execute(text("""INSERT INTO creator_niches (instagram_user_id, username, niche, engagement_rate, product_focused_pct, content_categories)
                                 VALUES (900, 'alice', 'Beauty', 7.5, 55, CAST('[{"name": "Product review", "pct": 100}]' AS jsonb))"""))
        comp_id, _ = self.add("alice")
        with mock.patch.object(cc, "call_gpt_json", side_effect=AssertionError("the mix exists: no LLM call")):
            cc.run_competitor_job(comp_id, 1)
        comp = self.competitor("alice")
        self.assertEqual((comp.engagement_rate, comp.product_focused_pct, comp.content_categories), (7.5, 55, [{"name": "Product review", "pct": 100}]))

    # --- a creator MAVLIT does not hold ---------------------------------------------------------------------------------------------------------
    def apify_posts(self):
        meta = {"fullName": "New Creator", "followersCount": 5000, "biography": "gear reviews"}
        return [
            {"id": "1", "shortCode": "AAA", "type": "Video", "timestamp": "2026-06-10T10:00:00.000Z", "caption": "Sponsored by @nike", "likesCount": 400, "commentsCount": 40,
             "videoViewCount": 20000, "mentions": ["nike"], "paidPartnership": True, "metaData": meta},
            {"id": "2", "shortCode": "BBB", "type": "Image", "timestamp": "2026-06-08T10:00:00.000Z", "caption": "Just a photo", "likesCount": 200, "commentsCount": 10, "metaData": meta},
            {"id": "3", "shortCode": "CCC", "type": "Video", "timestamp": "2026-03-01T10:00:00.000Z", "caption": "Old reel", "likesCount": -1, "commentsCount": -1, "videoPlayCount": 8000, "metaData": meta},
        ]

    def run_apify_job(self, posts, brand_for_post="nike"):
        comp_id, runs = self.add("newcreator")
        self.assertTrue(runs)

        def check(db, username, full_name, item):
            return [{"username": brand_for_post, "has_referral_code": False, "referral_code": None}] if item["id"] == "1" and brand_for_post else []
        mix = {"product_focused_pct": 40, "content_categories": {"Product review": 50, "Lifestyle": 50}}
        with mock.patch("pipeline.enrichment.instagram_users._scrape_posts", return_value=posts) as scrape, \
                mock.patch("pipeline.enrichment_re.content_creator_re._check_post_for_brands", side_effect=check), \
                mock.patch.object(cc, "call_gpt_json", return_value=mix):
            cc.run_competitor_job(comp_id, 1)
        return comp_id, scrape

    def test_an_unknown_account_has_its_latest_20_posts_read_saved_and_analysed(self):
        comp_id, scrape = self.run_apify_job(self.apify_posts())
        self.assertEqual(scrape.call_args[0][:2], ("newcreator", 20))                  # the latest 20 posts
        comp = self.competitor("newcreator")
        self.assertEqual((comp.status, comp.source, comp.full_name, comp.followers_count, comp.posts_analyzed), ("ready", "apify", "New Creator", 5000, 3))
        self.assertEqual((comp.avg_likes, comp.avg_comments, comp.engagement_rate), (300, 25, 6.5))       # (300 + 25) / 5000 * 100; the hidden -1 post is left out
        self.assertEqual((comp.avg_reel_views, comp.posts_per_week), (14000, 1.0))                        # (20000 + 8000) / 2; 2 posts in the 14 days
        self.assertEqual((comp.product_focused_pct, [c["name"] for c in comp.content_categories]), (40, ["Lifestyle", "Product review"]))
        with self.Session() as db:
            posts = db.query(cc.CompetitorCreatorPost).order_by(cc.CompetitorCreatorPost.post_id).all()
            self.assertEqual([(p.post_id, p.likes_count, p.video_view_count, p.post_url) for p in posts[:1]], [("1", 400, 20000, "https://www.instagram.com/p/AAA/")])
            self.assertEqual(len(posts), 3)

    def test_a_brand_in_a_partnership_post_gets_a_bare_brands_raw_row_when_it_has_none(self):
        self.run_apify_job(self.apify_posts())
        with self.engine.connect() as conn:
            brands = conn.execute(text("SELECT id, instagram_handle, name FROM brands_raw")).all()
        self.assertEqual([(b.instagram_handle, b.name) for b in brands], [("https://www.instagram.com/nike", None)])   # added with its Instagram handle only
        comp = self.competitor("newcreator")
        self.assertEqual(comp.partnership_count, 1)
        self.assertEqual(comp.partnership_brands, [{"brand_raw_id": brands[0].id, "handle": "nike", "name": None, "posts": 1}])
        with self.Session() as db:
            post = db.query(cc.CompetitorCreatorPost).filter_by(post_id="1").one()
            self.assertEqual((post.brand_raw_id, post.brand_handle), (brands[0].id, "nike"))

    def test_a_brand_already_in_brands_raw_is_reused_not_added_again(self):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO brands_raw (id, name, instagram_handle) VALUES (7, 'Nike', 'https://www.instagram.com/nike/')"))
        self.run_apify_job(self.apify_posts())
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM brands_raw")).scalar(), 1)
        comp = self.competitor("newcreator")
        self.assertEqual(comp.partnership_brands, [{"brand_raw_id": 7, "handle": "nike", "name": "Nike", "posts": 1}])

    def test_posts_with_no_brand_are_not_partnerships(self):
        self.run_apify_job(self.apify_posts(), brand_for_post=None)
        comp = self.competitor("newcreator")
        self.assertEqual((comp.partnership_count, comp.partnership_brands), (0, []))

    def test_an_account_with_no_posts_is_taken_back_out(self):
        comp_id, _ = self.run_apify_job([])
        self.assertIsNone(self.competitor("newcreator"))
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM creator_competitors")).scalar(), 0)   # and off the user's list: a wrong handle leaves nothing behind

    def test_apify_not_answering_leaves_a_failed_competitor_that_can_be_added_again(self):
        comp_id, _ = self.add("newcreator")
        with mock.patch("pipeline.enrichment.instagram_users._scrape_posts", return_value=None):
            cc.run_competitor_job(comp_id, 1)
        comp = self.competitor("newcreator")
        self.assertEqual((comp.status, comp.error), ("failed", "Could not reach this Instagram account right now. Please try again."))
        _, runs = self.add("newcreator")
        self.assertTrue(runs)                                            # adding it again starts a new lookup
        self.assertEqual(self.competitor("newcreator").status, "queued")

    # --- adding ---------------------------------------------------------------------------------------------------------------------------------
    def test_a_competitor_someone_else_added_is_reused_with_no_new_job(self):
        self.seed_known_creator()
        comp_id, _ = self.add("alice", user=1)
        with mock.patch.object(cc, "call_gpt_json", return_value={"product_focused_pct": 10, "content_categories": {"Tutorial": 100}}):
            cc.run_competitor_job(comp_id, 1)
        again_id, runs = self.add("@ALICE", user=2)
        self.assertEqual((again_id, runs), (comp_id, False))
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM competitor_creators")).scalar(), 1)
            self.assertEqual(conn.execute(text("SELECT count(*) FROM creator_competitors")).scalar(), 2)

    def test_a_user_can_have_ten_competitors_and_a_bad_handle_is_refused(self):
        for i in range(cc.MAX_COMPETITORS_PER_USER):
            self.add(f"creator{i}")
        with self.Session() as db, self.assertRaisesRegex(ValueError, "up to 10 competitors"):
            cc.start_competitor(db, 1, "one_too_many")
        with self.Session() as db:
            self.assertEqual(cc.start_competitor(db, 1, "creator3")[1], False)   # one already on the list is fine
            with self.assertRaises(ValueError):
                cc.start_competitor(db, 2, "not a handle!")
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM competitor_creators WHERE instagram_handle = 'one_too_many'")).scalar(), 0)


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class TheApi(AgainstPostgres):
    """The /feed/competitors routes (the migrated database is shared with the class above)."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        import api.app as api_app
        import api.competitors_api as competitors_api
        from api.auth import get_completed_user
        from pipeline.db import CreatorProfile, get_db

        def override_db():
            with self.Session() as session:
                yield session

        def user():
            with self.Session() as session:
                return session.get(CreatorProfile, self.user_id)
        self.user_id = 1
        api_app.app.dependency_overrides[get_db] = override_db
        api_app.app.dependency_overrides[get_completed_user] = user
        self.addCleanup(api_app.app.dependency_overrides.clear)
        self.submitted = []

        def run_inline(fn, *args, creator_id, locks, bind):
            self.submitted.append(args)
            fn(*args)
        patcher = mock.patch.object(competitors_api, "submit_user_job", side_effect=run_inline)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api_module = competitors_api
        self.client = TestClient(api_app.app, raise_server_exceptions=False)

    def test_adding_a_known_account_runs_the_lookup_and_the_list_shows_it_ready(self):
        self.seed_known_creator()
        with mock.patch.object(cc, "call_gpt_json", return_value={"product_focused_pct": 30, "content_categories": {"Tutorial": 100}}):
            reply = self.client.post("/feed/competitors", json={"instagram": "https://www.instagram.com/Alice/"})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(len(self.submitted), 1)
        listed = self.client.get("/feed/competitors").json()["competitors"]
        self.assertEqual(len(listed), 1)
        alice = listed[0]
        self.assertEqual((alice["handle"], alice["status"], alice["source"], alice["followers"], alice["engagement_rate"], alice["partnership_count"]), ("alice", "ready", "instagram_users", 1000, 22.0, 2))
        self.assertEqual(alice["content_categories"], [{"name": "Tutorial", "pct": 100}])
        self.assertEqual(alice["partnership_brands"][0]["handle"], "acme")

    def test_a_ready_competitor_is_added_again_without_a_job_or_a_use_of_the_daily_cap(self):
        self.seed_known_creator()
        with mock.patch.object(cc, "call_gpt_json", return_value={"product_focused_pct": 30, "content_categories": {"Tutorial": 100}}):
            self.client.post("/feed/competitors", json={"instagram": "alice"})
        self.user_id = 2
        self.assertEqual(self.client.post("/feed/competitors", json={"instagram": "@alice"}).status_code, 200)
        self.assertEqual(len(self.submitted), 1)                                    # user 2 got the same competitor with no new job
        self.assertEqual(len(self.client.get("/feed/competitors").json()["competitors"]), 1)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT coalesce(sum(uses), 0) FROM feature_usage WHERE creator_profile_id = 2 AND feature = 'competitors'")).scalar(), 0)

    def test_a_bad_handle_is_a_400_and_does_not_use_the_cap(self):
        self.assertEqual(self.client.post("/feed/competitors", json={"instagram": "not a handle!"}).status_code, 400)
        self.assertEqual(self.client.post("/feed/competitors", json={"instagram": "https://www.instagram.com/p/ABC/"}).status_code, 400)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM feature_usage")).scalar(), 0)

    def test_a_daily_cap_of_new_lookups_and_one_lookup_at_a_time(self):
        with mock.patch.dict(os.environ, {"COMPETITORS_PER_DAY": "1"}), mock.patch.object(self.api_module, "submit_user_job"):   # a job that is queued and not run
            self.assertEqual(self.client.post("/feed/competitors", json={"instagram": "first"}).status_code, 200)
            self.assertEqual(self.client.post("/feed/competitors", json={"instagram": "second"}).status_code, 409)   # the first lookup still holds the user's lock
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM feature_locks"))
        with mock.patch.dict(os.environ, {"COMPETITORS_PER_DAY": "1"}), mock.patch.object(self.api_module, "submit_user_job"):
            self.assertEqual(self.client.post("/feed/competitors", json={"instagram": "second"}).status_code, 429)   # the one use of today is gone
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM competitor_creators WHERE instagram_handle = 'second'")).scalar(), 0)   # nothing was created

    def test_a_full_job_queue_is_a_503_and_nothing_is_counted(self):
        from api.jobs import QueueFull
        with mock.patch.object(self.api_module, "submit_user_job", side_effect=QueueFull("user_jobs")):
            self.assertEqual(self.client.post("/feed/competitors", json={"instagram": "someone"}).status_code, 503)
        self.assertEqual(self.competitor("someone").status, "failed")
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT coalesce(sum(uses), 0) FROM feature_usage")).scalar(), 0)
            self.assertEqual(conn.execute(text("SELECT count(*) FROM feature_locks")).scalar(), 0)

    def test_removing_a_competitor_takes_it_off_my_list_only(self):
        self.seed_known_creator()
        with mock.patch.object(cc, "call_gpt_json", return_value={"product_focused_pct": 30, "content_categories": {"Tutorial": 100}}):
            comp_id = self.client.post("/feed/competitors", json={"instagram": "alice"}).json()["id"]
        self.user_id = 2
        self.client.post("/feed/competitors", json={"instagram": "alice"})
        self.assertEqual(self.client.delete(f"/feed/competitors/{comp_id}").json(), {"removed": comp_id})
        self.assertEqual(self.client.get("/feed/competitors").json()["competitors"], [])
        self.assertEqual(self.client.delete(f"/feed/competitors/{comp_id}").status_code, 404)    # not on my list any more
        self.user_id = 1
        self.assertEqual(len(self.client.get("/feed/competitors").json()["competitors"]), 1)     # the other user keeps it
        self.assertIsNotNone(self.competitor("alice"))


if __name__ == "__main__":
    unittest.main()
