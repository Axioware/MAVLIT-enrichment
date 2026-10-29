"""
Access control for creator profiles and brand contacts (checklist 2.14): two creators log in through POST /auth/login and must not
reach each other's data; brand contacts are shown only for saved / matched / own brands and under a daily cap (counted in the
database); logins are rate limited and locked out per email and per client address.

The route tests need a disposable local PostgreSQL (MAVLIT_TEST_PG_URL, same rules as tests/test_migration_postgres.py): the schema is
rebuilt from the migrations. The client-address, admin-view and setting tests contact nothing.

Run:  python -m pytest tests/test_access_control.py -q
"""

import contextlib
import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TEST_URL = os.environ.get("MAVLIT_TEST_PG_URL")
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"
os.environ["JWT_SECRET"] = os.environ.get("JWT_SECRET") or "test-jwt-secret"
os.environ["RUN_MIGRATIONS"] = "0"
os.environ.pop("MIGRATION_DATABASE_URL", None)
os.environ.pop("CONTACT_REVEALS_PER_DAY", None)
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from api import client_ip as client_ip_module, contact_access  # noqa: E402

PASSWORD = "correct horse battery"


class _Request:
    def __init__(self, peer, headers=None):
        self.client = mock.Mock(host=peer) if peer else None
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


class ClientAddress(unittest.TestCase):
    def test_a_header_is_ignored_unless_the_peer_is_a_trusted_proxy(self):
        spoofed = {"CF-Connecting-IP": "9.9.9.9", "X-Forwarded-For": "8.8.8.8"}
        self.assertEqual(client_ip_module.client_ip(_Request("203.0.113.7", spoofed)), "203.0.113.7")
        self.assertEqual(client_ip_module.client_ip(_Request("203.0.113.7", {"X-Forwarded-For": "8.8.8.8"})), "203.0.113.7")

    def test_cloudflare_peers_are_believed(self):
        self.assertEqual(client_ip_module.client_ip(_Request("104.16.1.1", {"CF-Connecting-IP": "198.51.100.9"})), "198.51.100.9")
        self.assertEqual(client_ip_module.client_ip(_Request("2606:4700::1", {"CF-Connecting-IP": "2001:db8::5"})), "2001:db8::5")
        self.assertEqual(client_ip_module.client_ip(_Request("104.16.1.1", {"X-Forwarded-For": "8.8.8.8"})), "104.16.1.1")

    def test_a_garbage_header_from_cloudflare_falls_back_to_the_peer(self):
        self.assertEqual(client_ip_module.client_ip(_Request("104.16.1.1", {"CF-Connecting-IP": "not an ip, 1.2.3.4"})), "104.16.1.1")

    def test_own_proxies_can_be_added(self):
        request = _Request("127.0.0.1", {"CF-Connecting-IP": "198.51.100.9"})
        self.assertEqual(client_ip_module.client_ip(request), "127.0.0.1")
        with mock.patch.dict(os.environ, {"TRUSTED_PROXY_CIDRS": "127.0.0.1/32, 10.0.0.0/8"}):
            self.assertEqual(client_ip_module.client_ip(request), "198.51.100.9")
            self.assertEqual(client_ip_module.client_ip(_Request("10.1.2.3", {"CF-Connecting-IP": "198.51.100.10"})), "198.51.100.10")

    def test_no_peer(self):
        self.assertEqual(client_ip_module.client_ip(_Request(None)), "unknown")


class Settings(unittest.TestCase):
    def test_daily_cap(self):
        self.assertEqual(contact_access.daily_cap(), 50)
        for value, expected in (("7", 7), ("0", 0), (" 12 ", 12)):
            with mock.patch.dict(os.environ, {"CONTACT_REVEALS_PER_DAY": value}):
                self.assertEqual(contact_access.daily_cap(), expected)
        for bad in ("many", "-1", "1.5"):
            with mock.patch.dict(os.environ, {"CONTACT_REVEALS_PER_DAY": bad}), self.assertRaises(ValueError):
                contact_access.daily_cap()


class AdminViews(unittest.TestCase):
    def test_creator_and_contact_views_hide_secrets_and_cannot_export(self):
        import api.app as api_app
        views = {v.__class__.__name__: v for v in api_app.admin.views}
        creators, contacts = views["CreatorProfileAdmin"], views["BrandContactAdmin"]
        for name in ("password_hash", "embedding"):
            self.assertNotIn(name, creators._list_prop_names)
            self.assertNotIn(name, creators._details_prop_names)
            self.assertNotIn(name, creators._export_prop_names)
        self.assertFalse(creators.can_export)
        self.assertFalse(creators.can_delete)
        self.assertFalse(contacts.can_export)
        self.assertTrue(creators.can_create and creators.can_edit)   # accounts are still provisioned and edited there


