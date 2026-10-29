"""
Run BY tests/test_runtime_role_postgres.py in a subprocess, with DATABASE_URL set to the restricted runtime role of a
disposable database and RUN_MIGRATIONS=0: starts the real FastAPI app, logs in and calls the user routes, then opens every
SQLAdmin list page. Prints one JSON object {"steps": [...], "denied": [...]} on its last line. Not a test module itself.
Usage: python tests/_runtime_smoke.py EMAIL PASSWORD BRAND_ID
"""

import json
import logging
import sys
import uuid
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

email, password, brand_id = sys.argv[1], sys.argv[2], int(sys.argv[3])
steps: list[dict] = []
denied: list[str] = []


class _Watch(logging.Handler):
    def emit(self, record):
        text = record.getMessage()
        if "permission denied" in text.lower() or "InsufficientPrivilege" in text:
            denied.append(text[:300])


logging.getLogger().addHandler(_Watch())

import api.app as api_app  # noqa: E402  (imported after the handler so import-time logging is watched too)
from fastapi.testclient import TestClient  # noqa: E402


def step(name, fn, ok=(200, 201)):
    try:
        response = fn()
        status = response.status_code
        body = response.text[:200]
        steps.append({"name": name, "status": status, "ok": status in ok, "detail": "" if status in ok else body})
        if "permission denied" in response.text.lower():
            denied.append(f"{name}: {body}")
        return response
    except Exception as exc:   # an exception that escaped the route, e.g. a database error
        message = f"{type(exc).__name__}: {exc}"
        steps.append({"name": name, "status": None, "ok": False, "detail": message[:300]})
        if "permission denied" in message.lower() or "InsufficientPrivilege" in message:
            denied.append(f"{name}: {message[:300]}")
        return None


RATE = {"rate_min": 100, "rate_typical": 200, "rate_max": 300, "currency": "USD", "confidence": "low", "basis": "b", "adjustments": [], "reasoning": "r"}
CONTRACT = {"looks_good": True, "issues": [], "deal_comparison": [], "summary": "s"}

