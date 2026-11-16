"""Feed > Similar Creators: only creators with a partnership scored >= 90 (instagram_posts or test_creator_brand_partnership_posts) are embedded and shown,
and only when their embedding is at least 70 % similar to the user's. Needs MAVLIT_TEST_PG_URL for the database part."""
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
    """A 1024-dimension unit vector whose cosine similarity to vec(1.0) is `cosine`."""
    v = [0.0] * 1024
    v[0], v[1] = cosine, math.sqrt(max(0.0, 1 - cosine * cosine))
    return v


class TextOfTheUser(unittest.TestCase):
    def test_the_user_is_written_like_a_creator(self):
        from types import SimpleNamespace
        from pipeline.similar_creators import user_query_text
        profile = SimpleNamespace(instagram_primary_niche="Music", content_niche="Music, DJ", instagram_description=" Vinyl DJ. ", youtube_description=None,
                                  instagram_sub_niches=["vinyl", " "], youtube_sub_niches=["vinyl", "djing"], content_tags=["records"])
        self.assertEqual(user_query_text(profile), "Niche: Music\nDescription: Vinyl DJ.\nTags: vinyl, djing, records")
        profile.instagram_primary_niche = None
        self.assertTrue(user_query_text(profile).startswith("Niche: Music, DJ\n"))
        empty = SimpleNamespace(instagram_primary_niche=None, content_niche=None, instagram_description=None, youtube_description=None,
                                instagram_sub_niches=None, youtube_sub_niches=None, content_tags=None)
        self.assertIsNone(user_query_text(empty))


class EngagementRate(unittest.TestCase):
    def test_average_likes_plus_average_comments_over_followers(self):
        from pipeline.helpers.engagement import engagement_rate, engagement_rate_from_posts
        self.assertEqual(engagement_rate(150, 30, 1200), 15.0)
        self.assertIsNone(engagement_rate(None, 30, 1200))
        self.assertIsNone(engagement_rate(100, 10, 0))
        self.assertIsNone(engagement_rate(100, 10, None))
        # posts: (likes, comments); -1 is a count Instagram hides and None is missing - neither counts
        self.assertEqual(engagement_rate_from_posts([(100, 10), (300, 30), (-1, -1), (None, None)], 2000), 11.0)   # (average likes 200 + average comments 20) / 2000 * 100
        self.assertIsNone(engagement_rate_from_posts([(-1, 5), (None, None)], 2000))
        self.assertEqual(engagement_rate_from_posts([(50, None), (150, None)], 1000), 10.0)                      # no comments known: likes only
        self.assertEqual(engagement_rate_from_posts([(True, 1)], 100), None)                                       # a bool is not a count


class ProfileGenerationCalculatesTheEngagementRate(unittest.TestCase):
    def test_the_generated_profile_has_the_engagement_rate_of_its_posts(self):
        from pipeline.creator_content import instagram_description as d
        items = [{"id": str(i), "caption": f"c{i}", "likesCount": likes, "commentsCount": comments, "metaData": {"followersCount": 1000, "biography": "bio"}}
                 for i, (likes, comments) in enumerate([(80, 10), (120, 10), (-1, -1)])]
        with mock.patch.object(d, "_saved_items", return_value=items), mock.patch.object(d, "_POSTS_LIMIT", 3), \
                mock.patch.object(d, "sub_niche_options", return_value=[]), mock.patch.object(d, "_get_description_prompt", return_value="{niche}{bio}{captions}{sub_niche_options}{followers}"), \
                mock.patch.object(d, "call_gpt_json", return_value={"description": "a creator"}):
            result = d.generate_instagram_description(mock.Mock(), "someone", "Music")
        self.assertEqual(result["followers_count"], 1000)
        self.assertEqual(result["engagement_rate"], 11.0)    # (average likes 100 + average comments 10) / 1000 * 100; the hidden post is left out


