"""
Numbers for the admin "Usage & Cost" page (api/app.py UsageDashboardAdmin). Read-only SQL over llm_usage and apify_usage (spend), feature_usage,
contact_reveals, feature_locks, login_failures, own_brand_jobs, creator_profiles, early_access_signups and account_invites. Costs are the logged
cost_usd values (NULL counts as 0; Apify's is an estimate). Rows with no creator_profile_id come from the enrichment scripts.
"""
import calendar
import csv
import io
from datetime import datetime, timezone

from sqlalchemy import text

DAY_CHOICES = (7, 30, 90)
SPIKE_FACTOR = 3.0         # "today" is flagged when it is this many times the average of the 7 days before
NEAR_CAP = 0.8             # a user is "near" a daily limit from this share of it

# every OpenAI call and every Instagram scraper run as one row shape
_SPEND = """(SELECT created_at, 'llm' AS src, creator_profile_id, feature, COALESCE(cost_usd, 0) AS cost, context AS label FROM llm_usage
             UNION ALL
             SELECT created_at, 'apify', creator_profile_id, feature, COALESCE(cost_usd, 0), label FROM apify_usage)"""
# "demographics @someone" -> "demographics"; "brand tier for brand_id=3" -> "brand tier for": the first words before an @, an id or a number
_STEP = r"""COALESCE(NULLIF(array_to_string((regexp_split_to_array(regexp_replace(lower(COALESCE(label, '')), '\s+(@|\S*=|\(|[0-9]).*$', ''), '\s+'))[1:3], ' '), ''), '(no label)')"""

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
    return [dict(r._mapping) for r in db.execute(text(sql), params)]


def _bars(rows: list[dict], key: str = "cost") -> list[dict]:
    top = max([_num(r[key]) for r in rows] + [1e-9])
    for r in rows:
        r[key] = _num(r[key])
        r["pct"] = round(r[key] / top * 100, 1)
    return rows


