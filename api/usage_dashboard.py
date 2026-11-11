"""
Numbers for the admin "Usage & Cost" page (api/app.py UsageDashboardAdmin, api/usage_api.py). Read-only SQL over llm_usage and apify_usage (spend),
feature_usage, contact_reveals, feature_locks, login_failures, own_brand_jobs, creator_profiles, early_access_signups and account_invites. Costs are
the logged cost_usd values (NULL counts as 0; Apify's is an estimate).

Only REAL USERS count. Everything on the page leaves out:
  - the enrichment scripts (rows with no creator_profile_id),
  - the owner's test accounts (EXCLUDED_EMAILS), and
  - accounts that no longer exist (usage rows keep the old creator id after an account is deleted, so an id that is not in creator_profiles is dropped).
"""
import calendar
from datetime import datetime, timezone

from sqlalchemy import text

DAY_CHOICES = (7, 30, 90)
SPIKE_FACTOR = 3.0         # "today" is flagged when it is this many times the average of the 7 days before
NEAR_CAP = 0.8             # a user is "near" a daily limit from this share of it

# Test accounts: left out of every figure and chart (lower case).
EXCLUDED_EMAILS = ("abdulrauf@axioware.tech", "abdulrauf_99@outlook.com", "abc@gmail.com")

# A creator_profile_id that belongs to a real, existing account (not a deleted one, not a test account). `:excl` is bound by _rows().
_REAL = "creator_profile_id IN (SELECT id FROM creator_profiles WHERE lower(email) <> ALL(:excl))"

# every OpenAI call and every Instagram scraper run that a real user caused, as one row shape
_SPEND = f"""(SELECT created_at, 'llm' AS src, creator_profile_id, feature, COALESCE(cost_usd, 0) AS cost, context AS label FROM llm_usage
              WHERE {_REAL}
              UNION ALL
              SELECT created_at, 'apify', creator_profile_id, feature, COALESCE(cost_usd, 0), label FROM apify_usage
              WHERE {_REAL})"""

# One user's activity (:i = creator id), newest first when ordered by `at`: what they did and when, from the tables that record it. The paid AI calls of a
# pitch / rate estimate / contract review are NOT listed again (the pitch, estimate and review rows already say so); matching and profile calls come in
# bursts, so they are collapsed to one entry per minute. There is no log of pages opened, only of things that were done.
_ACTIVITY = """
    SELECT DISTINCT creator_profile_id AS uid, date_trunc('minute', created_at) AS at, 'Ran AI brand matching' AS what, 'Matching' AS grp
        FROM llm_usage WHERE creator_profile_id IS NOT NULL AND feature IN ('match_v2', 'match_v3', 'match_refresh')
    UNION ALL SELECT DISTINCT creator_profile_id, date_trunc('minute', created_at), 'Generated profile description', 'Profile generation'
        FROM llm_usage WHERE creator_profile_id IS NOT NULL AND feature = 'description'
    UNION ALL SELECT DISTINCT creator_profile_id, date_trunc('minute', created_at), 'Updated profile tags', 'Profile generation'
        FROM llm_usage WHERE creator_profile_id IS NOT NULL AND feature = 'profile_signals'
    UNION ALL SELECT creator_profile_id, created_at, 'Created a pitch for ' || COALESCE(NULLIF(brand_name, ''), 'a brand'), 'Pitches'
        FROM pitches
    UNION ALL SELECT creator_profile_id, created_at, 'Estimated a rate for ' || COALESCE(NULLIF(brand_name, ''), 'a brand'), 'Rate estimates'
        FROM rate_estimates
    UNION ALL SELECT creator_profile_id, created_at, 'Reviewed a contract' || COALESCE(' with ' || NULLIF(brand_name, ''), ''), 'Contract reviews'
        FROM contract_reviews
    UNION ALL SELECT r.creator_profile_id, r.created_at, 'Opened the contacts of ' || COALESCE(b.name, 'a brand'), 'Brand contacts'
        FROM contact_reveals r LEFT JOIN brands_raw b ON b.id = r.brand_raw_id
    UNION ALL SELECT s.creator_profile_id, s.created_at, 'Saved ' || COALESCE(b.name, 'a brand'), 'Saved brands'
        FROM saved_brands s LEFT JOIN brands_raw b ON b.id = s.brand_raw_id
    UNION ALL SELECT creator_profile_id, created_at, 'Added their own brand @' || instagram_handle, 'Own brands'
        FROM own_brand_jobs
    UNION ALL SELECT creator_profile_id, created_at, detail, 'Instagram posts'
        FROM user_activity
"""