class EngagementRateOfTheProfile(unittest.TestCase):
    BODY = {"full_name": "A", "creator_handle": "a"}

    def test_the_api_returns_it_and_a_save_may_correct_it_within_0_to_100(self):
        import api.app as api_app
        from api.schemas import CreatorProfileResponse
        from pydantic import ValidationError
        self.assertIn("instagram_engagement_rate", CreatorProfileResponse.model_fields)
        request = api_app.CreatorProfileRequest
        self.assertEqual(request(**self.BODY, instagram_engagement_rate=4.2).instagram_engagement_rate, 4.2)
        self.assertEqual(request(**self.BODY, instagram_engagement_rate=0).instagram_engagement_rate, 0)
        for bad in (-1, 100.5, "x"):
            with self.assertRaises(ValidationError):
                request(**self.BODY, instagram_engagement_rate=bad)

    def test_a_request_without_it_is_told_apart_from_one_that_clears_it(self):
        import api.app as api_app
        request = api_app.CreatorProfileRequest
        self.assertNotIn("instagram_engagement_rate", request(**self.BODY).model_fields_set)                          # the handler leaves the saved rate alone
        self.assertIn("instagram_engagement_rate", request(**self.BODY, instagram_engagement_rate=None).model_fields_set)   # the handler clears it

    def test_brand_partnerships_are_no_longer_typed_on_the_profile(self):
        import api.app as api_app
        from api.schemas import CreatorProfileResponse
        self.assertNotIn("brand_partnerships", api_app.CreatorProfileRequest.model_fields)    # the count comes from the posts marked as partnerships
        self.assertNotIn("brand_partnerships", CreatorProfileResponse.model_fields)


class PostingAndViews(unittest.TestCase):
    def test_posts_per_week_counts_the_14_days_before_the_newest_post(self):
        from pipeline.similar_creators import posts_per_week
        self.assertEqual(posts_per_week(["2026-05-10T10:00:00.000Z", "2026-05-08T10:00:00Z", "2026-04-26T10:00:01Z", "2026-03-01T00:00:00Z"]), 1.5)   # 3 posts in 14 days: 1.5 a week
        self.assertEqual(posts_per_week(["2026-05-10T10:00:00Z", "2026-04-26T10:00:00Z"]), 1.0)                  # exactly 14 days back still counts
        self.assertEqual(posts_per_week(["2026-01-01T00:00:00Z"]), 0.5)                                          # a single post: 1 in 2 weeks, however old it is
        self.assertEqual(posts_per_week(["2026-05-10", None, "not a date", ""]), 0.5)                            # unreadable dates are ignored
        self.assertIsNone(posts_per_week([None, "", "garbage"]))
        self.assertIsNone(posts_per_week([]))

    def test_average_views_ignores_missing_and_zero(self):
        from pipeline.similar_creators import average_views
        self.assertEqual(average_views([1000, 3000, None, 0, -1]), 2000)
        self.assertIsNone(average_views([None, 0]))
        self.assertIsNone(average_views([]))


class PostKind(unittest.TestCase):
    def test_what_a_partnership_post_is_called(self):
        from pipeline.similar_creators import post_kind
        self.assertEqual(post_kind("https://www.instagram.com/reel/A/", "Image", None), "Reel")    # the link wins
        self.assertEqual(post_kind("https://www.instagram.com/p/A/", "Video", None), "Video")
        self.assertEqual(post_kind("https://www.instagram.com/p/A/", "Sidecar", None), "Carousel")
        self.assertEqual(post_kind("https://www.instagram.com/p/A/", "Image", 10), "Photo")
        self.assertEqual(post_kind(None, None, 120), "Video")                                       # views mean video
        self.assertEqual(post_kind(None, None, None), "Post")