with TestClient(api_app.app, raise_server_exceptions=True) as client:   # the lifespan runs: RUN_MIGRATIONS=0 means no DDL
    step("health", lambda: client.get("/health"))
    step("brand-niches (the deploy health probe: a real database read)", lambda: client.get("/brand-niches"))
    step("login", lambda: client.post("/auth/login", json={"email": email, "password": password}))
    step("auth/me", lambda: client.get("/auth/me"))
    step("creator-profile/me", lambda: client.get("/creator-profile/me"))
    step("dashboard/me", lambda: client.get("/dashboard/me"))
    step("saved-brands save", lambda: client.post("/saved-brands", json={"brand_id": brand_id}))
    step("saved-brands list", lambda: client.get("/saved-brands/me"))
    step("saved-brands delete", lambda: client.delete(f"/saved-brands/{brand_id}"))
    created = step("pitches manual", lambda: client.post("/pitches/manual", json={"brand_id": brand_id, "sent_at": str(date.today())}))
    step("pitches list", lambda: client.get("/pitches/me"))
    if created is not None and created.status_code in (200, 201):
        step("pitches delete", lambda: client.delete(f"/pitches/{created.json()['id']}"))
    step("matches/me", lambda: client.get("/matches/me"))
    step("rate-intelligence latest", lambda: client.get("/rate-intelligence/latest"))
    with mock.patch("pipeline.rate_intelligence.call_gpt_json", return_value=RATE):
        step("rate-intelligence", lambda: client.post("/rate-intelligence", json={"brand_id": brand_id, "deliverable_type": "Reel"}))
    with mock.patch("pipeline.contract_advice.call_gpt_json", return_value=CONTRACT):
        step("contract-advice", lambda: client.post("/contract-advice", json={"contract_text": "The creator grants rights.", "brand_name": "X"}))

    # usage tracking, as the LLM and Apify helpers do it
    from pipeline.helpers import gpt_llm
    with gpt_llm.llm_usage_scope(int(client.get("/auth/me").json()["id"]), "smoke"):
        gpt_llm._record_usage("/v1/chat", {"model": "gpt-6-luna"}, {"usage": {}}, "smoke", 0.1)
        gpt_llm.record_apify_scrape("instagram", "someone", 5, 5)

    # operator endpoints: refused without the key, working with it, as the restricted database role
    import os
    operator_key = os.environ["ADMIN_PASSKEY"]
    step("operator bulk-add without a key is refused", lambda: client.post("/content-creator-re/bulk-add", json={"urls": ["https://www.instagram.com/operator.smoke/"], "niche": "music"}), ok=(401,))
    step("operator bulk-add with a wrong key is refused", lambda: client.post("/content-creator-re/bulk-add", json={"urls": ["https://www.instagram.com/operator.smoke/"], "niche": "music"}, headers={"Authorization": "Bearer nope"}), ok=(401,))
    step("operator bulk-add with the key", lambda: client.post("/content-creator-re/bulk-add", json={"urls": ["https://www.instagram.com/operator.smoke/"], "niche": "music"}, headers={"Authorization": f"Bearer {operator_key}"}))
    step("operator status route without a key is refused", lambda: client.get("/seed/status/abcd1234"), ok=(401,))
    step("operator status route with the key (unknown job)", lambda: client.get("/seed/status/abcd1234", headers={"Authorization": f"Bearer {operator_key}"}), ok=(404,))

    # SQLAdmin is closed until the passkey is entered on its login form
    anonymous = TestClient(api_app.app, follow_redirects=False)
    step("admin is closed without a login", lambda: anonymous.get("/admin/prompt/list"), ok=(302,))
    step("admin login with a wrong passkey does not open it", lambda: anonymous.post("/admin/login", data={"username": "", "password": "wrong"}), ok=(400,))
    step("admin still closed after the wrong passkey", lambda: anonymous.get("/admin/prompt/list"), ok=(302,))
    step("admin login", lambda: client.post("/admin/login", data={"username": "", "password": operator_key}, follow_redirects=False), ok=(302,))

    # every SQLAdmin list page, including the read-only view page
    for view in api_app.admin.views:
        if getattr(view, "model", None) is not None:
            step(f"admin list {view.identity}", lambda v=view: client.get(f"/admin/{v.identity}/list"))

    # ---- SQLAdmin create / edit / delete through its real HTTP forms, as the runtime role ----------------------------------
    from pipeline.db import BrandRaw, ContentCreatorRE, CreatorInstagramPost, EarlyAccessSignup, LlmUsage, OwnBrandJob, Prompt, SessionLocal

    def check(name, condition, detail=""):
        steps.append({"name": name, "status": None, "ok": bool(condition), "detail": "" if condition else str(detail)[:300]})

    me = client.get("/auth/me").json()
    with SessionLocal() as db:
        check("operator bulk-add wrote its row", db.query(ContentCreatorRE).filter(ContentCreatorRE.niche == "music").count() >= 1)
        # prompts: create -> edit -> delete (a table with full admin rights)
        step("admin create prompt", lambda: client.post("/admin/prompt/create", data={"name": "admin_smoke_prompt", "content": "v1"}, follow_redirects=False), ok=(302,))
        row = db.query(Prompt).filter(Prompt.name == "admin_smoke_prompt").first()
        check("admin create prompt: row exists", row is not None)
        if row is not None:
            prompt_id = row.id
            step("admin edit prompt", lambda: client.post(f"/admin/prompt/edit/{prompt_id}", data={"name": "admin_smoke_prompt", "content": "v2"}, follow_redirects=False), ok=(302,))
            db.expire_all()
            check("admin edit prompt: content changed", db.get(Prompt, prompt_id).content == "v2")
            step("admin delete prompt", lambda: client.request("DELETE", "/admin/prompt/delete", params={"pks": str(prompt_id)}))
            db.expire_all()
            check("admin delete prompt: row gone", db.get(Prompt, prompt_id) is None)

        # brands_raw: a big table with a sequence
        step("admin create brand-raw", lambda: client.post("/admin/brand-raw/create", data={"name": "Admin Smoke Brand"}, follow_redirects=False), ok=(302,))
        brand = db.query(BrandRaw).filter(BrandRaw.name == "Admin Smoke Brand").first()
        check("admin create brand-raw: row exists", brand is not None)
        if brand is not None:
            new_brand_id = brand.id
            step("admin edit brand-raw", lambda: client.post(f"/admin/brand-raw/edit/{new_brand_id}", data={"name": "Admin Smoke Brand 2"}, follow_redirects=False), ok=(302,))
            db.expire_all()
            check("admin edit brand-raw: name changed", db.get(BrandRaw, new_brand_id).name == "Admin Smoke Brand 2")
            step("admin delete brand-raw", lambda: client.request("DELETE", "/admin/brand-raw/delete", params={"pks": str(new_brand_id)}))
            db.expire_all()
            check("admin delete brand-raw: row gone", db.get(BrandRaw, new_brand_id) is None)

        # early_access_signups: create disabled, edit + delete enabled
        step("public early-access sign-up", lambda: client.post("/early-access", json={"email": "admin-smoke@example.com", "handle": "smoke", "platform": "instagram", "niche": "beauty", "followers": "under10k"}))
        signup = db.query(EarlyAccessSignup).filter(EarlyAccessSignup.email == "admin-smoke@example.com").first()
        check("sign-up row exists", signup is not None)
        step("admin create early-access-signup is refused", lambda: client.get("/admin/early-access-signup/create"), ok=(403,))
        if signup is not None:
            signup_id, signup_email = signup.id, signup.email
            step("admin edit early-access-signup", lambda: client.post(f"/admin/early-access-signup/edit/{signup_id}", data={
                "email": signup_email, "handle": "edited", "platform": "instagram", "niche": "beauty", "followers": "under10k"}, follow_redirects=False), ok=(302,))
            db.expire_all()
            check("admin edit early-access-signup: handle changed", db.get(EarlyAccessSignup, signup_id).handle == "edited")
            step("admin delete early-access-signup", lambda: client.request("DELETE", "/admin/early-access-signup/delete", params={"pks": str(signup_id)}))
            db.expire_all()
            check("admin delete early-access-signup: row gone", db.get(EarlyAccessSignup, signup_id) is None)

        # creator_instagram_posts: delete only
        post = CreatorInstagramPost(creator_profile_id=me["id"], instagram_handle="smoke", post_id="admin-smoke-post")
        db.add(post)
        db.commit()
        post_id = post.id
        step("admin create creator-instagram-post is refused", lambda: client.get("/admin/creator-instagram-post/create"), ok=(403,))
        step("admin edit creator-instagram-post is refused", lambda: client.get(f"/admin/creator-instagram-post/edit/{post_id}"), ok=(403,))
        step("admin delete creator-instagram-post", lambda: client.request("DELETE", "/admin/creator-instagram-post/delete", params={"pks": str(post_id)}))
        db.expire_all()
        check("admin delete creator-instagram-post: row gone", db.get(CreatorInstagramPost, post_id) is None)

        # llm_usage: read-only in the admin (and insert-only for the role)
        usage = db.query(LlmUsage).first()
        check("llm_usage has a row to protect", usage is not None)
        if usage is not None:
            usage_id = usage.id
            step("admin create llm-usage is refused", lambda: client.get("/admin/llm-usage/create"), ok=(403,))
            step("admin edit llm-usage is refused", lambda: client.get(f"/admin/llm-usage/edit/{usage_id}"), ok=(403,))
            step("admin delete llm-usage is refused", lambda: client.request("DELETE", "/admin/llm-usage/delete", params={"pks": str(usage_id)}), ok=(403,))
            db.expire_all()
            check("llm-usage row still there", db.get(LlmUsage, usage_id) is not None)

        # the read-only SQL view
        step("admin details creator-llm-cost", lambda: client.get(f"/admin/creator-llm-cost/details/{me['id']}"))
        step("admin create creator-llm-cost is refused", lambda: client.get("/admin/creator-llm-cost/create"), ok=(403,))
        step("admin delete creator-llm-cost is refused", lambda: client.request("DELETE", "/admin/creator-llm-cost/delete", params={"pks": str(me["id"])}), ok=(403,))

        # own_brand_jobs: delete only (a uuid key)
        job = OwnBrandJob(id=str(uuid.uuid4()), creator_profile_id=me["id"], instagram_handle="smoke", status="done")
        db.add(job)
        db.commit()
        job_id = job.id
        step("admin create own-brand-job is refused", lambda: client.get("/admin/own-brand-job/create"), ok=(403,))
        step("admin delete own-brand-job", lambda: client.request("DELETE", "/admin/own-brand-job/delete", params={"pks": job_id}))
        db.expire_all()
        check("admin delete own-brand-job: row gone", db.get(OwnBrandJob, job_id) is None)

print(json.dumps({"steps": steps, "denied": denied}))
