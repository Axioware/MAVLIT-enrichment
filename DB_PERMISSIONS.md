# Runtime database role: least-privilege plan

`mavlit_user` owns every object and stays the migration / maintenance role. `mavlit_app` is the role for normal API and SQLAdmin
operation. The plan lives in **`pipeline/db_grants.py`**; the reviewable SQL is **`scripts/sql/runtime_role_grants.sql`** and its
undo is **`scripts/sql/runtime_role_rollback.sql`** (both generated; tests keep them identical to the code). Nothing here has been
applied to production: run everything on a staging copy first.

## What `mavlit_app` gets

| Object | Privileges |
|---|---|
| database `mavlit_enrichment_test` | CONNECT |
| schema `public` | USAGE (nothing else) |
| 34 model tables | exactly the letters below, after `REVOKE ALL` |
| view `creator_llm_cost` | SELECT |
| the sequences owned by those tables (22 in production) | USAGE only |

| Privileges | Tables |
|---|---|
| SELECT, INSERT | `apify_usage`, `llm_usage`, `contact_reveals` (the daily cap on brand-contact reveals) |
| SELECT, INSERT, DELETE | `creator_brand_llm_rankings`, `creator_brand_llm_v3_rankings`, `login_failures` (failed-login lockout), `feature_locks` (one in-flight pitch per user) |
| SELECT, INSERT, UPDATE | `creator_profiles` (the admin view can create and edit but no longer delete), `feature_usage` (per-user daily caps on LLM / Apify features) |
| SELECT, INSERT, UPDATE, DELETE | the other 24 |

Never granted: ownership, superuser, CREATE, TRUNCATE, REFERENCES, TRIGGER, and every object not listed (the 57 `bak_*` copies,
`llm_model_test_rankings`, `v3cmp_*`, anything else). INSERT always comes with SELECT (`INSERT ... RETURNING`). No
`ALTER DEFAULT PRIVILEGES`, no role membership, no change to PUBLIC.

## SQLAdmin keeps every create / edit / delete it has today
A table's privileges are the union of what routes and jobs need and what its SQLAdmin view allows (SQLAdmin runs in the API process
under this role). `tests/test_db_grants.py` compares the plan with the live view flags, and
`tests/test_runtime_role_postgres.py::test_every_sqladmin_operation_has_its_privilege` checks every enabled operation against the
real privileges. Nothing was made read-only.

| Admin view flags (create / edit / delete) | Tables | Exercised through the real SQLAdmin forms in the tests |
|---|---|---|
| all three | 23: `brand_contacts`, `brand_instagram_users`, `brand_match_profile`, `brands_niches`, `brands_raw`, `content_creator_re`, `contract_reviews`, `creator_niches`, `creator_profiles`, `facebook_branded_content_checks`, `initial_brand_score`, `instagram_creator_commenters`, `instagram_posts`, `instagram_users`, `meta_ads`, `pitches`, `prompts`, `rate_estimates`, `saved_brands`, `test_brands_with_instagram_posts`, `test_creator_brand_partnership_posts`, `test_niches`, `youtube_sponsorships` | **`prompts` and `brands_raw`: create, edit, delete (HTTP POST / DELETE, results checked in the database).** The other 21: list page + privilege check only |
| edit + delete (no create) | `early_access_signups` | create refused (403), **edit and delete performed** |
| delete only | `creator_instagram_posts`, `own_brand_jobs` | create / edit refused (403), **delete performed** |
| none (read-only) | `llm_usage` | create / edit / delete refused (403), row still there; the role also has no UPDATE / DELETE in the database |
| no admin view | `apify_usage`, `creator_brand_llm_rankings`, `creator_brand_llm_v3_rankings` | not applicable |
| read-only SQL view | `creator_llm_cost` | list + detail pages work; create / delete refused (403); role has SELECT only and cannot drop or replace it |

All 28 admin list pages (27 tables and the view) were opened as `mavlit_app`. Not exercised: create / edit / delete on the other 21 full-access
tables (the SQLAdmin login with `ADMIN_PASSKEY` set is now exercised: closed without it, a wrong passkey refused, the right one opens the pages).

## Pre-existing grants, memberships and inherited access
The script (and `python -m pipeline.migrate --grant-role mavlit_app`) runs in one transaction and ends with an **audit of the role's
effective access** (direct, inherited through any role membership, and PUBLIC). It does two different things:

- **Revokes the role's OWN direct grants** that the plan does not contain: on any other table, view, sequence or schema of the
  database, column-level grants, and extra privileges on listed objects (each listed object is reset with `REVOKE ALL` first); then
  re-grants the plan. Another role's grants are never touched (tested with an unrelated role).
