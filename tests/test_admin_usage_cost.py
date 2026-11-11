"""Admin > Usage & Cost: the page renders the logged OpenAI and Apify spend. Needs MAVLIT_TEST_PG_URL."""
import os
import re
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


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class UsageCostPage(unittest.TestCase):
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
        import pipeline.mailer as mailer
        from pipeline.db import get_db
        engine = self.engine
        factory = lambda *a, **k: Session(bind=engine, autoflush=False, expire_on_commit=False)

        def override():
            with factory() as session:
                yield session

        api_app.app.dependency_overrides[get_db] = override
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        self.sent, self.mail_error = [], None

        def fake_send(messages):
            if self.mail_error:
                raise self.mail_error
            self.sent.extend(messages)
            return len(messages)

        for patcher in (mock.patch.object(attempt_limits, "SessionLocal", factory), mock.patch.object(api_app, "SessionLocal", factory),
                        mock.patch.object(api_app, "operator_configured", lambda: True), mock.patch.object(api_app, "passkey_matches", lambda v: True),
                        mock.patch.object(api_app, "passkey", lambda: "p" * 40), mock.patch.object(mailer, "send_by_smtp", fake_send)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api_app.app, follow_redirects=False)
        self.assertEqual(self.client.post("/admin/login", data={"username": "", "password": "x"}).status_code, 302)
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, llm_usage, apify_usage, login_failures RESTART IDENTITY CASCADE"))
            conn.execute(text("INSERT INTO creator_profiles (email, password_hash, is_active) VALUES ('member@example.test','x',true)"))

    def seed(self):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO llm_usage (model, endpoint, prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens, context, cost_usd, creator_profile_id, feature, created_at) VALUES "
                              "('gpt', '/chat/completions', 0, 0, 0, 0, 'pitch', 0.5, 1, 'pitch', now()),"
                              "('script-model', '/chat/completions', 0, 0, 0, 0, 'demographics @nike', 0.25, NULL, NULL, now() - interval '2 days')"))   # an enrichment script's call
            conn.execute(text("INSERT INTO apify_usage (creator_profile_id, feature, platform, handle, posts_scraped, cost_usd) VALUES "
                              "(1, 'description', 'instagram', 'me', 20, 0.046), (NULL, NULL, 'instagram', 'nike', 40, 0.092)"))

    def test_the_page_shows_only_what_users_spent_not_the_enrichment_scripts(self):
        self.seed()
        page = self.client.get("/admin/usage-cost")
        self.assertEqual(page.status_code, 200, page.text[:300])
        for text_ in ("Usage &amp; Cost", "$0.546", "member@example.test", "Pitches", "Sign-up funnel", "Failed sign-ins"):
            self.assertIn(text_, page.text)
        for gone in ("$0.888", "script-model", "demographics", "Enrichment scripts", "nike"):   # the script's call and Apify run are not shown anywhere
            self.assertNotIn(gone, page.text)
        if os.environ.get("UC_SAVE"):
            Path(os.environ["UC_SAVE"]).write_text(page.text)

    def test_every_cost_figure_leaves_the_enrichment_scripts_out(self):
        from api import usage_dashboard
        self.seed()
        with Session(self.engine) as db:
            d = usage_dashboard.load(db, 30)
        self.assertAlmostEqual(d["cards"]["d30"], 0.546)                                      # 0.5 OpenAI + 0.046 Apify, both by the user
        self.assertAlmostEqual(sum(r["llm"] + r["apify"] for r in d["daily"]), 0.546)         # the daily bars: the same total, no script cost
        self.assertEqual([m["name"] for m in d["models"]], ["gpt"])                           # the script's model is not in the model mix
        self.assertAlmostEqual(d["models"][0]["cost"], 0.5)
        self.assertEqual([a["name"] for a in d["apify"]], ["me"])                             # the script's Apify run on "nike" is not listed
        self.assertEqual([c["context"] for c in d["expensive"]], ["pitch"])
        self.assertNotIn("steps", d)
        self.assertNotIn("split", d)

    def test_the_window_and_user_options_work(self):
        self.seed()
        for q in ("?days=7", "?days=90", "?days=999", "?user=1", "?user=999", "?user=abc"):
            self.assertEqual(self.client.get("/admin/usage-cost" + q).status_code, 200, q)

    def test_the_limits_show_up_and_there_is_no_budget_anywhere(self):
        from api import usage_dashboard
        self.seed()
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO feature_usage (creator_profile_id, feature, used_on, uses) VALUES (1, 'pitches', current_date, 29)"))
        with mock.patch.dict(os.environ, {"USAGE_MONTHLY_BUDGET_USD": "10"}):   # the old setting does nothing now
            page = self.client.get("/admin/usage-cost").text
            with Session(self.engine) as db:
                data = usage_dashboard.load(db, 30)
        self.assertIn("29 / 30", page)
        self.assertNotIn("budget", page.lower())
        self.assertFalse([k for k in data["cards"] if "budget" in k] + [k for k in data if "budget" in k])

    def test_an_empty_database_still_renders(self):
        self.assertEqual(self.client.get("/admin/usage-cost").status_code, 200)

    def test_the_page_is_behind_the_admin_login(self):
        self.assertEqual(TestClient(self.client.app, follow_redirects=False).get("/admin/usage-cost").status_code, 302)

    def test_active_users_per_day_counts_each_person_once_and_counts_every_kind_of_activity(self):
        from api import usage_dashboard
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO creator_profiles (email, password_hash, is_active) VALUES ('two@example.test','x',true), ('three@example.test','x',true)"))
            conn.execute(text("INSERT INTO llm_usage (model, endpoint, prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens, creator_profile_id, created_at) VALUES "
                              "('m','/e',0,0,0,0,1,now()), ('m','/e',0,0,0,0,1,now()), ('m','/e',0,0,0,0,NULL,now())"))   # user 1 twice today; a script call is nobody
            conn.execute(text("INSERT INTO feature_usage (creator_profile_id, feature, used_on, uses) VALUES (2, 'pitches', current_date, 1), (1, 'pitches', current_date - 1, 1)"))
            conn.execute(text("INSERT INTO contact_reveals (creator_profile_id, brand_raw_id, revealed_on) SELECT 3, id, current_date FROM (SELECT 1 AS id) b WHERE false"))
        with Session(self.engine) as db:
            rows = usage_dashboard.load(db, 7)["active_users"]
        self.assertEqual(len(rows), 7)
        today, yesterday = rows[-1], rows[-2]
        self.assertEqual((today["users"], yesterday["users"]), (2, 1))   # users 1 and 2 today; only user 1 yesterday
        self.assertEqual(today["actions"], 3)                           # user 1 twice + user 2 once (the script call is not counted)
        self.assertTrue(all(r["users"] == 0 for r in rows[:-2]))

    def test_test_accounts_and_deleted_accounts_are_left_out_of_everything(self):
        from api import usage_dashboard
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO creator_profiles (id, email, password_hash, is_active) VALUES "
                              "(2, 'abdulrauf@axioware.tech', 'x', true), (3, 'Abdulrauf_99@Outlook.com', 'x', true), (4, 'abc@gmail.com', 'x', true)"))
            for uid in (1, 2, 3, 4, 99):   # 1 = the real user; 2-4 = the owner's test accounts; 99 = an account that was deleted (no creator_profiles row)
                conn.execute(text("INSERT INTO llm_usage (model, endpoint, prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens, cost_usd, creator_profile_id, feature, created_at, duration_ms) "
                                  "VALUES (:m, '/e', 10, 5, 1, 0, 1.0, :u, 'pitch', now(), 100)"), {"m": f"model-{uid}", "u": uid})
                conn.execute(text("INSERT INTO apify_usage (creator_profile_id, feature, platform, handle, posts_scraped, cost_usd) VALUES (:u, 'description', 'instagram', :h, 10, 0.5)"),
                             {"u": uid, "h": f"handle{uid}"})
                conn.execute(text("INSERT INTO feature_usage (creator_profile_id, feature, used_on, uses) SELECT :u, 'pitches', current_date, 30 WHERE EXISTS (SELECT 1 FROM creator_profiles WHERE id = :u)"), {"u": uid})
            conn.execute(text("INSERT INTO early_access_signups (email, handle, platform, niche, followers) VALUES "
                              "('abc@gmail.com','a','instagram','music','10k-50k'), ('real@example.test','r','instagram','music','10k-50k')"))
            conn.execute(text("INSERT INTO account_invites (email, token_hash, expires_at) VALUES ('abc@gmail.com','h1', now() + interval '1 day'), ('real@example.test','h2', now() + interval '1 day')"))
        with Session(self.engine) as db:
            d = usage_dashboard.load(db, 30)
            blocked = usage_dashboard.load(db, 30, user_id=4)
        self.assertAlmostEqual(d["cards"]["d30"], 1.5)                                            # only the real user: 1.0 OpenAI + 0.5 Apify
        self.assertAlmostEqual(sum(r["llm"] + r["apify"] for r in d["daily"]), 1.5)
        self.assertEqual([m["name"] for m in d["models"]], ["model-1"])                          # test and deleted accounts' models are not in the mix
        self.assertEqual([u["name"] for u in d["users"]], ["member@example.test"])
        self.assertEqual([a["name"] for a in d["apify"]], ["handle1"])
        self.assertEqual(len(d["expensive"]) + len(d["slow"]), 2)                                 # one call each, the real user's
        self.assertEqual(d["active_users"][-1]["users"], 1)                                       # one person was active today, not five
        self.assertEqual([r["name"] for r in d["near_cap"]], ["member@example.test"])
        self.assertEqual([f["n"] for f in d["funnel"]], [1, 1, 0])                                # the test account's sign-up and invitation are not counted
        self.assertEqual(d["funnel_extra"]["accounts"], 1)
        self.assertEqual((blocked["user"]["features"], blocked["user"]["recent"]), ([], []))     # opening a test account shows nothing
        page = self.client.get("/admin/usage-cost").text
        for hidden in ("abdulrauf@axioware.tech", "abdulrauf_99", "abc@gmail.com", "model-4", "model-99", "handle2", "handle99"):
            self.assertNotIn(hidden.lower(), page.lower())