class TextOfAnEmbedding(unittest.TestCase):
    def test_same_format_as_the_embedding_script(self):
        from pipeline.similar_creators import embedding_text
        self.assertEqual(embedding_text("Beauty", "Skincare tips", ["skincare", " ", "spf"]), "Niche: Beauty\nDescription: Skincare tips\nTags: skincare, spf")
        self.assertEqual(embedding_text("Beauty", "d", None), "Niche: Beauty\nDescription: d\nTags: ")


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
            conn.execute(text("TRUNCATE creator_niches, test_creator_brand_partnership_posts, instagram_posts, instagram_users, content_creator_re, brands_raw, creator_profiles RESTART IDENTITY CASCADE"))
            conn.execute(text("INSERT INTO brands_raw (id, name) VALUES (1, 'Acme'), (2, 'Globex')"))
            # alice: partnership 95 on instagram_posts; bob: only 80 there; carol: 92 only in the test table; dave: commenter with a 99 post
            # erin: 95 partnership but only 50 % similar; frank: partnership 91 but no embedding yet; me: the user's own handle (95, 100 % similar)
            conn.execute(text("""INSERT INTO instagram_posts (brand_raw_id, instagram_handle, post_id, post_url, timestamp, sponsorship_confidence) VALUES
                (1, 'acme', 'p-alice', 'https://i/p-alice', '2026-05-01', 95), (1, 'acme', 'p-bob', 'https://i/p-bob', '2026-05-02', 80),
                (2, 'globex', 'p-dave', 'https://i/p-dave', '2026-05-03', 99), (2, 'globex', 'p-erin', 'https://i/p-erin', '2026-05-04', 95),
                (2, 'globex', 'p-frank', 'https://i/p-frank', '2026-05-05', 91), (1, 'acme', 'p-me', 'https://i/p-me', '2026-05-06', 95)"""))
            conn.execute(text("""INSERT INTO instagram_users (id, username, user_type, post_id, followers_count, country, full_name) VALUES
                (1, 'Alice', 'mention', 'p-alice', 1200, 'US', 'Alice A'), (2, 'bob', 'mention', 'p-bob', 900, 'US', NULL),
                (3, 'carol', 'contentcreatorRE', NULL, 700, 'UK', NULL), (4, 'dave', 'commenter', 'p-dave', 50, NULL, NULL),
                (5, 'erin', 'tagged_user', 'p-erin', 500, NULL, NULL), (6, 'frank', 'tagged_user', 'p-frank', 400, NULL, NULL),
                (7, 'me', 'mention', 'p-me', 100, NULL, NULL)"""))
            # alice's likes and comments: two posts, (100 + 200) / 2 likes and (20 + 40) / 2 comments; one post has hidden counts (-1) that must not count
            conn.execute(text("UPDATE instagram_users SET likes_count = 100, comments_count = 20 WHERE id = 1"))
            # alice's own posts: two inside the 14 days before her newest post (views 1000 and 3000), one months older without views
            conn.execute(text("UPDATE instagram_users SET post_timestamp = '2026-05-10T10:00:00.000Z', video_view_count = 1000 WHERE id = 1"))
            conn.execute(text("UPDATE instagram_posts SET post_type = 'Video', video_view_count = 5000 WHERE post_id = 'p-alice'"))
            # carol's partnership post is in the test table only; her own scraped row for that post holds its likes and comments, the test row its views
            conn.execute(text("UPDATE instagram_users SET post_id = 'cp-1', likes_count = 50, comments_count = 10 WHERE id = 3"))
            conn.execute(text("""INSERT INTO instagram_users (id, username, user_type, followers_count, likes_count, comments_count) VALUES
                (8, 'Alice', 'mention', 1200, 200, 40), (9, 'Alice', 'mention', 1200, -1, -1)"""))
            conn.execute(text("UPDATE instagram_users SET post_timestamp = '2026-05-08T10:00:00.000Z', video_play_count = 3000 WHERE id = 8"))
            conn.execute(text("UPDATE instagram_users SET post_timestamp = '2026-03-01T10:00:00.000Z' WHERE id = 9"))
            conn.execute(text("""INSERT INTO test_creator_brand_partnership_posts (brand_raw_id, brand_name, creator_username, post_url, post_timestamp, sponsorship_confidence, post_id, video_play_count)
                VALUES (2, 'Globex', 'carol', 'https://www.instagram.com/reel/XYZ/', '2026-04-01', 92, 'cp-1', 900), (1, 'Acme', 'Alice', 'https://i/alice-2', '2026-06-01', 60, NULL, NULL),
                       (1, 'Acme', 'Alice', 'https://i/alice-3', '2026-03-01', 97, NULL, NULL)"""))
            for uid, name, cosine in [(1, "Alice", 0.9), (2, "bob", 0.95), (3, "carol", 0.75), (4, "dave", 0.99), (5, "erin", 0.5), (7, "me", 1.0)]:
                conn.execute(text("INSERT INTO creator_niches (instagram_user_id, username, niche, description, tags, embedding) VALUES (:i, :u, 'Beauty', 'desc', CAST('[\"skincare\"]' AS jsonb), :e)"),
                             {"i": uid, "u": name, "e": str(vec(cosine))})
            conn.execute(text("INSERT INTO creator_niches (instagram_user_id, username, niche, description) VALUES (6, 'frank', 'Beauty', 'desc')"))

    def find(self, **kwargs):
        from pipeline.similar_creators import find_similar_creators
        with self.Session() as db:
            return find_similar_creators(db, vec(1.0), **kwargs)

    def test_partnership_creators_are_the_ones_with_a_score_of_90_in_either_table(self):
        from pipeline.similar_creators import partnership_usernames
        with self.Session() as db:
            self.assertEqual(partnership_usernames(db), {"alice", "carol", "erin", "frank", "me"})   # not bob (80), not dave (commenter)

    def test_only_partnership_creators_at_70_percent_or_more_are_shown_most_similar_first(self):
        found = self.find(own_handle="me")
        self.assertEqual([c["username"] for c in found], ["Alice", "carol"])    # bob: no 90 partnership; dave: commenter; erin: 50 %; frank: no embedding; me: own handle
        self.assertEqual([c["similarity"] for c in found], [90, 75])

    def test_the_threshold_is_70_percent(self):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_niches SET embedding = CAST(:e AS vector) WHERE username = 'erin'"), {"e": str(vec(0.71))})
        self.assertIn("erin", [c["username"] for c in self.find(own_handle="me")])
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_niches SET embedding = CAST(:e AS vector) WHERE username = 'erin'"), {"e": str(vec(0.69))})
        self.assertNotIn("erin", [c["username"] for c in self.find(own_handle="me")])

    def test_creators_get_average_reel_views_and_posts_per_week(self):
        found = {c["username"]: c for c in self.find(own_handle="me")}
        # the summary of all their partnership posts (Acme x2 for alice: the brand-side post and a test-table one; nothing is known about brands' niches in this data)
        self.assertEqual((found["Alice"]["unique_brands"], found["Alice"]["top_brand_categories"], found["Alice"]["top_formats"]), (1, [], ["Post", "Video"]))
        self.assertEqual((found["carol"]["unique_brands"], found["carol"]["top_formats"]), (1, ["Reel"]))
        self.assertEqual((found["Alice"]["avg_reel_views"], found["Alice"]["posts_per_week"]), (2000, 1.0))   # views 1000 and 3000; 2 posts in 14 days
        # carol has no view counts on her own posts, so the average falls back to her partnership posts (the one with 900 views)
        self.assertEqual((found["carol"]["avg_reel_views"], found["carol"]["posts_per_week"]), (900, None))

    def test_the_users_own_numbers(self):
        from pipeline.similar_creators import your_stats
        from pipeline.db import CreatorProfile
        with self.engine.begin() as conn:
            conn.execute(text("""INSERT INTO creator_profiles (id, email, full_name, instagram_handle, instagram_followers, instagram_engagement_rate)
                                 VALUES (1, 'u@example.test', 'Nova H', 'me', 5000, 4.2)"""))
            conn.execute(text("""INSERT INTO creator_instagram_posts (creator_profile_id, instagram_handle, post_id, timestamp, video_view_count, video_play_count, likes_count, comments_count) VALUES
                (1, 'me', 'a', '2026-06-10T10:00:00.000Z', 100, NULL, 200, 20), (1, 'me', 'b', '2026-06-01T10:00:00.000Z', NULL, 500, 100, 10), (1, 'me', 'c', '2026-04-01T10:00:00.000Z', NULL, NULL, -1, -1)"""))
        with self.Session() as db:
            stats = your_stats(db, db.get(CreatorProfile, 1))
        self.assertEqual(stats, {"name": "Nova H", "handle": "me", "avg_likes": 150, "avg_comments": 15, "followers": 5000, "engagement_rate": 4.2, "avg_reel_views": 300, "posts_per_week": 1.0, "brand_partnerships": 0,
                                 "product_focused_pct": None, "content_categories": None})   # 2 of 3 posts in the 14 days; no post is marked as a partnership: 0; the content mix is not generated yet
        # each post the user marked as a paid partnership counts as one brand partnership
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_instagram_posts SET partnership_post = true WHERE post_id IN ('a', 'b')"))
        with self.Session() as db:
            self.assertEqual(your_stats(db, db.get(CreatorProfile, 1))["brand_partnerships"], 2)
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_instagram_posts SET partnership_post = false WHERE post_id = 'a'"))     # switched off again
        with self.Session() as db:
            self.assertEqual(your_stats(db, db.get(CreatorProfile, 1))["brand_partnerships"], 1)

    def test_the_audience_filter_keeps_creators_between_a_third_and_double_the_followers(self):
        from pipeline.similar_creators import audience_range
        self.assertEqual(audience_range(1000), (334, 2000))     # a third rounded up, and twice
        self.assertEqual(audience_range(3), (1, 6))
        self.assertIsNone(audience_range(None))
        self.assertIsNone(audience_range(0))
        # alice has 1200 followers, carol 700 (both above the 70 % match); erin 500 is not similar enough anyway
        names = lambda rng: [c["username"] for c in self.find(own_handle="me", followers_range=rng)]
        self.assertEqual(names(None), ["Alice", "carol"])
        self.assertEqual(names(audience_range(1000)), ["Alice", "carol"])      # 334 to 2000
        self.assertEqual(names(audience_range(3000)), ["Alice"])               # 1000 to 6000: alice (1200) is in, carol (700) is out
        self.assertEqual(names((701, 5000)), ["Alice"])                        # carol (700) is out when the range starts at 701
        self.assertEqual(names((1, 650)), [])
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE instagram_users SET followers_count = NULL WHERE id = 3"))   # carol: no follower count
        self.assertEqual(names(audience_range(1000)), ["Alice"])               # a creator with no follower count is left out by the hard filter
        self.assertEqual(names(None), ["Alice", "carol"])                      # and stays in without the filter

    def test_profile_numbers_and_the_partnerships_come_from_both_tables(self):
        alice = self.find(own_handle="me")[0]
        self.assertEqual((alice["followers"], alice["country"], alice["full_name"]), (1200, "US", "Alice A"))
        self.assertEqual(alice["tags"], ["skincare"])
        links = [p["post_url"] for p in alice["partnerships"]]
        self.assertEqual(sorted(links), ["https://i/alice-3", "https://i/p-alice"])   # the 60-scored post is not a partnership
        self.assertEqual(alice["partnership_count"], 2)
        carol = self.find(own_handle="me")[1]
        self.assertEqual([(p["brand"], p["post_url"]) for p in carol["partnerships"]], [("Globex", "https://www.instagram.com/reel/XYZ/")])
        # what the partnership dialog shows for each post
        by_url = {p["post_url"]: p for p in alice["partnerships"]}
        post = by_url["https://i/p-alice"]
        self.assertEqual((post["type"], post["likes"], post["comments"], post["views"], post["engagement"]), ("Video", 100, 20, 5000, 10.0))   # (100 + 20) / 1200 * 100
        self.assertEqual(post["category"], None)                                                                                              # brands_raw.niche is empty in the test data
        self.assertIsNone(post["thumbnail_url"])                                                                                              # "https://i/p-alice" is not an Instagram post link
        bare = by_url["https://i/alice-3"]
        self.assertEqual((bare["type"], bare["likes"], bare["views"], bare["engagement"]), ("Post", None, None, None))                       # nothing is known about that post
        first = carol["partnerships"][0]
        self.assertEqual((first["type"], first["likes"], first["comments"], first["views"], first["engagement"]), ("Reel", 50, 10, 900, 8.6))   # (50 + 10) / 700 * 100
        self.assertEqual(first["thumbnail_url"], "/feed/post-thumbnail?code=XYZ")                                                           # fetched by the server, see the endpoint test

    def test_the_engagement_rate_is_average_likes_plus_comments_over_followers(self):
        found = {c["username"]: c for c in self.find(own_handle="me")}
        alice = found["Alice"]
        self.assertEqual((alice["avg_likes"], alice["avg_comments"], alice["engagement_rate"]), (150, 30, 15.0))   # (150 + 30) / 1200 * 100
        self.assertEqual(found["carol"]["engagement_rate"], 8.6)                                                   # (50 + 10) / 700 * 100
        from pipeline.similar_creators import engagement_rate
        self.assertEqual(engagement_rate(50, 10, 2000), 3.0)
        self.assertIsNone(engagement_rate(50, 10, 0))
        self.assertIsNone(engagement_rate(None, 10, 2000))
        self.assertEqual(engagement_rate(50, None, 1000), 5.0)

    def test_the_script_fills_the_engagement_rate_only_where_it_is_missing(self):
        from pipeline.similar_creators import fill_engagement_rates
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_niches SET engagement_rate = 99.0 WHERE username = 'erin'"))
        with self.Session() as db:
            self.assertEqual(fill_engagement_rates(db, {"alice", "carol", "erin"}), 2)    # alice and carol; erin already has a value
        with self.engine.connect() as conn:
            rates = dict(conn.execute(text("SELECT username, engagement_rate FROM creator_niches WHERE username IN ('Alice', 'carol', 'erin')")).all())
        self.assertEqual(rates, {"Alice": 15.0, "carol": 8.6, "erin": 99.0})
        found = {c["username"]: c for c in self.find(own_handle="me")}
        self.assertEqual(found["Alice"]["engagement_rate"], 15.0)                          # the saved value is what the tab shows

    def test_creators_are_embedded_only_when_they_have_a_partnership_and_no_embedding(self):
        from pipeline import similar_creators
        with self.Session() as db:
            seen = []
            def fake(texts, context=""):
                seen.extend(texts)
                return [vec(0.8) for _ in texts]
            with mock.patch.object(similar_creators, "embed_text_batch", side_effect=fake):
                self.assertEqual(similar_creators.embed_pending_creators(db, {"alice", "carol", "erin", "frank", "me"}), 1)   # only frank has none
        self.assertEqual(seen, ["Niche: Beauty\nDescription: desc\nTags: "])
        with self.engine.connect() as conn:
            self.assertTrue(conn.execute(text("SELECT embedding IS NOT NULL FROM creator_niches WHERE username = 'frank'")).scalar())

    def test_the_niche_script_only_processes_partnership_creators_and_then_embeds(self):
        from pipeline.enrichment import creator_niches
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM creator_niches"))
            conn.execute(text("UPDATE instagram_users SET niche = 'Beauty', bio = 'bio'"))
        generated = []
        def fake_generate(db, username, context, retries):
            generated.append(username)
            return {"description": "a creator", "tags": ["a", "b", "c", "d", "e"]}
        embedded = []
        with self.Session() as db, mock.patch.object(creator_niches, "_generate_niche", side_effect=fake_generate), \
                mock.patch.object(creator_niches, "embed_pending_creators", side_effect=lambda db_, names: embedded.append(set(names))):
            created = creator_niches.run_creator_niches(db)
        self.assertEqual(sorted(generated), ["Alice", "carol", "erin", "frank", "me"])   # not bob (score 80) and not dave (commenter)
        self.assertEqual(created, 5)
        self.assertEqual(embedded, [{"alice", "carol", "erin", "frank", "me"}])

    def test_diagnose_shows_where_the_list_gets_empty(self):
        from pipeline.similar_creators import diagnose
        from pipeline import similar_creators
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO creator_profiles (id, email, instagram_primary_niche, instagram_description) VALUES (1, 'u@example.test', 'Beauty', 'Skincare')"))
        with self.Session() as db, mock.patch.object(similar_creators, "embed_text", return_value=vec(1.0)):
            similar_creators._QUERY_CACHE.clear()
            report = diagnose(db, "U@example.test")
        self.assertEqual((report["partnership_creators"], report["creator_niches_rows"], report["creator_niches_with_embedding"]), (5, 7, 6))
        self.assertEqual(report["partnership_creators_with_an_embedding"], 4)    # frank has a row but no embedding
        self.assertTrue(report["user_has_embedding"])
        self.assertEqual(report["closest_creators_any"][0], {"username": "me", "similarity": 100.0, "has_partnership": True})

    def test_only_partnership_posts_have_a_picture_to_fetch(self):
        from pipeline.similar_creators import is_partnership_post_code
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE instagram_posts SET post_url = 'https://www.instagram.com/p/AbC_123/' WHERE post_id = 'p-alice'"))
            conn.execute(text("UPDATE instagram_posts SET post_url = 'https://www.instagram.com/p/LowScore1/' WHERE post_id = 'p-bob'"))   # bob's post is scored 80
        with self.Session() as db:
            self.assertTrue(is_partnership_post_code(db, "AbC_123"))     # brand-side post scored 95
            self.assertTrue(is_partnership_post_code(db, "XYZ"))         # test-table post scored 92
            self.assertFalse(is_partnership_post_code(db, "LowScore1"))  # scored below 90
            self.assertFalse(is_partnership_post_code(db, "Unknown99"))

    def test_the_picture_endpoint_serves_only_partnership_posts(self):
        from fastapi.testclient import TestClient
        import api.app as api_app
        import api.feed_api as feed_api
        from api.auth import get_current_user
        from pipeline.db import get_db
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE instagram_posts SET post_url = 'https://www.instagram.com/p/AbC_123/' WHERE post_id = 'p-alice'"))

        def override_db():
            with self.Session() as session:
                yield session
        api_app.app.dependency_overrides[get_db] = override_db
        api_app.app.dependency_overrides[get_current_user] = lambda: object()
        self.addCleanup(api_app.app.dependency_overrides.clear)
        client = TestClient(api_app.app, raise_server_exceptions=False)
        asked = []
        with mock.patch.object(feed_api, "_fetch_thumbnail", side_effect=lambda code: asked.append(code) or (b"jpegbytes", "image/jpeg")):
            ok = client.get("/feed/post-thumbnail?code=AbC_123")
            self.assertEqual((ok.status_code, ok.content, ok.headers["content-type"]), (200, b"jpegbytes", "image/jpeg"))
            self.assertIn("private", ok.headers["cache-control"])
            self.assertEqual(client.get("/feed/post-thumbnail?code=Unknown99").status_code, 404)    # not a partnership post: never fetched
            self.assertEqual(client.get("/feed/post-thumbnail?code=a/b").status_code, 404)           # not a short code
            self.assertEqual(asked, ["AbC_123"])
        with mock.patch.object(feed_api, "_fetch_thumbnail", return_value=None):
            self.assertEqual(client.get("/feed/post-thumbnail?code=AbC_123").status_code, 404)       # Instagram has no picture
        with mock.patch.object(feed_api, "_fetch_thumbnail", side_effect=feed_api.ThumbnailTooLarge()):
            self.assertEqual(client.get("/feed/post-thumbnail?code=AbC_123").status_code, 502)

    def test_the_description_job_saves_the_engagement_rate_and_the_content_mix(self):
        import api.app as api_app
        from pipeline.db import CreatorProfile
        with self.engine.begin() as conn:
            conn.execute(text("""INSERT INTO creator_profiles (id, email, instagram_description_status, instagram_description_job_id, instagram_handle)
                                 VALUES (1, 'u@example.test', 'queued', 'job-1', 'me')"""))
        result = {"description": "A creator", "followers_count": 2000, "engagement_rate": 4.5, "product_focused_pct": 35,
                  "content_categories": [{"name": "Tutorial", "pct": 32}, {"name": "Product review", "pct": 18}]}
        with mock.patch.object(api_app, "BatchSessionLocal", self.Session), mock.patch.object(api_app, "generate_instagram_description", return_value=result):
            api_app._run_creator_description_job(1, "instagram", "me", "Music", "job-1")
        with self.Session() as db:
            row = db.get(CreatorProfile, 1)
            self.assertEqual((row.instagram_description_status, row.instagram_engagement_rate, row.instagram_product_focused_pct), ("completed", 4.5, 35))
            self.assertEqual(row.instagram_content_categories, [{"name": "Tutorial", "pct": 32}, {"name": "Product review", "pct": 18}])
        # a generation that returns no mix keeps what was saved
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET instagram_description_status = 'queued', instagram_description_job_id = 'job-2' WHERE id = 1"))
        with mock.patch.object(api_app, "BatchSessionLocal", self.Session), mock.patch.object(api_app, "generate_instagram_description",
                                                                                              return_value={"description": "A creator", "product_focused_pct": None, "content_categories": None}):
            api_app._run_creator_description_job(1, "instagram", "me", "Music", "job-2")
        with self.Session() as db:
            row = db.get(CreatorProfile, 1)
            self.assertEqual((row.instagram_product_focused_pct, len(row.instagram_content_categories)), (35, 2))

    def test_the_script_estimates_the_content_mix_only_where_it_is_missing(self):
        from pipeline.enrichment import creator_niches
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE instagram_users SET caption = 'Gym tutorial: how to squat', niche = 'Fitness', bio = 'coach' WHERE id IN (1, 8)"))   # alice: two posts
            conn.execute(text("UPDATE creator_niches SET product_focused_pct = 80, content_categories = CAST('[{\"name\": \"Promotional\", \"pct\": 100}]' AS jsonb) WHERE username = 'erin'"))
        asked = []

        def fake_llm(prompt, context="", **kwargs):
            asked.append((context, prompt))
            return {"product_focused_pct": 40, "content_categories": {"Tutorial": 70, "Lifestyle": 30}}
        with self.Session() as db, mock.patch.object(creator_niches, "call_gpt_json", side_effect=fake_llm):
            done = creator_niches.fill_content_mix(db, {"alice", "carol", "erin"})
        self.assertEqual(done, 1)                                     # alice only: carol has no captions; erin already has a mix
        self.assertEqual(len(asked), 1)
        self.assertIn("Gym tutorial: how to squat", asked[0][1])
        with self.engine.connect() as conn:
            rows = {r[0]: r[1:] for r in conn.execute(text("SELECT username, product_focused_pct, content_categories FROM creator_niches WHERE username IN ('Alice', 'carol', 'erin')"))}
        self.assertEqual(rows["Alice"], (40, [{"name": "Tutorial", "pct": 70}, {"name": "Lifestyle", "pct": 30}]))
        self.assertEqual(rows["carol"], (None, None))
        self.assertEqual(rows["erin"][0], 80)                          # an existing mix is never replaced
        found = {c["username"]: c for c in self.find(own_handle="me")}
        self.assertEqual((found["Alice"]["product_focused_pct"], found["Alice"]["content_categories"][0]), (40, {"name": "Tutorial", "pct": 70}))
        self.assertEqual((found["carol"]["product_focused_pct"], found["carol"]["content_categories"]), (None, None))

    def test_the_content_mix_stops_cleanly_when_openai_is_unavailable(self):
        from pipeline.enrichment import creator_niches
        from pipeline.helpers.gpt_llm import LLMUnavailableError
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE instagram_users SET caption = 'a caption', niche = 'Fitness' WHERE id IN (1, 3)"))
        replies = [{"product_focused_pct": 10, "content_categories": {"Lifestyle": 100}}, LLMUnavailableError("down")]
        with self.Session() as db, mock.patch.object(creator_niches, "call_gpt_json", side_effect=replies):
            with self.assertRaises(LLMUnavailableError):
                creator_niches.fill_content_mix(db, {"alice", "carol"})
        with self.engine.connect() as conn:
            saved = conn.execute(text("SELECT count(*) FROM creator_niches WHERE content_categories IS NOT NULL")).scalar()
        self.assertEqual(saved, 1)                                     # the creator finished before the outage is kept

    def test_the_endpoint(self):
        from fastapi.testclient import TestClient
        import api.app as api_app
        from api.auth import get_current_user
        from pipeline.db import CreatorProfile, get_db
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO creator_profiles (id, email, instagram_handle, instagram_primary_niche, instagram_description) VALUES (1, 'u@example.test', '@me', 'Beauty', 'Skincare'), (2, 'v@example.test', NULL, NULL, NULL)"))

        def override_db():
            with self.Session() as session:
                yield session

        def as_user(user_id):
            def dependency():
                with self.Session() as session:
                    return session.get(CreatorProfile, user_id)
            return dependency
        api_app.app.dependency_overrides[get_db] = override_db
        self.addCleanup(api_app.app.dependency_overrides.clear)
        client = TestClient(api_app.app, raise_server_exceptions=False)
        api_app.app.dependency_overrides[get_current_user] = as_user(1)
        from pipeline import similar_creators
        similar_creators._QUERY_CACHE.clear()
        asked = []
        with mock.patch.object(similar_creators, "embed_text", side_effect=lambda t, context="": asked.append(t) or vec(1.0)):
            body = client.get("/feed/similar-creators").json()
            client.get("/feed/similar-creators")
        self.assertEqual(asked, ["Niche: Beauty\nDescription: Skincare\nTags: "])   # the user in the creators' format, embedded once
        self.assertEqual(body["ready"], True)
        self.assertEqual(body["you"]["handle"], "@me")
        self.assertEqual(body["you"]["brand_partnerships"], 0)     # no post marked as a partnership: 0, not empty
        self.assertEqual((body["your_followers"], body["audience_range"]), (None, None))   # the test user has no follower count
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET instagram_followers = 1000 WHERE id = 1"))
        names = lambda url: [c["username"] for c in client.get(url).json()["creators"]]
        self.assertEqual(names("/feed/similar-creators?same_audience=true"), ["Alice", "carol"])       # 334 to 2000
        self.assertEqual(client.get("/feed/similar-creators").json()["audience_range"], {"min": 334, "max": 2000})
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET instagram_followers = 3000 WHERE id = 1"))
        self.assertEqual(names("/feed/similar-creators?same_audience=true"), ["Alice"])                # 1000 to 6000
        self.assertEqual(names("/feed/similar-creators"), ["Alice", "carol"])                          # without the filter everyone similar stays
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE creator_profiles SET instagram_followers = NULL WHERE id = 1"))
        self.assertEqual(names("/feed/similar-creators?same_audience=true"), [])                       # no follower count: the filter shows nothing
        self.assertEqual((body["ready"], body["threshold"], [c["username"] for c in body["creators"]]), (True, 70, ["Alice", "carol"]))
        api_app.app.dependency_overrides[get_current_user] = as_user(2)
        self.assertEqual(client.get("/feed/similar-creators").json()["creators"], [])
        self.assertFalse(client.get("/feed/similar-creators").json()["ready"])


if __name__ == "__main__":
    unittest.main()