- **Refuses, with instructions, anything it must not guess about**: the role being a member of `mavlit_user`, of a role that is a member
  of it, or of a predefined role such as `pg_read_all_data`; access coming from PUBLIC; default privileges naming the role; objects,
  schemas or the database owned by the role; `SUPERUSER` / `CREATEDB` / `CREATEROLE` / `REPLICATION` / `BYPASSRLS`; CREATE on the
  database or a schema; USAGE on other schemas. The transaction aborts, **nothing changes**, and each finding prints its fix, e.g.
  `REVOKE "mavlit_user" FROM "mavlit_app"`. Revoking direct grants never removes inherited privileges, so membership is checked, not assumed.
- Only `TEMPORARY` on the database (a PUBLIC default; nothing in the API uses it) is a warning, not a refusal.

## Future tables and sequences, without exposing backups
Default privileges are not used (they would grant every future table `mavlit_user` creates, including a hand-made `bak_*` copy). Grants are
re-applied by the migration step, from the explicit list:

    python -m pipeline.migrate --grant-role mavlit_app
    # or MIGRATION_GRANT_ROLE=mavlit_app in the migration process / in .env.migrate (never in the API's environment)

- the view `creator_llm_cost` is dropped and recreated by every migration run, and a dropped view loses its grants: the migration therefore saves the
  view's ACL before the drop and restores it right after the create, in the same transaction, for every role (with or without `--grant-role`, so a
  hand-run migration cannot silently break the admin page); with `--grant-role` the runtime role is then set to exactly SELECT;
- afterwards every listed table gets `REVOKE ALL` + its exact grant and its owned sequences get USAGE;
- a table that exists but is not in the plan is reported in a warning and stays inaccessible; a new model table nobody classified fails `tests/test_db_grants.py`;
- without the option nothing is granted, and the API's own startup migrations never grant.
`python -m pipeline.db_grants --print-audit | psql -f -` prints the read-only audit query: run it any time to see the role's effective access
(an empty result, or only the TEMPORARY warning, means exactly the plan).

## Batch runners and schema scripts run as the owner
Once the API's `DATABASE_URL` is `mavlit_app`, anything that changes the schema or empties a table cannot use `.env`. Those scripts need the table
owner (`mavlit_user`): `main.py`, `run_reverse_engineering.py` / `run_end_to_end_pipeline.py` / `content_creator_re.py` (their partnership-evidence
table setup), `gender_check/run_brand_gender_check.py`, `gender_check/run_brand_website_audience_check.py`, `scripts/populate_test_niches.py`
(TRUNCATE), `scripts/embed_test_niches.py` and `script_creator_niche/embed_creator_niches.py`. They now check `current_user` first and stop with
a clear message when it is the runtime role (`MAVLIT_RUNTIME_ROLE`, default `mavlit_app`; `pipeline/owner_guard.py`); as the owner nothing changes.

Give them their own root-only env file, separate from `.env`, from `.env.migrate` and from the service's environment:

    # /root/.mavlit-batch.env   (chown root:root, chmod 600; never in git, never in the API's EnvironmentFile)
    DATABASE_URL=postgresql+psycopg2://mavlit_user:<owner password>@localhost:5432/<db>

    set -a; . /root/.mavlit-batch.env; set +a      # exported variables beat .env (dotenv does not override)
    python run_reverse_engineering.py ...

`python -m pipeline.migrate` does not read this file: it needs `MIGRATION_DATABASE_URL` (see MIGRATIONS.md) and exits with code 2 when none is
configured; it never falls back to `DATABASE_URL`, which is the restricted role.