# The lines of the "feature use per day" chart, in the order the page shows them. Each activity (see _ACTIVITY) belongs to at most one line.
FEATURE_LINES = ("matches", "rate", "pitch", "description", "post_added", "own_brand", "contacts", "saved")   # removed posts are logged but not charted
_LINE_CASE = """(CASE WHEN what = 'Ran AI brand matching' THEN 'matches'
                       WHEN grp = 'Rate estimates' THEN 'rate'
                       WHEN grp = 'Pitches' THEN 'pitch'
                       WHEN what = 'Generated profile description' THEN 'description'
                       WHEN what = 'Added an Instagram post' THEN 'post_added'
                       WHEN what LIKE 'Added their own brand%' THEN 'own_brand'
                       WHEN grp = 'Brand contacts' THEN 'contacts'
                       WHEN grp = 'Saved brands' THEN 'saved' END)"""

FEATURE_GROUPS = {
    "match_v2": "Matching", "match_v3": "Matching", "match_refresh": "Matching",
    "pitch": "Pitches", "rate_estimate": "Rate estimates", "contract_advice": "Contract reviews",
    "description": "Profile generation", "profile_signals": "Profile generation",
    "own_brand": "Own brands", "own_brand_followup": "Own brands",
}


def parse_days(raw) -> int:
    return int(raw) if str(raw or "").isdigit() and int(raw) in DAY_CHOICES else 30


def _num(v) -> float:
    return float(v or 0)


def _rows(db, sql: str, **params) -> list[dict]:
    params.setdefault("excl", list(EXCLUDED_EMAILS))
    return [dict(r._mapping) for r in db.execute(text(sql), params)]


def _bars(rows: list[dict], key: str = "cost") -> list[dict]:
    top = max([_num(r[key]) for r in rows] + [1e-9])
    for r in rows:
        r[key] = _num(r[key])
        r["pct"] = round(r[key] / top * 100, 1)
    return rows