def steps_csv(steps: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["step", "openai_usd", "apify_usd", "total_usd", "calls", "avg_usd_per_call"])
    for r in steps:
        writer.writerow([r["name"], f"{r['llm']:.6f}", f"{r['apify']:.6f}", f"{r['cost']:.6f}", r["calls"], f"{r['avg']:.6f}"])
    return out.getvalue()


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
    out["active_users"] = _rows(db, """
        SELECT to_char(d, 'Mon DD') AS day, count(DISTINCT a.uid) AS users, count(a.uid) AS actions
        FROM generate_series(current_date - (:d - 1), current_date, interval '1 day') d
        LEFT JOIN (
            SELECT creator_profile_id AS uid, created_at::date AS day FROM llm_usage WHERE creator_profile_id IS NOT NULL AND created_at >= current_date - :d
            UNION ALL SELECT creator_profile_id, created_at::date FROM apify_usage WHERE creator_profile_id IS NOT NULL AND created_at >= current_date - :d
            UNION ALL SELECT creator_profile_id, used_on FROM feature_usage WHERE used_on >= current_date - :d
            UNION ALL SELECT creator_profile_id, revealed_on FROM contact_reveals WHERE revealed_on >= current_date - :d
            UNION ALL SELECT creator_profile_id, created_at::date FROM pitches WHERE created_at >= current_date - :d
            UNION ALL SELECT creator_profile_id, created_at::date FROM saved_brands WHERE created_at >= current_date - :d
        ) a ON a.day = d::date
        GROUP BY d ORDER BY d""", d=days)
    for r in out["active_users"]:
        r["users"], r["actions"] = int(r["users"]), int(r["actions"])

    # ---- enrichment scripts vs users ----
    split = _rows(db, f"""
        SELECT COALESCE(sum(cost) FILTER (WHERE creator_profile_id IS NULL), 0) AS scripts,
               COALESCE(sum(cost) FILTER (WHERE creator_profile_id IS NOT NULL), 0) AS users FROM {_SPEND} s WHERE {w}""", d=days)[0]
    scripts, users = _num(split["scripts"]), _num(split["users"])
    total = scripts + users
    out["split"] = {"scripts": scripts, "users": users, "scripts_pct": round(scripts / total * 100, 1) if total else 0,
                    "users_pct": round(users / total * 100, 1) if total else 0}

    # ---- by feature (users), merged into friendly groups ----
    groups: dict[str, dict] = {}
    for r in _rows(db, f"""SELECT feature AS name, sum(cost) AS cost, count(*) AS calls FROM {_SPEND} s
                           WHERE creator_profile_id IS NOT NULL AND {w} GROUP BY 1""", d=days):
        g = groups.setdefault(FEATURE_GROUPS.get(r["name"], r["name"] or "other"), {"cost": 0.0, "calls": 0})
        g["cost"] += _num(r["cost"]); g["calls"] += int(r["calls"])
    feats = [{"name": k, "cost": v["cost"], "calls": v["calls"], "avg": v["cost"] / v["calls"] if v["calls"] else 0} for k, v in groups.items()]
    out["features"] = _bars(sorted(feats, key=lambda r: -r["cost"]))

    # ---- by step (enrichment scripts), OpenAI and Apify together ----
    steps = _rows(db, f"""
        SELECT {_STEP} AS name, sum(cost) FILTER (WHERE src = 'llm') AS llm, sum(cost) FILTER (WHERE src = 'apify') AS apify,
               sum(cost) AS cost, count(*) AS calls
        FROM {_SPEND} s WHERE creator_profile_id IS NULL AND {w} GROUP BY 1 ORDER BY 4 DESC NULLS LAST LIMIT 40""", d=days)
    for r in steps:
        r["llm"], r["apify"], r["calls"] = _num(r["llm"]), _num(r["apify"]), int(r["calls"])
        r["avg"] = _num(r["cost"]) / r["calls"] if r["calls"] else 0
    out["steps"] = _bars(steps)

    # ---- top users with each one's per-feature split ----
    top = _rows(db, f"""
        WITH t AS (SELECT creator_profile_id AS id, sum(cost) AS cost, count(*) AS calls FROM {_SPEND} s
                   WHERE creator_profile_id IS NOT NULL AND {w} GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 10)
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

    # ---- model mix and cached tokens ----
    models = _rows(db, f"""SELECT model AS name, COALESCE(sum(cost_usd), 0) AS cost, count(*) AS calls, COALESCE(sum(prompt_tokens), 0) AS prompt,
                           COALESCE(sum(cached_tokens), 0) AS cached FROM llm_usage WHERE {w} GROUP BY 1 ORDER BY 2 DESC""", d=days)
    for r in models:
        r["cached_pct"] = round(int(r["cached"]) / int(r["prompt"]) * 100, 1) if r["prompt"] else 0
    out["models"] = _bars(models)

    # ---- expensive and slow calls ----
    cols = "created_at, model, left(COALESCE(context, '(no label)'), 60) AS context, COALESCE(cost_usd, 0) AS cost, duration_ms, creator_profile_id"
    out["expensive"] = _rows(db, f"SELECT {cols} FROM llm_usage WHERE {w} ORDER BY cost_usd DESC NULLS LAST LIMIT 10", d=days)
    out["slow"] = _rows(db, f"SELECT {cols} FROM llm_usage WHERE {w} AND duration_ms IS NOT NULL ORDER BY duration_ms DESC LIMIT 10", d=days)
    for r in out["expensive"] + out["slow"]:
        r["cost"] = _num(r["cost"]); r["created_at"] = r["created_at"].strftime("%b %d %H:%M") if r["created_at"] else ""

    # ---- Apify: runs where fewer results came back than were asked for, and the dearest accounts ----
    out["apify"] = _rows(db, f"""SELECT COALESCE(handle, '?') AS name, count(*) AS runs, COALESCE(sum(posts_requested), 0) AS asked, sum(posts_scraped) AS got,
                                 COALESCE(sum(cost_usd), 0) AS cost FROM apify_usage WHERE {w} GROUP BY 1 ORDER BY 5 DESC LIMIT 10""", d=days)
    out["apify"] = _bars(out["apify"])

    # ---- limits: users near a daily cap today, and brand contacts opened ----
    from api.usage_limits import DAILY_CAPS, daily_cap
    near = []
    for r in _rows(db, """SELECT f.creator_profile_id AS id, COALESCE(c.email, 'user #' || f.creator_profile_id) AS name, f.feature, f.uses
                          FROM feature_usage f LEFT JOIN creator_profiles c ON c.id = f.creator_profile_id WHERE f.used_on = current_date"""):
        if r["feature"] in DAILY_CAPS:
            cap = daily_cap(r["feature"])
            if cap and r["uses"] >= NEAR_CAP * cap:
                near.append({"id": r["id"], "name": r["name"], "feature": DAILY_CAPS[r["feature"]][2], "uses": r["uses"], "cap": cap,
                             "pct": min(100, round(r["uses"] / cap * 100))})
    out["near_cap"] = sorted(near, key=lambda r: -r["pct"])[:15]
    out["reveals"] = _bars(_rows(db, """SELECT COALESCE(c.email, 'user #' || r.creator_profile_id) AS name, count(*) AS cost, count(DISTINCT r.brand_raw_id) AS brands
                                         FROM contact_reveals r LEFT JOIN creator_profiles c ON c.id = r.creator_profile_id
                                         WHERE r.revealed_on > current_date - :d GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", d=days))

    # ---- health ----
    from api.usage_limits import job_lock_stale_seconds
    stuck = _rows(db, """SELECT COALESCE(c.email, 'user #' || l.creator_profile_id) AS name, l.feature, l.started_at FROM feature_locks l
                         LEFT JOIN creator_profiles c ON c.id = l.creator_profile_id
                         WHERE l.started_at < now() - make_interval(secs => :s) ORDER BY l.started_at LIMIT 10""", s=job_lock_stale_seconds())
    for r in stuck:
        r["since"] = r["started_at"].strftime("%b %d %H:%M")
    failed_status = _rows(db, """SELECT (SELECT count(*) FROM creator_profiles WHERE instagram_description_status = 'failed') AS instagram_description,
                                        (SELECT count(*) FROM creator_profiles WHERE youtube_description_status = 'failed') AS youtube_description,
                                        (SELECT count(*) FROM creator_profiles WHERE llm_ranking_status = 'failed') AS ranking_v2,
                                        (SELECT count(*) FROM creator_profiles WHERE llm_v3_ranking_status = 'failed') AS ranking_v3,
                                        (SELECT count(*) FROM own_brand_jobs WHERE status = 'failed' AND created_at > now() - make_interval(days => :d)) AS own_brand""", d=days)[0]
    out["health"] = {"stuck": stuck, "failed": {k.replace("_", " "): int(v) for k, v in failed_status.items()}, "failed_total": sum(int(v) for v in failed_status.values())}
    out["failed_signins"] = _rows(db, f"""SELECT ip AS name, count(*) AS n, count(DISTINCT email_hash) AS emails, max(created_at) AS last FROM login_failures
                                          WHERE {w} GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", d=days)
    for r in out["failed_signins"]:
        r["last"] = r["last"].strftime("%b %d %H:%M")

    # ---- funnel ----
    f = _rows(db, """SELECT (SELECT count(*) FROM early_access_signups) AS signups,
                            (SELECT count(*) FROM account_invites) AS invited,
                            (SELECT count(*) FROM account_invites i WHERE i.used_at IS NOT NULL OR EXISTS (SELECT 1 FROM creator_profiles c WHERE c.email = i.email)) AS joined_from_invite,
                            (SELECT count(*) FROM creator_profiles) AS accounts,
                            (SELECT count(*) FROM early_access_signups WHERE created_at > now() - make_interval(days => :d)) AS signups_window""", d=days)[0]
    top_n = max(f["signups"], f["invited"], f["joined_from_invite"], 1)
    out["funnel"] = [{"name": "Early-access sign-ups", "n": f["signups"], "pct": round(f["signups"] / top_n * 100)},
                     {"name": "Invitations sent", "n": f["invited"], "pct": round(f["invited"] / top_n * 100)},
                     {"name": "Created an account from an invitation", "n": f["joined_from_invite"], "pct": round(f["joined_from_invite"] / top_n * 100)}]
    out["funnel_extra"] = {"accounts": f["accounts"], "signups_window": f["signups_window"]}

    # ---- one user ----
    out["user"] = None
    if user_id is not None:
        who = _rows(db, "SELECT email FROM creator_profiles WHERE id = :i", i=user_id)
        by_f = _rows(db, f"""SELECT COALESCE(feature, 'other') AS name, sum(cost) AS cost, count(*) AS calls FROM {_SPEND} s
                             WHERE creator_profile_id = :i AND {w} GROUP BY 1 ORDER BY 2 DESC""", i=user_id, d=days)
        recent = _rows(db, """SELECT created_at, model, left(COALESCE(context, ''), 60) AS context, COALESCE(cost_usd, 0) AS cost, duration_ms
                              FROM llm_usage WHERE creator_profile_id = :i ORDER BY id DESC LIMIT 20""", i=user_id)
        for r in recent:
            r["cost"] = _num(r["cost"]); r["created_at"] = r["created_at"].strftime("%b %d %H:%M") if r["created_at"] else ""
        out["user"] = {"id": user_id, "email": who[0]["email"] if who else f"user #{user_id}", "features": _bars(by_f), "recent": recent}
    return out
