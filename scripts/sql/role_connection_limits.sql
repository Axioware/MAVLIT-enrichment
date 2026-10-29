-- Per-role connection limits for the MAVLIT database roles (checklist 2.9). NOT applied by anything in this repo and not generated:
-- review it, then run it ONCE as a superuser (ALTER ROLE ... CONNECTION LIMIT needs superuser or CREATEROLE; the owner role cannot change
-- another role). Superusers (backups, emergency access) are exempt from these limits.
--
--   psql -v ON_ERROR_STOP=1 -U postgres -d postgres -f scripts/sql/role_connection_limits.sql
--
-- Sizing (max_connections = 100, superuser_reserved_connections = 3, so 97 slots for ordinary roles):
--   API process: DB_POOL_SIZE 10 + DB_MAX_OVERFLOW 5 = 15, plus the 2 of the long-job pool (BatchSessionLocal) = 17 per process, and up to 34
--   while an old process drains after a restart (TimeoutStopSec=300).
--   Batch scripts and runners: 1-15 each depending on DB_POOL_SIZE / DB_MAX_OVERFLOW (the small utility scripts are capped at 1).
--   Migration command: 1 at a time (no pooling).
BEGIN;

-- The API's runtime role: 17 + 17 (restart overlap) + 6 spare.
ALTER ROLE mavlit_app CONNECTION LIMIT 40;

-- The owner / maintenance role. While the API still connects as mavlit_user it also carries the API's 17-34, which is why this is 40 for now.
-- LOWER IT TO 20 once the API runs as mavlit_app (DB_PERMISSIONS.md): then only batch scripts, the migration command and manual sessions use it.
ALTER ROLE mavlit_user CONNECTION LIMIT 40;

COMMIT;

-- Check afterwards:  SELECT rolname, rolconnlimit FROM pg_roles WHERE rolname IN ('mavlit_app', 'mavlit_user');   (-1 means no limit)
-- Undo:              ALTER ROLE mavlit_app CONNECTION LIMIT -1;  ALTER ROLE mavlit_user CONNECTION LIMIT -1;
