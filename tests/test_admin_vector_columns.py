"""
An admin page must never try to show a pgvector `embedding` column: sqladmin does `if value and isinstance(value, Enum)` on every cell, which raises "The truth
value of an array ... is ambiguous" (HTTP 500) for any row that has an embedding. The static test covers every admin view; the database test opens the pages
of a row that really has an embedding. (/admin/creator-niche/list was returning Internal Server Error before the column was left out.)
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")

from pgvector.sqlalchemy import Vector  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402


def vector_columns(model) -> set[str]:
    return {c.name for c in model.__table__.columns if isinstance(c.type, Vector)}


class NoAdminPageShowsAnEmbedding(unittest.TestCase):
    def test_every_view_leaves_vector_columns_out_of_list_details_sorting_and_forms(self):
        import api.app as api_app
        checked = []
        for view in api_app.admin.views:
            model = getattr(view, "model", None)
            vectors = vector_columns(model) if model is not None else set()
            if not vectors:
                continue
            checked.append(view.identity)
            for name in vectors:
                self.assertNotIn(name, view._list_prop_names, f"{view.identity}: the list page would show {name}")
                self.assertNotIn(name, view._details_prop_names, f"{view.identity}: the detail page would show {name}")
                self.assertNotIn(name, view._export_prop_names, f"{view.identity}: an export would include {name}")
                self.assertNotIn(name, [getattr(c, "key", c) for c in view.column_sortable_list], f"{view.identity}: sortable by {name}")
                self.assertNotIn(name, view._form_prop_names, f"{view.identity}: the form would show {name}")
        self.assertEqual(sorted(checked), ["brand-profile", "creator-niche", "creator-profile"])   # the three models that have an embedding


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class RowsWithEmbeddingsOpen(unittest.TestCase):
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

    def test_the_list_and_detail_pages_of_a_row_with_an_embedding_load(self):
        import api.app as api_app
        from fastapi.testclient import TestClient
        vec = "[" + ",".join(["0.1"] * 1024) + "]"
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, brands_raw, instagram_users, creator_niches, brand_match_profile RESTART IDENTITY CASCADE"))
            conn.execute(text("INSERT INTO instagram_users (id, username, user_type) VALUES (1, 'u1', 'contentcreatorRE')"))
            conn.execute(text("INSERT INTO creator_niches (id, instagram_user_id, username, niche, description, embedding) VALUES (1, 1, 'u1', 'Music', 'd', CAST(:v AS vector))"), {"v": vec})
            conn.execute(text("INSERT INTO creator_profiles (id, email, password_hash, is_active, embedding) VALUES (1, 'a@example.test', 'x', true, CAST(:v AS vector))"), {"v": vec})
            conn.execute(text("INSERT INTO brands_raw (id, name) VALUES (1, 'Nike')"))
            conn.execute(text("INSERT INTO brand_match_profile (brand_raw_id, embedding) VALUES (1, CAST(:v AS vector))"), {"v": vec})
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
        for path in ("/admin/creator-niche/list", "/admin/creator-niche/details/1", "/admin/creator-niche/edit/1",
                     "/admin/creator-profile/list", "/admin/creator-profile/details/1",
                     "/admin/brand-profile/list", "/admin/brand-profile/details/1"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, f"{path}: {response.status_code}")
        self.assertIn("Music", client.get("/admin/creator-niche/list").text)   # and the row itself is there
