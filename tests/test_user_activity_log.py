"""Adding or removing an Instagram post is logged in user_activity and shows in the admin timeline and in Top 10 users by activity. Needs MAVLIT_TEST_PG_URL."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

PASSWORD = "Sup3r-secret-pass"


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class PostActivityLog(unittest.TestCase):
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

    def setUp(self):
        import api.app as api_app
        import api.attempt_limits as attempt_limits
        from pipeline.db import CreatorInstagramPost, CreatorProfile, get_db
        from pipeline.helpers.passwords import hash_password
        engine = self.engine
        factory = lambda *a, **k: Session(bind=engine, autoflush=False, expire_on_commit=False)

        def override():
            with factory() as session:
                yield session
        api_app.app.dependency_overrides[get_db] = override
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        for patcher in (mock.patch.object(attempt_limits, "SessionLocal", factory), mock.patch.object(api_app, "SessionLocal", factory)):
            patcher.start()
            self.addCleanup(patcher.stop)
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, user_activity RESTART IDENTITY CASCADE"))
        with factory() as s:
            self.user = CreatorProfile(email="poster@example.test", password_hash=hash_password(PASSWORD), is_active=True)
            s.add(self.user)
            s.flush()
            s.add_all([CreatorInstagramPost(creator_profile_id=self.user.id, instagram_handle="poster", post_id=f"p{i}", post_url=f"https://www.instagram.com/p/P{i}/")
                       for i in range(11)])   # 11 posts: one more than the 10 that always stay, so exactly one can be deleted
            s.commit()
            self.user_id = self.user.id
            self.first_post = s.query(CreatorInstagramPost).filter_by(creator_profile_id=self.user_id).order_by(CreatorInstagramPost.id).first().id
        self.client = TestClient(api_app.app, follow_redirects=False)
        self.assertEqual(self.client.post("/auth/login", json={"email": "poster@example.test", "password": PASSWORD}).status_code, 200)

    def kinds(self):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT kind, detail FROM user_activity WHERE creator_profile_id = :i ORDER BY id"), {"i": self.user_id}).all()

    def test_a_real_delete_is_logged_and_a_refused_one_is_not(self):
        self.assertEqual(self.client.delete(f"/creator-profile/me/posts/{self.first_post}").status_code, 200)
        self.assertEqual(self.kinds(), [("instagram_post_removed", "Removed an Instagram post")])
        with self.engine.connect() as conn:
            second = conn.execute(text("SELECT min(id) FROM creator_instagram_posts WHERE creator_profile_id = :i"), {"i": self.user_id}).scalar()
        self.assertEqual(self.client.delete(f"/creator-profile/me/posts/{second}").status_code, 400)   # only 10 left: nothing is deleted
        self.assertEqual(len(self.kinds()), 1)                                                          # and nothing more is logged

    def test_adding_a_post_is_logged_but_a_post_that_was_already_there_is_not(self):
        import api.creator_posts as creator_posts
        from pipeline.db import CreatorInstagramPost
        with Session(self.engine) as s:
            row = s.query(CreatorInstagramPost).filter_by(creator_profile_id=self.user_id).first()
            s.expunge(row)
        for how, logged in (("scraped", 1), ("copied", 2), ("already_added", 2)):
            with mock.patch.object(creator_posts, "add_post", return_value=(row, how)):
                response = self.client.post("/creator-profile/me/posts", json={"post_url": "https://www.instagram.com/p/NEW123/"})
            self.assertEqual(response.status_code, 200, response.text[:300])
            self.assertEqual(len(self.kinds()), logged, how)
        self.assertEqual({k for k, _d in self.kinds()}, {"instagram_post_added"})

    def test_a_logging_failure_never_breaks_the_request(self):
        from pipeline import user_activity
        with mock.patch.object(user_activity, "UserActivity", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):   # a programming error is not swallowed (only database errors are)
                user_activity.record_activity(Session(self.engine), self.user_id, user_activity.POST_ADDED)
        from sqlalchemy.exc import OperationalError
        session = mock.MagicMock()
        session.commit.side_effect = OperationalError("x", {}, Exception("db down"))
        user_activity.record_activity(session, self.user_id, user_activity.POST_ADDED)   # a database error is logged and ignored
        session.rollback.assert_called_once()

    def test_the_admin_timeline_and_the_ranking_include_it(self):
        from api import usage_dashboard
        self.client.delete(f"/creator-profile/me/posts/{self.first_post}")
        with Session(self.engine) as db:
            d = usage_dashboard.load(db, 30, user_id=self.user_id)
            today = usage_dashboard.load(db, 30)
        self.assertEqual([a["what"] for a in d["user"]["activity"]], ["Removed an Instagram post"])
        self.assertEqual(d["user"]["activity_mix"], [{"name": "Instagram posts", "count": 1, "pct": 100.0}])
        self.assertEqual([(u["name"], u["count"], u["split"]) for u in today["activity_users"]], [("poster@example.test", 1, [("Instagram posts", 1)])])
        self.assertEqual(today["active_users"][-1]["users"], 1)   # counts as being active today
