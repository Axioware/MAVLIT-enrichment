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
    def test_the_api_returns_it_but_a_save_cannot_set_it(self):
        import api.app as api_app
        from api.schemas import CreatorProfileResponse
        self.assertIn("instagram_engagement_rate", CreatorProfileResponse.model_fields)
        self.assertNotIn("instagram_engagement_rate", api_app.CreatorProfileRequest.model_fields)   # only MAVLIT's generation writes it


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
            conn.execute(text("""INSERT INTO instagram_users (id, username, user_type, followers_count, likes_count, comments_count) VALUES
                (8, 'Alice', 'mention', 1200, 200, 40), (9, 'Alice', 'mention', 1200, -1, -1)"""))
            conn.execute(text("""INSERT INTO test_creator_brand_partnership_posts (brand_raw_id, brand_name, creator_username, post_url, post_timestamp, sponsorship_confidence)
                VALUES (2, 'Globex', 'carol', 'https://i/carol-1', '2026-04-01', 92), (1, 'Acme', 'Alice', 'https://i/alice-2', '2026-06-01', 60),
                       (1, 'Acme', 'Alice', 'https://i/alice-3', '2026-03-01', 97)"""))
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

    def test_profile_numbers_and_the_partnerships_come_from_both_tables(self):
        alice = self.find(own_handle="me")[0]
        self.assertEqual((alice["followers"], alice["country"], alice["full_name"]), (1200, "US", "Alice A"))
        self.assertEqual(alice["tags"], ["skincare"])
        links = [p["post_url"] for p in alice["partnerships"]]
        self.assertEqual(sorted(links), ["https://i/alice-3", "https://i/p-alice"])   # the 60-scored post is not a partnership
        self.assertEqual(alice["partnership_count"], 2)
        carol = self.find(own_handle="me")[1]
        self.assertEqual([(p["brand"], p["post_url"]) for p in carol["partnerships"]], [("Globex", "https://i/carol-1")])

    def test_the_engagement_rate_is_average_likes_plus_comments_over_followers(self):
        found = {c["username"]: c for c in self.find(own_handle="me")}
        alice = found["Alice"]
        self.assertEqual((alice["avg_likes"], alice["avg_comments"], alice["engagement_rate"]), (150, 30, 15.0))   # (150 + 30) / 1200 * 100
        self.assertIsNone(found["carol"]["engagement_rate"])                                                       # no likes known
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
            self.assertEqual(fill_engagement_rates(db, {"alice", "carol", "erin"}), 1)    # alice only: carol has no likes; erin already has a value
        with self.engine.connect() as conn:
            rates = dict(conn.execute(text("SELECT username, engagement_rate FROM creator_niches WHERE username IN ('Alice', 'carol', 'erin')")).all())
        self.assertEqual(rates, {"Alice": 15.0, "carol": None, "erin": 99.0})
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
        self.assertEqual((body["ready"], body["threshold"], [c["username"] for c in body["creators"]]), (True, 70, ["Alice", "carol"]))
        api_app.app.dependency_overrides[get_current_user] = as_user(2)
        self.assertEqual(client.get("/feed/similar-creators").json()["creators"], [])
        self.assertFalse(client.get("/feed/similar-creators").json()["ready"])


if __name__ == "__main__":
    unittest.main()
