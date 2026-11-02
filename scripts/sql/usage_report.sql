-- scripts/sql/usage_report.sql
--
-- READ-ONLY usage and abuse report. Every statement is a SELECT; run it as the application role (mavlit_app has SELECT on all of these
-- tables) or the owner, and ideally in a read-only session:
--     PGOPTIONS='-c default_transaction_read_only=on' psql "$DATABASE_URL" -X -f scripts/sql/usage_report.sql
-- It prints creator ids, hashes (the first 12 characters of a SHA-256) and client addresses. It prints NO email address.
-- The daily caps below are the defaults (30/20/10/10/10/20); if you set PITCHES_PER_DAY and the others in the environment, change the caps table.
-- See docs/OPERATIONS.md for what to alert on.

-- 1. Top users by LLM cost: last 24 hours and last 7 days
SELECT creator_profile_id,
       round(coalesce(sum(cost_usd) FILTER (WHERE created_at > now() - interval '1 day'), 0), 3) AS usd_24h,
       round(coalesce(sum(cost_usd), 0), 3)                                                      AS usd_7d,
       count(*) FILTER (WHERE created_at > now() - interval '1 day')                              AS calls_24h,
       count(*)                                                                                   AS calls_7d
FROM llm_usage
WHERE created_at > now() - interval '7 days' AND creator_profile_id IS NOT NULL
GROUP BY creator_profile_id
ORDER BY usd_24h DESC, usd_7d DESC
LIMIT 20;

-- 2. Users at 80% or more of a daily cap today (UTC day)
WITH caps(feature, cap) AS (
    VALUES ('pitches', 30), ('rate_intelligence', 20), ('contract_advice', 10),
           ('match_refresh', 10), ('own_brands', 10), ('description_generation', 20)
)
SELECT u.creator_profile_id, u.feature, u.uses, c.cap, round(100.0 * u.uses / c.cap) AS percent_of_cap
FROM feature_usage u
JOIN caps c ON c.feature = u.feature
WHERE u.used_on = (now() AT TIME ZONE 'utc')::date
  AND u.uses * 100 >= 80 * c.cap
ORDER BY percent_of_cap DESC, u.uses DESC;

-- 3. Apify scrapes per user, last 24 hours
SELECT creator_profile_id, count(*) AS runs, coalesce(sum(posts_scraped), 0) AS posts_scraped
FROM apify_usage
WHERE created_at > now() - interval '1 day' AND creator_profile_id IS NOT NULL
GROUP BY creator_profile_id
ORDER BY runs DESC, posts_scraped DESC
LIMIT 20;

-- 4. Brand contacts revealed per user today (UTC day)
SELECT creator_profile_id, count(*) AS brands_revealed_today
FROM contact_reveals
WHERE revealed_on = (now() AT TIME ZONE 'utc')::date
GROUP BY creator_profile_id
ORDER BY brands_revealed_today DESC
LIMIT 20;

-- 5. Failed logins by client address, last hour
SELECT ip, count(*) AS failures, count(DISTINCT email_hash) AS accounts_tried, max(created_at) AS last_failure
FROM login_failures
WHERE created_at > now() - interval '1 hour'
GROUP BY ip
ORDER BY failures DESC
LIMIT 20;

-- 6. Failed logins by hashed account, last hour (5 or more = that account is locked out)
SELECT left(email_hash, 12) AS account_hash, count(*) AS failures, count(DISTINCT ip) AS addresses, max(created_at) AS last_failure
FROM login_failures
WHERE created_at > now() - interval '1 hour'
GROUP BY email_hash
HAVING count(*) >= 5
ORDER BY failures DESC
LIMIT 20;

-- 7. In-flight locks held longer than 3 minutes (a pitch takes at most about 2.5 minutes; background jobs hold theirs while they run)
SELECT creator_profile_id, feature, started_at, round(extract(epoch FROM now() - started_at)) AS seconds_held
FROM feature_locks
WHERE started_at < now() - interval '3 minutes'
ORDER BY started_at;

-- 8. Jobs stuck in queued/running: no live lock (not alive), or a lock older than 30 minutes
SELECT p.id AS creator_profile_id, k.kind, k.status, l.started_at AS lock_started_at,
       CASE WHEN l.started_at IS NULL THEN 'no lock: the job is not alive' ELSE 'lock older than 30 minutes' END AS why
FROM creator_profiles p
CROSS JOIN LATERAL (VALUES ('description:instagram', p.instagram_description_status), ('description:youtube', p.youtube_description_status),
                           ('ranking:v2', p.llm_ranking_status), ('ranking:v3', p.llm_v3_ranking_status)) AS k(kind, status)
LEFT JOIN feature_locks l ON l.creator_profile_id = p.id AND l.feature = k.kind
WHERE k.status IN ('queued', 'running')
  AND (l.started_at IS NULL OR l.started_at < now() - interval '30 minutes')
ORDER BY p.id, k.kind;

-- 9. Own-brand jobs that have not moved for 10 minutes
SELECT creator_profile_id, id AS job_id, status, stage, updated_at
FROM own_brand_jobs
WHERE status IN ('queued', 'running') AND updated_at < now() - interval '10 minutes'
ORDER BY updated_at;
