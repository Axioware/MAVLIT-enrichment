"""The admin pages of the competitor tables (Competitor Creators, their posts, and which user added which) open and are read only."""
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

IDENTITIES = ("competitor-creator", "competitor-creator-post", "user-competitor")


class TheViewsAreReadOnly(unittest.TestCase):
    def test_the_three_pages_exist_and_cannot_create_edit_delete_or_export(self):
        import api.app as api_app
        from pipeline import db_grants
        views = {v.identity: v for v in api_app.admin.views}
        for identity in IDENTITIES:
            self.assertIn(identity, views)
            view = views[identity]
            self.assertEqual((view.can_create, view.can_edit, view.can_delete, view.can_export), (False, False, False, False), identity)
        for table in ("competitor_creators", "competitor_creator_posts", "creator_competitors"):
            self.assertEqual(db_grants.ADMIN[table], (False, False, False), table)   # the grants plan says the same


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class ThePagesOpen(unittest.TestCase):
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

    def test_the_lists_and_details_of_a_competitor_load(self):
        import api.app as api_app
        from fastapi.testclient import TestClient
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE competitor_creator_posts, creator_competitors, competitor_creators, brands_raw, creator_profiles RESTART IDENTITY CASCADE"))
            conn.execute(text("INSERT INTO creator_profiles (id, email) VALUES (1, 'a@example.test')"))
            conn.execute(text("INSERT INTO brands_raw (id, instagram_handle) VALUES (1, 'https://www.instagram.com/nike')"))
            conn.execute(text("""INSERT INTO competitor_creators (id, instagram_handle, source, status, full_name, followers_count, engagement_rate, content_categories, partnership_brands)
                                 VALUES (1, 'rivalcreator', 'apify', 'ready', 'Rival R', 8000, 5.5, CAST('[{"name": "Tutorial", "pct": 60}]' AS jsonb), CAST('[{"handle": "nike", "posts": 2}]' AS jsonb))"""))
            conn.execute(text("INSERT INTO competitor_creator_posts (id, competitor_creator_id, post_id, post_url, likes_count, brand_raw_id, brand_handle) VALUES (1, 1, 'p1', 'https://www.instagram.com/p/AAA/', 400, 1, 'nike')"))
            conn.execute(text("INSERT INTO creator_competitors (creator_profile_id, competitor_creator_id) VALUES (1, 1)"))
        patchers = [mock.patch.object(api_app, "operator_configured", lambda: True), mock.patch.object(api_app, "passkey_matches", lambda v: True),
                    mock.patch.object(api_app, "passkey", lambda: "p" * 40)]
        for view in api_app.admin.views:   # the admin pages open their own connections: point them at the test database
            if getattr(view, "model", None) is not None:
                patchers.append(mock.patch.object(view, "session_maker", sessionmaker(self.engine), create=True))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        client = TestClient(api_app.app, base_url="https://testserver", follow_redirects=False)
        self.assertEqual(client.post("/admin/login", data={"username": "", "password": "x"}).status_code, 302)
        for path in ("/admin/competitor-creator/list", "/admin/competitor-creator/details/1", "/admin/competitor-creator-post/list", "/admin/competitor-creator-post/details/1",
                     "/admin/user-competitor/list", "/admin/user-competitor/details/1;1"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, f"{path}: {response.status_code}")
        self.assertIn("rivalcreator", client.get("/admin/competitor-creator/list").text)
        self.assertIn("https://www.instagram.com/p/AAA/", client.get("/admin/competitor-creator-post/list").text)
        for path in ("/admin/competitor-creator/create", "/admin/competitor-creator/edit/1", "/admin/competitor-creator-post/edit/1", "/admin/user-competitor/create"):
            self.assertEqual(client.get(path).status_code, 403, path)                    # read only
        self.assertEqual(client.delete("/admin/competitor-creator/delete?pks=1").status_code, 403)


if __name__ == "__main__":
    unittest.main()
