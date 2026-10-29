# Database migrations

The schema changes (new tables and columns, indexes, the `creator_llm_cost` view, one-off data fixes and the default
prompts) live in `pipeline/migrations.py`. They are safe to run repeatedly.

## Two environments, kept apart

| | Runtime (the API, `mavlit.service`) | Migration (`python -m pipeline.migrate`) |
|---|---|---|
| Database role | restricted (`mavlit_app`) | the table owner (`mavlit_user`) |
| Credentials in | `.env` → `DATABASE_URL` | **`.env.migrate`** (root, `chmod 600`) or the migration process's own `MIGRATION_DATABASE_URL` |
| Reads `MIGRATION_DATABASE_URL`? | **never** (if it is found in the API's environment, the API logs a warning and removes it) | yes |
| `RUN_MIGRATIONS` | controls whether the API migrates itself at startup | ignored |

The owner's password must not be in `.env` or in the service's `EnvironmentFile`, because then the API process holds it.

### Setting up the migration environment (production)
```bash
cp .env.migrate.example .env.migrate      # then put the real URL in it
chown root:root .env.migrate && chmod 600 .env.migrate
python -m pipeline.migrate --dry-run      # checks the file, its permissions and the URL; connects to nothing
```
The command refuses a migration env file that group/others can read, or that another user owns. The URL is checked
(PostgreSQL, names a database) before any connection, and none of our error messages ever contain a URL or password.

Where the migration URL comes from, first match wins: `--env-file PATH` (or `MIGRATION_ENV_FILE`) → the
`MIGRATION_DATABASE_URL` variable of the migration process → `<project>/.env.migrate` if it exists. If none of these is
configured the command stops with exit code 2: it never falls back to the runtime `DATABASE_URL` (a restricted role cannot change
the schema). For local development export `MIGRATION_DATABASE_URL` yourself.

## Running them

```bash
python -m pipeline.migrate --dry-run     # which source would be used; connects to nothing
python -m pipeline.migrate               # apply (exit 0 done, 1 failed, 2 bad configuration, 3 lock timeout)
python -m pipeline.migrate --lock-timeout-ms 5000
```

**At API startup** (the default, unchanged): the API migrates itself with its own runtime engine and no lock timeout,
exactly as before. Set `RUN_MIGRATIONS=0` (`1/true/yes/on` or `0/false/no/off`; anything else stops the API with an
error) to skip that; the API then also skips the default-prompt seeding, which runs inside the migrations.

## Lock timeout

The standalone command starts every migration transaction with `SET LOCAL lock_timeout` (default 10 000 ms,
`--lock-timeout-ms` or `MIGRATION_LOCK_TIMEOUT_MS`, 0 = no limit). A lock that cannot be had in time fails the step with
exit code 3 and rolls it back; nothing is left open and the deploy does not continue to the restart.
The migrations are several transactions, so earlier ones stay committed; every step is idempotent, so just run it again.
A timeout is a mitigation, not a cure: each `ALTER TABLE` still takes a brief exclusive lock, and a statement that
rewrites a table is not interrupted once it holds its lock.

### Maintenance / drain: a decision for you, not implemented
Running migrations while the old API serves traffic (what `deploy.sh` does when enabled) keeps downtime at zero but each
`ALTER` can briefly queue behind long transactions; the lock timeout turns that into a retryable failure. Alternatives:
stop the service, migrate, start it (no lock contention from the API, but the API is down for the migration plus the
start, and background jobs inside the API die); or put a maintenance page in front of it (needs a proxy rule). Dropping a
column the running code still reads is also unsafe in the migrate-then-restart window; do such drops in a later release.

## Deploys (`scripts/deploy.sh`)

Default: unchanged (pull, restart, check). **Opt in** with `DEPLOY_RUN_MIGRATIONS=1`; nothing sets it automatically, and
`.github/workflows/deploy.yml` does not (enable it deliberately by changing its ssh line to
`DEPLOY_RUN_MIGRATIONS=1 bash /opt/MAVLIT-enrichment/scripts/deploy.sh`).

The health check of this path is `GET /brand-niches` (public, reads `content_creator_re`), not `/health`, because `/health` never touches the database and would stay
green with a broken runtime role; override with `DEPLOY_HEALTH_URL` / `DEPLOY_HEALTH_CMD`. The script does not set or check `RUN_MIGRATIONS=0` for the API:
if the API still starts with `RUN_MIGRATIONS=1` as a restricted role it fails at startup, the restart check fails and the deploy is not marked complete.

With it on, the order is pull → requirements → migrate → restart → service active → health check (`/health`) → write
`/var/lib/mavlit-deploy/last_success_commit`. The marker is the only thing that means "deployed": if it is not the current
commit, **every step runs again at the same commit**, even though git says "already up to date". A failed migration stops
before the restart (the old API keeps running) and exits 1 with a message.

Limits you should know: it is not atomic. If the migration succeeds but the restart or health check fails, the schema
is already new while the old code is running or down; fix and re-run. A crash of the old process in that window
restarts it (`Restart=always`) on the already-pulled new code. There is no automatic rollback of schema or code.

## Runtime DDL
The API no longer changes the schema while it works. `enrich_instagram_users` checks that `instagram_users.post_collaborators`
exists and raises a clear error otherwise (CLI runs pass `ensure_schema=True`); usage tracking logs a clear error when
`llm_usage` / `apify_usage` is missing. Other CLI scripts with their own `ALTER TABLE` / `create_all` are unchanged.

## `youtube_last_sponsorship`
It is a TIMESTAMPTZ written by `brand_signals.py`. The migration only converts it if it is still TEXT/VARCHAR/TIMESTAMP/DATE
(values kept) and stops with an error on an unparseable value or an unexpected type; a TIMESTAMPTZ column is not touched.

## Runtime role grants
The API's restricted database role, its exact privileges, the SQL script and how new tables get grants without exposing backups:
see **DB_PERMISSIONS.md** (including the tested rollback, which is separate from switching `DATABASE_URL` back). `python -m pipeline.migrate --grant-role mavlit_app` (or `MIGRATION_GRANT_ROLE`) re-applies them after a
migration; without it nothing is granted.

## Tests
```bash
python -m pytest tests -q                       # no database needed; the Postgres tests skip themselves
MAVLIT_TEST_PG_URL='postgresql+psycopg2://postgres@/mavlit_test_scratch?host=/tmp/mpg&port=55432' python -m pytest tests -q   # + the disposable-database tests
```
`tests/test_migration_differential.py` upgrades a deliberately LEGACY database (old table and index names, duplicates, text timestamps, 768-dim vectors, `login_credentials`, stale prompts, ...)
with the ORIGINAL `_run_migrations` (read from `git show $MAVLIT_MIGRATION_BASELINE_REV:api/app.py`, default `HEAD`; set it to the commit before the extraction once the extraction is committed) and with
`pipeline.migrations.run_migrations()` on two clones of the same starting database, and compares schema, indexes, constraints, views, grants and every row. It needs BOTH
`MAVLIT_TEST_PG_URL` and an explicit `MAVLIT_EXPECT_PG_MAJOR` (it is skipped otherwise), refuses a server of another major, a non-local host, a database not named `mavlit_(test|scratch)_*`, and a cluster that contains any
other database; it creates and drops only `mavlit_difftest_<random>_*` databases and one role of the same prefix.

The Postgres tests (`test_migration_postgres.py`, `test_runtime_role_postgres.py`) run only against a disposable local database (name must contain `test` or `scratch`, host local; they drop its `public` schema and create two test roles; the cluster needs trust authentication for local sockets).
