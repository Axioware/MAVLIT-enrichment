#!/usr/bin/env bash
# Daily database dump — run ON THE SERVER (cron).
#
# Writes, named by date (UTC):
#   /root/db_dumps/mavlit_YYYY-MM-DD.dump    custom format (pg_restore, same/newer Postgres)
#   /root/db_dumps/mavlit_YYYY-MM-DD.sql.gz  plain SQL (restorable into an older local Postgres)
# and deletes dumps older than KEEP_DAYS.
#
# Install (crontab -e on the server), e.g. every day at 02:00 UTC:
#   0 2 * * * /opt/MAVLIT-enrichment/scripts/db_backup_server.sh >> /root/db_dumps/backup.log 2>&1
set -euo pipefail
export LC_ALL=C.UTF-8

PROJECT_DIR="${PROJECT_DIR:-/opt/MAVLIT-enrichment}"
DUMP_DIR="${DUMP_DIR:-/root/db_dumps}"
KEEP_DAYS="${KEEP_DAYS:-7}"

# DATABASE_URL from the project's .env, minus SQLAlchemy's "+psycopg2" driver suffix.
DB_URL="$(grep -E '^DATABASE_URL=' "$PROJECT_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"'"'" | sed 's/+psycopg2//')"
if [ -z "$DB_URL" ]; then
    echo "$(date -u '+%F %T') ERROR: DATABASE_URL not found in $PROJECT_DIR/.env" >&2
    exit 1
fi

mkdir -p "$DUMP_DIR"
STAMP="$(date -u +%F)"
BASE="$DUMP_DIR/mavlit_$STAMP"

# Write to temp names first so a half-written dump is never picked up by the download script.
pg_dump "$DB_URL" -Fc --no-owner --no-privileges -f "$BASE.dump.tmp"
pg_dump "$DB_URL" -Fp --no-owner --no-privileges | gzip > "$BASE.sql.gz.tmp"
mv "$BASE.dump.tmp" "$BASE.dump"
mv "$BASE.sql.gz.tmp" "$BASE.sql.gz"

find "$DUMP_DIR" -maxdepth 1 -name 'mavlit_*.dump' -mtime +"$KEEP_DAYS" -delete
find "$DUMP_DIR" -maxdepth 1 -name 'mavlit_*.sql.gz' -mtime +"$KEEP_DAYS" -delete

echo "$(date -u '+%F %T') OK: $(du -h "$BASE.dump" | cut -f1) $BASE.dump, $(du -h "$BASE.sql.gz" | cut -f1) $BASE.sql.gz"