Deploys: `.github/workflows/deploy.yml` runs `DEPLOY_RUN_MIGRATIONS=1 bash scripts/deploy.sh` (pull -> requirements -> migrate -> restart -> health).
A failed migration stops the deploy before the restart. The health check needs `GET /brand-niches` and `GET /health/db` (a read of
`creator_profiles` and `brands_raw` as the API's own role) to answer 200, so a missing grant keeps the deploy from being marked complete.
Set `RUN_MIGRATIONS=0` in the API's `.env` BEFORE switching its `DATABASE_URL` to `mavlit_app` (unset means "on", and the startup migration would fail).

## Staging procedure
Use a restored copy of the database, never production. Substitute the staging database name for `<db>`.
```bash
# 0. roles on the staging cluster (not shown: create mavlit_app LOGIN with a staging password, no memberships)
# 1. review what will run (connects to nothing)
python -m pipeline.db_grants --role mavlit_app --owner mavlit_user --database <db> > /tmp/grants.sql
python -m pipeline.db_grants --role mavlit_app --owner mavlit_user --database <db> --rollback > /tmp/rollback.sql
# 2. apply as the owner role, one transaction; a refusal prints the findings and changes nothing
psql -v ON_ERROR_STOP=1 -h <staging-host> -U mavlit_user -d <db> -f /tmp/grants.sql
# 3. check the effective access (empty, or only the TEMPORARY warning)
python -m pipeline.db_grants --role mavlit_app --print-audit | psql -h <staging-host> -U mavlit_user -d <db> -A -f -
# 4. run the API as mavlit_app with RUN_MIGRATIONS=0 (DATABASE_URL = the mavlit_app staging URL), then by hand: log in, matches, a pitch,
#    rate intelligence, a SQLAdmin edit + delete on a full-access table, and ONE run each of /seed, /enrich/signals, /score/brands and
#    the own-brand job (they need Apify / OpenAI / Apollo keys). Any "permission denied" in the log = a letter missing from CODE.
# 5. a migration with the grant step, as the owner (keeps the view and new tables granted)
python -m pipeline.migrate --env-file <staging .env.migrate> --grant-role mavlit_app
# 6. rehearse the rollback (below), then apply the grants again
```

## Rollback
Two separate things; do not confuse them.

**A. Restore the previous application configuration (does not revoke anything).** Set `DATABASE_URL` back to the `mavlit_user` URL in `.env`,
remove or reset `RUN_MIGRATIONS=0` if you want the API to migrate itself again, restart `mavlit.service`, check `/health`. After this
`mavlit_app` **still holds every grant and can still log in**: switching `DATABASE_URL` changes who the API connects as, nothing in PostgreSQL.
Do this first in an emergency; it restores service.

**B. Revoke the runtime role's permissions.**
```bash
psql -v ON_ERROR_STOP=1 -h <host> -U mavlit_user -d <db> -f scripts/sql/runtime_role_rollback.sql
```
One transaction, repeatable, guarded like the grant script (right database, run as the owner or a superuser). It revokes every direct
grant `mavlit_app` holds in the database (all tables, views and sequences of `public`, anything outside the plan, the schema, the
database), column grants included, and ends by checking that no direct grant remains. It does **not** drop the role, delete data, touch PUBLIC or any other role,
or change memberships. Check afterwards: `python -m pipeline.db_grants --print-audit | psql ...` shows nothing planned any more; `\\dp` shows no
`mavlit_app` entries.

- **Memberships** (only if someone granted them; the scripts never do): `REVOKE "mavlit_user" FROM "mavlit_app";` as a superuser or the grantor.
- **Not routine, only if you decide the role must go:** `DROP OWNED BY mavlit_app; DROP ROLE mavlit_app;` (after A and B).
- Put the grants back at any time with the grant script; the rollback does not make that harder.

## What was proven (disposable PostgreSQL 14, `tests/test_runtime_role_postgres.py`)
Migrations by a non-superuser owner; the exact privilege set on exactly the planned objects; sequences usable (USAGE, no `setval`); ORM CRUD with
`RETURNING`; every DDL / TRUNCATE / ownership / index / trigger / `DROP VIEW` / `DROP SCHEMA` attempt fails with insufficient_privilege and
`run_migrations` fails as that role; the recreated view loses access without the grant step and gets it back with it; later tables get the
grants while `bak_*` copies stay hidden; other schemas and databases are unreachable; the script equals the grant step and refuses wrong
database / user; pre-existing stray direct grants are revoked and an unrelated role's grants survive (script and migration paths);
membership (direct, transitive, predefined), PUBLIC access, default privileges, ownership and role attributes are refused with
instructions and change nothing; rollback removes every direct grant, keeps the role, the data and other roles' grants, and is repeatable;
the real app logs in as the role (`RUN_MIGRATIONS=0`), 15 route calls and the SQLAdmin operations in the table above work.

## Remaining risks
- Background jobs (`/seed`, `/enrich/signals`, `/score/brands`, own-brand) were not run end to end; their needs come from code tracing and production counters.
- Tested on PostgreSQL 14.23 only (PostgreSQL 15+ schema defaults set explicitly); **PostgreSQL 18 is not verified**. The audit also checks the PostgreSQL 17+ table
  privilege `MAINTAIN` behind a version test; that branch has never run. Repeat the whole procedure on 18 (the tests print the server version and can insist on a
  major with `MAVLIT_EXPECT_PG_MAJOR=18`).
- Create/edit/delete on 21 of 23 full-access tables are covered by the privilege check only.
- PUBLIC defaults remain (CONNECT, TEMPORARY, and CREATE on `public` before PostgreSQL 15); the audit refuses CREATE and reports TEMPORARY.
- CLI scripts keep using `mavlit_user`; the operator endpoints and `/admin` now require `ADMIN_PASSKEY` (api/operator.py); the role still limits what a holder of that key can do to DML on the 30 tables.