def load(db, days: int, user_id: int | None = None) -> dict:
    w = "created_at > now() - make_interval(days => :d)"
    out: dict = {"days": days, "choices": DAY_CHOICES}

    # ---- spend cards, month-end projection, spike ----
    c = _rows(db, f"""
        SELECT COALESCE(sum(cost) FILTER (WHERE created_at >= current_date), 0) AS today,
               COALESCE(sum(cost) FILTER (WHERE created_at >= current_date - 7 AND created_at < current_date), 0) / 7 AS prev7_avg,
               COALESCE(sum(cost) FILTER (WHERE created_at > now() - interval '7 days'), 0) AS d7,
               COALESCE(sum(cost) FILTER (WHERE created_at > now() - interval '30 days'), 0) AS d30,
               COALESCE(sum(cost) FILTER (WHERE created_at >= date_trunc('month', now())), 0) AS month
        FROM {_SPEND} s""")[0]
    cards = {k: _num(v) for k, v in c.items()}
    now = datetime.now(timezone.utc)
    dim = calendar.monthrange(now.year, now.month)[1]
    cards["projected"] = cards["month"] / max(now.day, 1) * dim
    cards["spike"] = cards["prev7_avg"] > 0 and cards["today"] >= SPIKE_FACTOR * cards["prev7_avg"]
    out["cards"] = cards

    # ---- trend ----
    daily = _rows(db, f"""
        SELECT d::date AS day,
               COALESCE(sum(s.cost) FILTER (WHERE s.src = 'llm'), 0) AS llm, COALESCE(sum(s.cost) FILTER (WHERE s.src = 'apify'), 0) AS apify
        FROM generate_series(current_date - (:d - 1), current_date, interval '1 day') d
        LEFT JOIN {_SPEND} s ON s.created_at::date = d::date GROUP BY d ORDER BY d""", d=days)
    peak = max([_num(r["llm"]) + _num(r["apify"]) for r in daily] + [1e-9])
    for r in daily:
        r["day"] = r["day"].strftime("%b %d")
        r["llm"], r["apify"] = _num(r["llm"]), _num(r["apify"])
        r["llm_pct"], r["apify_pct"] = round(r["llm"] / peak * 100, 1), round(r["apify"] / peak * 100, 1)
    out["daily"] = daily
    out["window_total"] = sum(r["llm"] + r["apify"] for r in daily)

    # ---- active users per day: people who did anything that day (a paid call, a pitch, a saved brand, a daily-limit feature, a contact opened) ----
    out["active_users"] = _rows(db, f"""
        SELECT to_char(d, 'Mon DD') AS day, count(DISTINCT a.uid) AS users, count(a.uid) AS actions
        FROM generate_series(current_date - (:d - 1), current_date, interval '1 day') d
        LEFT JOIN (
            SELECT creator_profile_id AS uid, created_at::date AS day FROM llm_usage WHERE {_REAL} AND created_at >= current_date - :d
            UNION ALL SELECT creator_profile_id, created_at::date FROM apify_usage WHERE {_REAL} AND created_at >= current_date - :d
            UNION ALL SELECT creator_profile_id, used_on FROM feature_usage WHERE {_REAL} AND used_on >= current_date - :d
            UNION ALL SELECT creator_profile_id, revealed_on FROM contact_reveals WHERE {_REAL} AND revealed_on >= current_date - :d
            UNION ALL SELECT creator_profile_id, created_at::date FROM pitches WHERE {_REAL} AND created_at >= current_date - :d
            UNION ALL SELECT creator_profile_id, created_at::date FROM saved_brands WHERE {_REAL} AND created_at >= current_date - :d
            UNION ALL SELECT creator_profile_id, created_at::date FROM user_activity WHERE {_REAL} AND created_at >= current_date - :d
        ) a ON a.day = d::date
        GROUP BY d ORDER BY d""", d=days)
    for r in out["active_users"]:
        r["users"], r["actions"] = int(r["users"]), int(r["actions"])

    # ---- each feature per day, for the multi-line chart: how many DIFFERENT users used it, and how many times it was used (several per user) ----
    per_day: dict[tuple[str, str], tuple[int, int]] = {}
    for r in _rows(db, "SELECT at::date AS day, " + _LINE_CASE + " AS line, count(DISTINCT uid) AS users, count(*) AS uses FROM (" + _ACTIVITY + """) a
                        WHERE at >= current_date - (:d - 1) AND uid IN (SELECT id FROM creator_profiles WHERE lower(email) <> ALL(:excl)) AND """ + _LINE_CASE + """ IS NOT NULL
                        GROUP BY 1, 2""", d=days):
        per_day[(r["day"].strftime("%b %d"), r["line"])] = (int(r["users"]), int(r["uses"]))
    out["feature_daily"] = [{"day": r["day"], "users": {k: per_day.get((r["day"], k), (0, 0))[0] for k in FEATURE_LINES},
                             "uses": {k: per_day.get((r["day"], k), (0, 0))[1] for k in FEATURE_LINES}} for r in out["daily"]]   # same days as the spend chart
    out["feature_lines"] = list(FEATURE_LINES)

    # ---- by feature (users), merged into friendly groups ----
    groups: dict[str, dict] = {}
    for r in _rows(db, f"""SELECT feature AS name, sum(cost) AS cost, count(*) AS calls FROM {_SPEND} s WHERE {w} GROUP BY 1""", d=days):
        g = groups.setdefault(FEATURE_GROUPS.get(r["name"], r["name"] or "other"), {"cost": 0.0, "calls": 0})
        g["cost"] += _num(r["cost"]); g["calls"] += int(r["calls"])
    feats = [{"name": k, "cost": v["cost"], "calls": v["calls"], "avg": v["cost"] / v["calls"] if v["calls"] else 0} for k, v in groups.items()]
    out["features"] = _bars(sorted(feats, key=lambda r: -r["cost"]))

    # ---- top users with each one's per-feature split ----
    top = _rows(db, f"""
        WITH t AS (SELECT creator_profile_id AS id, sum(cost) AS cost, count(*) AS calls FROM {_SPEND} s
                   WHERE {w} GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 10)
        SELECT t.id, COALESCE(c.email, 'user #' || t.id) AS name, t.cost, t.calls FROM t LEFT JOIN creator_profiles c ON c.id = t.id
        ORDER BY t.cost DESC NULLS LAST""", d=days)
    if top:
        split_rows = _rows(db, f"""SELECT creator_profile_id AS id, feature, sum(cost) AS cost FROM {_SPEND} s
                                   WHERE creator_profile_id = ANY(:ids) AND {w} GROUP BY 1, 2""", ids=[r["id"] for r in top], d=days)
        per_user: dict[int, dict[str, float]] = {}
        for r in split_rows:
            label = FEATURE_GROUPS.get(r["feature"], r["feature"] or "other")
            per_user.setdefault(r["id"], {})[label] = per_user.get(r["id"], {}).get(label, 0.0) + _num(r["cost"])
        for r in top:
            r["split"] = sorted(((k, v) for k, v in per_user.get(r["id"], {}).items()), key=lambda kv: -kv[1])
    out["users"] = _bars(top)

    # ---- top users by ACTIVITY (how much they did, not what it cost), each with their per-feature split ----
    busy = _rows(db, "SELECT a.uid AS id, c.email AS name, count(*) AS count FROM (" + _ACTIVITY + """) a JOIN creator_profiles c ON c.id = a.uid
                     WHERE a.at > now() - make_interval(days => :d) AND lower(c.email) <> ALL(:excl) GROUP BY 1, 2 ORDER BY 3 DESC, 2 LIMIT 10""", d=days)
    if busy:
        mix_rows = _rows(db, "SELECT uid AS id, grp, count(*) AS count FROM (" + _ACTIVITY + """) a
                              WHERE uid = ANY(:ids) AND at > now() - make_interval(days => :d) GROUP BY 1, 2""", ids=[r["id"] for r in busy], d=days)
        per_busy: dict[int, list] = {}
        for r in mix_rows:
            per_busy.setdefault(r["id"], []).append((r["grp"], int(r["count"])))
        top_busy = max(int(r["count"]) for r in busy)
        for r in busy:
            r["count"] = int(r["count"]); r["pct"] = round(r["count"] / top_busy * 100, 1)
            r["split"] = sorted(per_busy.get(r["id"], []), key=lambda kv: -kv[1])   # most used feature first
    out["activity_users"] = busy

    # ---- model mix and cached tokens ----
    models = _rows(db, f"""SELECT model AS name, COALESCE(sum(cost_usd), 0) AS cost, count(*) AS calls, COALESCE(sum(prompt_tokens), 0) AS prompt,
                           COALESCE(sum(cached_tokens), 0) AS cached FROM llm_usage WHERE {_REAL} AND {w} GROUP BY 1 ORDER BY 2 DESC""", d=days)
    for r in models:
        r["cached_pct"] = round(int(r["cached"]) / int(r["prompt"]) * 100, 1) if r["prompt"] else 0
    out["models"] = _bars(models)

    # ---- expensive and slow calls ----
    cols = "created_at, model, left(COALESCE(context, '(no label)'), 60) AS context, COALESCE(cost_usd, 0) AS cost, duration_ms, creator_profile_id"
    out["expensive"] = _rows(db, f"SELECT {cols} FROM llm_usage WHERE {_REAL} AND {w} ORDER BY cost_usd DESC NULLS LAST LIMIT 10", d=days)
    out["slow"] = _rows(db, f"SELECT {cols} FROM llm_usage WHERE {_REAL} AND {w} AND duration_ms IS NOT NULL ORDER BY duration_ms DESC LIMIT 10", d=days)
    for r in out["expensive"] + out["slow"]:
        r["cost"] = _num(r["cost"]); r["created_at"] = r["created_at"].strftime("%b %d %H:%M") if r["created_at"] else ""

    # ---- Apify: runs where fewer results came back than were asked for, and the dearest accounts ----
    out["apify"] = _rows(db, f"""SELECT COALESCE(handle, '?') AS name, count(*) AS runs, COALESCE(sum(posts_requested), 0) AS asked, sum(posts_scraped) AS got,
                                 COALESCE(sum(cost_usd), 0) AS cost FROM apify_usage WHERE {_REAL} AND {w} GROUP BY 1 ORDER BY 5 DESC LIMIT 10""", d=days)
    out["apify"] = _bars(out["apify"])

    # ---- limits: users near a daily cap today, and brand contacts opened ----
    from api.usage_limits import DAILY_CAPS, daily_cap
    near = []
    for r in _rows(db, """SELECT f.creator_profile_id AS id, c.email AS name, f.feature, f.uses
                          FROM feature_usage f JOIN creator_profiles c ON c.id = f.creator_profile_id
                          WHERE f.used_on = current_date AND lower(c.email) <> ALL(:excl)"""):
        if r["feature"] in DAILY_CAPS:
            cap = daily_cap(r["feature"])
            if cap and r["uses"] >= NEAR_CAP * cap:
                near.append({"id": r["id"], "name": r["name"], "feature": DAILY_CAPS[r["feature"]][2], "uses": r["uses"], "cap": cap,
                             "pct": min(100, round(r["uses"] / cap * 100))})
    out["near_cap"] = sorted(near, key=lambda r: -r["pct"])[:15]
    out["reveals"] = _bars(_rows(db, """SELECT c.email AS name, count(*) AS cost, count(DISTINCT r.brand_raw_id) AS brands
                                         FROM contact_reveals r JOIN creator_profiles c ON c.id = r.creator_profile_id
                                         WHERE r.revealed_on > current_date - :d AND lower(c.email) <> ALL(:excl) GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", d=days))

    # ---- health ----
    from api.usage_limits import job_lock_stale_seconds
    stuck = _rows(db, """SELECT c.email AS name, l.feature, l.started_at FROM feature_locks l JOIN creator_profiles c ON c.id = l.creator_profile_id
                         WHERE l.started_at < now() - make_interval(secs => :s) AND lower(c.email) <> ALL(:excl) ORDER BY l.started_at LIMIT 10""",
                  s=job_lock_stale_seconds())
    for r in stuck:
        r["since"] = r["started_at"].strftime("%b %d %H:%M")
    failed_status = _rows(db, """SELECT (SELECT count(*) FROM creator_profiles WHERE instagram_description_status = 'failed' AND lower(email) <> ALL(:excl)) AS instagram_description,
                                        (SELECT count(*) FROM creator_profiles WHERE youtube_description_status = 'failed' AND lower(email) <> ALL(:excl)) AS youtube_description,
                                        (SELECT count(*) FROM creator_profiles WHERE llm_ranking_status = 'failed' AND lower(email) <> ALL(:excl)) AS ranking_v2,
                                        (SELECT count(*) FROM creator_profiles WHERE llm_v3_ranking_status = 'failed' AND lower(email) <> ALL(:excl)) AS ranking_v3,
                                        (SELECT count(*) FROM own_brand_jobs j WHERE j.status = 'failed' AND j.created_at > now() - make_interval(days => :d)
                                            AND j.creator_profile_id IN (SELECT id FROM creator_profiles WHERE lower(email) <> ALL(:excl))) AS own_brand""", d=days)[0]
    out["health"] = {"stuck": stuck, "failed": {k.replace("_", " "): int(v) for k, v in failed_status.items()}, "failed_total": sum(int(v) for v in failed_status.values())}
    out["failed_signins"] = _rows(db, f"""SELECT ip AS name, count(*) AS n, count(DISTINCT email_hash) AS emails, max(created_at) AS last FROM login_failures
                                          WHERE {w} GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", d=days)
    for r in out["failed_signins"]:
        r["last"] = r["last"].strftime("%b %d %H:%M")

    # ---- funnel (the test accounts' own sign-ups, invitations and accounts are left out) ----
    f = _rows(db, """SELECT (SELECT count(*) FROM early_access_signups WHERE lower(email) <> ALL(:excl)) AS signups,
                            (SELECT count(*) FROM account_invites WHERE lower(email) <> ALL(:excl)) AS invited,
                            (SELECT count(*) FROM account_invites i WHERE lower(i.email) <> ALL(:excl)
                                AND (i.used_at IS NOT NULL OR EXISTS (SELECT 1 FROM creator_profiles c WHERE c.email = i.email))) AS joined_from_invite,
                            (SELECT count(*) FROM creator_profiles WHERE lower(email) <> ALL(:excl)) AS accounts,
                            (SELECT count(*) FROM early_access_signups WHERE lower(email) <> ALL(:excl) AND created_at > now() - make_interval(days => :d)) AS signups_window""", d=days)[0]
    top_n = max(f["signups"], f["invited"], f["joined_from_invite"], 1)
    out["funnel"] = [{"name": "Early-access sign-ups", "n": f["signups"], "pct": round(f["signups"] / top_n * 100)},
                     {"name": "Invitations sent", "n": f["invited"], "pct": round(f["invited"] / top_n * 100)},
                     {"name": "Created an account from an invitation", "n": f["joined_from_invite"], "pct": round(f["joined_from_invite"] / top_n * 100)}]
    out["funnel_extra"] = {"accounts": f["accounts"], "signups_window": f["signups_window"]}

    # ---- one user (a test or deleted account shows as not found, with nothing) ----
    out["user"] = None
    if user_id is not None:
        who = _rows(db, "SELECT email FROM creator_profiles WHERE id = :i AND lower(email) <> ALL(:excl)", i=user_id)
        if who:
            by_f = _rows(db, f"""SELECT COALESCE(feature, 'other') AS name, sum(cost) AS cost, count(*) AS calls FROM {_SPEND} s
                                 WHERE creator_profile_id = :i AND {w} GROUP BY 1 ORDER BY 2 DESC""", i=user_id, d=days)
            recent = _rows(db, """SELECT created_at, model, left(COALESCE(context, ''), 60) AS context, COALESCE(cost_usd, 0) AS cost, duration_ms
                                  FROM llm_usage WHERE creator_profile_id = :i ORDER BY id DESC LIMIT 20""", i=user_id)
            activity = _rows(db, "SELECT at, what, grp FROM (" + _ACTIVITY + ") a WHERE uid = :i ORDER BY at DESC LIMIT 20", i=user_id)
            mix = _rows(db, "SELECT grp AS name, count(*) AS count FROM (" + _ACTIVITY + ") a WHERE uid = :i AND at > now() - make_interval(days => :d) GROUP BY 1 ORDER BY 2 DESC",
                        i=user_id, d=days)
        else:
            by_f, recent, activity, mix = [], [], [], []
        for r in recent:
            r["cost"] = _num(r["cost"]); r["created_at"] = r["created_at"].strftime("%b %d %H:%M") if r["created_at"] else ""
        top_count = max([int(r["count"]) for r in mix] + [1])
        for r in mix:
            r["count"] = int(r["count"]); r["pct"] = round(r["count"] / top_count * 100, 1)
        out["user"] = {"id": user_id, "email": who[0]["email"] if who else f"user #{user_id}", "features": _bars(by_f), "recent": recent,
                       "activity": [{"at": r["at"], "what": r["what"], "group": r["grp"]} for r in activity],   # what they did, newest first (no money)
                       "activity_mix": mix}                                                                         # how many times per feature in the window, most used first
    return out