@unittest.skipUnless(TEST_URL, "set MAVLIT_TEST_PG_URL to a disposable local PostgreSQL database to run these tests")
class TwoCreators(unittest.TestCase):
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

    # -- fixtures --------------------------------------------------------------------------------------------------------
    def setUp(self):
        import api.app as api_app
        import api.pitches as api_pitches
        from fastapi.testclient import TestClient
        from pipeline.db import get_db
        self.api_app = api_app
        self.TestClient = TestClient
        engine = self.engine

        def session_factory(*a, **k):
            return Session(bind=engine, autoflush=False, expire_on_commit=False)

        def override():
            with session_factory() as session:
                yield session

        api_app.app.dependency_overrides[get_db] = override
        self.addCleanup(api_app.app.dependency_overrides.pop, get_db, None)
        patcher = mock.patch.object(api_pitches, "SessionLocal", session_factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = session_factory
        self._seed()

    def _seed(self):
        from pipeline.db import (BrandContact, BrandRaw, CreatorBrandLlmV3Ranking, CreatorInstagramPost, CreatorProfile, OwnBrandJob,
                                 Pitch, SavedBrand)
        from pipeline.helpers.passwords import hash_password
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE creator_profiles, brands_raw, login_failures RESTART IDENTITY CASCADE"))
        pw_hash = hash_password(PASSWORD)

        def creator(email, name, complete=True):
            fields = dict(email=email, password_hash=pw_hash, is_active=True, full_name=name)
            if complete:
                fields.update(age=30, gender="female", primary_platform="instagram", follower_count=20000, content_niche="Beauty",
                              instagram_handle=name.lower(), instagram_followers=20000, instagram_description="I make beauty videos.")
            return CreatorProfile(**fields)

        with self.session() as s:
            self.a, self.b, self.c, self.incomplete = (creator("a@example.test", "Alice"), creator("b@example.test", "Bob"),
                                                       creator("c@example.test", "Cara"), creator("d@example.test", "Dan", complete=False))
            s.add_all([self.a, self.b, self.c, self.incomplete])
            s.add_all([BrandRaw(name=f"Brand {i}") for i in range(1, 6)])
            s.flush()
            for i in range(1, 6):   # one enriched contact per brand, with a marker no other field has
                s.add(BrandContact(brand_raw_id=i, full_name=f"Zorblax{i} Contact", email=f"contact{i}@brand.test", title="Head of Marketing",
                                   is_enriched=True, still_at_brand=True, email_status="verified", rank=1))
            s.add_all([
                SavedBrand(creator_profile_id=self.a.id, brand_raw_id=1),                                  # A saved brand 1
                CreatorBrandLlmV3Ranking(creator_profile_id=self.a.id, brand_raw_id=2, confidence=90, rank=1),   # A is matched to brand 2
                OwnBrandJob(id="job-a", creator_profile_id=self.a.id, brand_raw_id=4, instagram_handle="abrand", status="ready"),  # A's own brand
                SavedBrand(creator_profile_id=self.b.id, brand_raw_id=3),                                  # only B saved brand 3
                OwnBrandJob(id="job-b", creator_profile_id=self.b.id, brand_raw_id=5, instagram_handle="bbrand", status="ready"),
                SavedBrand(creator_profile_id=self.c.id, brand_raw_id=1),
                SavedBrand(creator_profile_id=self.c.id, brand_raw_id=2),
                SavedBrand(creator_profile_id=self.c.id, brand_raw_id=3),
            ])
            self.pitch_a = Pitch(creator_profile_id=self.a.id, brand_name="Brand 1", story="a", pitch_text="pitch of alice", status="generated")
            self.pitch_b = Pitch(creator_profile_id=self.b.id, brand_name="Brand 3", story="b", pitch_text="pitch of bob", status="generated")
            s.add_all([self.pitch_a, self.pitch_b])
            self.post_b = CreatorInstagramPost(creator_profile_id=self.b.id, instagram_handle="bob", post_id="bobpost", post_url="https://www.instagram.com/p/BOBPOST/")
            s.add(self.post_b)
            s.commit()

    def login(self, email, password=PASSWORD):
        client = self.TestClient(self.api_app.app, raise_server_exceptions=False)
        response = client.post("/auth/login", json={"email": email, "password": password})
        return client, response

    def client_for(self, email):
        client, response = self.login(email)
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def count(self, sql, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    # -- profile ---------------------------------------------------------------------------------------------------------
    def test_me_and_profile_return_only_own_data(self):
        a = self.client_for("a@example.test")
        for path in ("/auth/me", "/creator-profile/me"):
            body = a.get(path)
            self.assertEqual(body.status_code, 200)
            self.assertEqual((body.json()["id"], body.json()["email"]), (self.a.id, "a@example.test"))
            for secret in ("password_hash", "b@example.test", "c@example.test", "embedding"):
                self.assertNotIn(secret, body.text)
        login = self.login("a@example.test")[1]
        self.assertNotIn("password_hash", login.text)

    def test_unauthenticated_and_incomplete_profiles_get_no_brand_detail(self):
        self.assertEqual(self.TestClient(self.api_app.app).get("/brands/1").status_code, 401)
        incomplete = self.client_for("d@example.test")
        self.assertEqual(incomplete.get("/brands/1").status_code, 403)

    # -- ownership -------------------------------------------------------------------------------------------------------
    def test_a_cannot_touch_bs_pitch_posts_or_own_brand_job(self):
        a = self.client_for("a@example.test")
        foreign = a.patch(f"/pitches/{self.pitch_b.id}", json={"pitch_text": "hacked"})
        missing = a.patch("/pitches/999999", json={"pitch_text": "hacked"})
        self.assertEqual((foreign.status_code, missing.status_code), (404, 404))
        self.assertEqual(foreign.json(), missing.json(), "a foreign pitch id must look exactly like a missing one")
        self.assertEqual(a.delete(f"/pitches/{self.pitch_b.id}").status_code, 404)
        self.assertEqual(a.get("/own-brands/jobs/job-b").status_code, 404)
        self.assertEqual(a.get("/own-brands/jobs/job-b").json(), a.get("/own-brands/jobs/nope").json())
        base = f"/creator-profile/me/posts/{self.post_b.id}"
        self.assertEqual(a.patch(base, json={"is_partnership_post": True}).status_code, 404)
        self.assertEqual(a.delete(base).status_code, 404)
        self.assertEqual(a.get(base + "/thumbnail").status_code, 404)
        # nothing of B's changed
        self.assertEqual(self.count("SELECT pitch_text FROM pitches WHERE id = :i", i=self.pitch_b.id), "pitch of bob")
        self.assertEqual(self.count("SELECT count(*) FROM creator_instagram_posts WHERE id = :i", i=self.post_b.id), 1)
        # B still can
        b = self.client_for("b@example.test")
        self.assertEqual(b.patch(f"/pitches/{self.pitch_b.id}", json={"pitch_text": "edited by bob"}).status_code, 200)
        self.assertEqual(b.get("/own-brands/jobs/job-b").status_code, 200)

    def test_lists_never_contain_the_other_creators_rows(self):
        a = self.client_for("a@example.test")
        pitches = a.get("/pitches/me").json()
        self.assertEqual([p["id"] for p in pitches["pitches"]], [self.pitch_a.id])
        self.assertNotIn("pitch of bob", json.dumps(pitches))
        saved = a.get("/saved-brands/me").json()
        self.assertEqual([s["brand_id"] for s in saved], [1])
        self.assertEqual([s["brand_id"] for s in self.client_for("b@example.test").get("/saved-brands/me").json()], [3])

    # -- contacts --------------------------------------------------------------------------------------------------------
    def test_contacts_only_for_saved_matched_or_own_brands(self):
        a = self.client_for("a@example.test")
        for brand_id, allowed in ((1, True), (2, True), (4, True), (3, False), (5, False)):
            with self.subTest(brand_id=brand_id):
                body = a.get(f"/brands/{brand_id}")
                self.assertEqual(body.status_code, 200)
                if allowed:
                    self.assertEqual(body.json()["top_contact"]["email"], f"contact{brand_id}@brand.test")
                else:
                    self.assertIsNone(body.json()["top_contact"])
                    self.assertEqual(body.json()["other_contacts"], [])
                    self.assertNotIn(f"contact{brand_id}@brand.test", body.text)
                    self.assertNotIn(f"Zorblax{brand_id}", body.text)
        # unentitled brands cost nothing against the cap
        self.assertEqual(self.count("SELECT count(*) FROM contact_reveals WHERE creator_profile_id = :i", i=self.a.id), 3)

    def test_the_daily_cap_is_enforced_and_kept_in_the_database(self):
        c = self.client_for("c@example.test")
        with mock.patch.dict(os.environ, {"CONTACT_REVEALS_PER_DAY": "2"}):
            self.assertEqual(c.get("/brands/1").status_code, 200)
            self.assertEqual(c.get("/brands/2").status_code, 200)
            over = c.get("/brands/3")
            self.assertEqual(over.status_code, 429)
            self.assertIn("daily limit", over.json()["detail"])
            self.assertIn("2 different brands", over.json()["detail"])
            self.assertEqual(c.get("/brands/1").status_code, 200, "the same brand again the same day is free")
            # a brand's page without contacts is not a reveal
            self.assertEqual(self.count("SELECT count(*) FROM contact_reveals WHERE creator_profile_id = :i", i=self.c.id), 2)
            # "another process": a brand new client, nothing in memory
            fresh = self.client_for("c@example.test")
            self.assertEqual(fresh.get("/brands/3").status_code, 429)
            # a different creator is not affected
            self.assertEqual(self.client_for("a@example.test").get("/brands/1").status_code, 200)
            # tomorrow the count starts again
            with self.engine.begin() as conn:
                conn.execute(text("UPDATE contact_reveals SET revealed_on = revealed_on - 1"))
            self.assertEqual(c.get("/brands/3").status_code, 200)

    def test_a_cap_of_zero_turns_revealing_off(self):
        a = self.client_for("a@example.test")
        with mock.patch.dict(os.environ, {"CONTACT_REVEALS_PER_DAY": "0"}):
            self.assertEqual(a.get("/brands/1").status_code, 429)

    def _pitch(self, client, brand_id, llm_prompts):
        def llm(prompt, **kw):
            llm_prompts.append(prompt)
            return "Hello there"
        with mock.patch("pipeline.pitching.call_gpt_text", side_effect=llm):
            return client.post("/pitches", json={"is_custom": False, "brand_id": brand_id, "story": "my story"})

    def test_a_pitch_for_an_unentitled_brand_stores_no_contact(self):
        a, prompts = self.client_for("a@example.test"), []
        refused = self._pitch(a, 3, prompts)   # brand 3: not saved, matched or A's own
        self.assertEqual(refused.status_code, 200, "an unentitled brand is not an error")
        self.assertIsNone(refused.json()["contact_email"])
        self.assertIsNone(refused.json()["contact_name"])
        self.assertIsNone(self.count("SELECT contact_email FROM pitches WHERE id = :i", i=refused.json()["id"]))
        self.assertNotIn("Zorblax3", prompts[0], "the contact's name must not reach the prompt (and so the pitch text)")
        self.assertNotIn("contact3@brand.test", prompts[0])
        allowed = self._pitch(a, 1, prompts)
        self.assertEqual(allowed.json()["contact_email"], "contact1@brand.test")
        self.assertIn("Zorblax1", prompts[1])
        self.assertEqual(self.count("SELECT contact_email FROM pitches WHERE id = :i", i=allowed.json()["id"]), "contact1@brand.test")

    def test_a_pitch_over_the_cap_is_429_before_the_llm_runs(self):
        c, prompts = self.client_for("c@example.test"), []
        with mock.patch.dict(os.environ, {"CONTACT_REVEALS_PER_DAY": "1"}):
            self.assertEqual(self._pitch(c, 1, prompts).status_code, 200)
            over = self._pitch(c, 2, prompts)
        self.assertEqual(over.status_code, 429)
        self.assertEqual(len(prompts), 1, "no LLM call (and no cost) for a refused pitch")

    def test_the_streamed_pitch_follows_the_same_rule(self):
        a = self.client_for("a@example.test")

        def run(brand_id):
            with mock.patch("pipeline.pitching.stream_gpt_text", side_effect=lambda *x, **k: iter(["Hel", "lo"])):
                response = a.post("/pitches/stream", json={"is_custom": False, "brand_id": brand_id, "story": "s"})
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            return response, next(e for e in events if e["type"] == "done")

        response, done = run(3)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(done["pitch"]["contact_email"])
        _, done = run(1)
        self.assertEqual(done["pitch"]["contact_email"], "contact1@brand.test")
        with mock.patch.dict(os.environ, {"CONTACT_REVEALS_PER_DAY": "1"}):   # brand 1 is already counted today; brand 2 would be the 2nd
            with mock.patch("pipeline.pitching.stream_gpt_text", side_effect=lambda *x, **k: iter(["x"])):
                self.assertEqual(a.post("/pitches/stream", json={"is_custom": False, "brand_id": 2, "story": "s"}).status_code, 429)

    def test_a_manual_pitch_never_carries_a_contact(self):
        a = self.client_for("a@example.test")
        for brand_id in (1, 3):
            body = a.post("/pitches/manual", json={"brand_id": brand_id, "status": "sent", "sent_at": datetime.now(timezone.utc).date().isoformat()})
            self.assertEqual(body.status_code, 200, body.text)
            self.assertIsNone(body.json()["contact_email"])

    # -- login -----------------------------------------------------------------------------------------------------------
    def test_a_failed_login_logs_and_prints_no_email(self):
        stdout = io.StringIO()
        with self.assertLogs(level="DEBUG") as logs, contextlib.redirect_stdout(stdout):   # every logger; the libraries' own lines are fine
            client, response = self.login("a@example.test", "wrong password")
            unknown = self.login("nobody@example.test", "whatever")[1]
        self.assertNotIn("example.test", "\n".join(logs.output))
        self.assertEqual((response.status_code, unknown.status_code), (401, 401))
        self.assertEqual(response.json(), unknown.json(), "an unknown email answers exactly like a wrong password")
        self.assertNotIn("example.test", stdout.getvalue())
        self.assertNotIn("example.test", response.text)
        with self.engine.connect() as conn:   # the lockout table keeps a hash, not the address
            rows = conn.execute(text("SELECT email_hash, ip FROM login_failures")).all()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("example.test", " ".join(f"{h} {i}" for h, i in rows))

    def test_an_unknown_email_still_costs_one_password_check(self):
        import api.auth as auth
        with mock.patch.object(auth, "verify_password", wraps=auth.verify_password) as verify:
            self.login("nobody@example.test", "whatever")
        self.assertEqual(verify.call_count, 1)
        self.assertTrue(verify.call_args.args[1].startswith("$2"), "checked against a real bcrypt hash")

    def test_repeated_failures_lock_the_email_out_even_for_the_right_password(self):
        for attempt in range(5):
            self.assertEqual(self.login("a@example.test", "bad")[1].status_code, 401, attempt)
        locked = self.login("a@example.test", PASSWORD)[1]
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Try again in", locked.json()["detail"])
        self.assertGreater(int(locked.headers["retry-after"]), 0)
        self.assertEqual(self.login("c@example.test")[1].status_code, 200, "another email is not locked")
        # the lock lives in the database: it survives a new client and a new "process"
        self.assertEqual(self.login("a@example.test", PASSWORD)[1].status_code, 429)
        # an unknown email locks the same way (no way to tell which emails exist)
        for _ in range(5):
            self.login("nobody@example.test", "bad")
        self.assertEqual(self.login("nobody@example.test", "bad")[1].status_code, 429)
        # after the window the right password works again, and a success clears the failures
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE login_failures SET created_at = created_at - interval '20 minutes'"))
        self.assertEqual(self.login("a@example.test", PASSWORD)[1].status_code, 200)
        self.assertEqual(self.count("SELECT count(*) FROM login_failures WHERE email_hash = :h", h=__import__("api.login_limits", fromlist=["x"]).email_hash("a@example.test")), 0)

    def test_failures_below_the_limit_are_forgiven_by_a_success(self):
        for _ in range(4):
            self.login("a@example.test", "bad")
        self.assertEqual(self.login("a@example.test", PASSWORD)[1].status_code, 200)
        for _ in range(4):   # the count starts again from zero
            self.assertEqual(self.login("a@example.test", "bad")[1].status_code, 401)

    def test_one_client_address_failing_for_many_emails_is_locked(self):
        from api import login_limits
        with self.session() as s:
            for i in range(login_limits.MAX_FAILURES_PER_IP):
                login_limits.record_failure(s, f"user{i}@example.test", "testclient")
        self.assertEqual(self.login("c@example.test")[1].status_code, 429)

    def test_a_spoofed_forwarded_header_does_not_change_the_counted_address(self):
        client = self.TestClient(self.api_app.app, raise_server_exceptions=False)
        for i in range(3):
            client.post("/auth/login", json={"email": f"x{i}@example.test", "password": "bad"},
                        headers={"X-Forwarded-For": f"10.0.0.{i}", "CF-Connecting-IP": f"10.0.1.{i}"})
        with self.engine.connect() as conn:
            self.assertEqual({r[0] for r in conn.execute(text("SELECT ip FROM login_failures"))}, {"testclient"})

    def test_early_access_limit_ignores_a_spoofed_forwarded_header(self):
        import api.early_access as early
        early._recent.clear()
        client = self.TestClient(self.api_app.app, raise_server_exceptions=False)
        body = {"email": "x@example.test", "handle": "x", "platform": "instagram", "niche": "beauty", "followers": "under10k"}
        statuses = [client.post("/early-access", json=body, headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(12)]
        self.assertEqual(statuses[:10], [201] * 10)
        self.assertEqual(statuses[10:], [429, 429])
        early._recent.clear()


if __name__ == "__main__":
    unittest.main()
