#!/usr/bin/env bash
# Daily database dump — run ON THE SERVER (cron).
#
# Writes, named by date (UTC):
#   /root/db_dumps/mavlit_YYYY-MM-DD.dump    custom format (pg_restore, same/newer Postgres)
#   /root/db_dumps/mavlit_YYYY-MM-DD.sql.gz  plain SQL (restorable into an older local Postgres)
# and deletes dumps older than KEEP_DAYS.
#
# pg_dump runs as the postgres superuser (sudo -u postgres), NOT as the app's database user: a
# superuser can read every table, so a table created by `sudo -u postgres psql` (e.g. a bak_ safety
# copy) can never make the dump fail with "permission denied for table ...". The files themselves
# are written by root.
#
# A failed run exits non-zero, removes its half-written files and leaves the previous dumps alone.
#
# Install (crontab -e on the server), e.g. every day at 02:00 UTC:
#   0 2 * * * /opt/MAVLIT-enrichment/scripts/db_backup_server.sh >> /root/db_dumps/backup.log 2>&1
set -euo pipefail
export LC_ALL=C.UTF-8

PROJECT_DIR="${PROJECT_DIR:-/opt/MAVLIT-enrichment}"
DUMP_DIR="${DUMP_DIR:-/root/db_dumps}"
KEEP_DAYS="${KEEP_DAYS:-7}"
MIN_BYTES="${MIN_BYTES:-1000000}"   # a real dump is tens of MB; anything smaller means something went wrong

# Database name from the project's .env DATABASE_URL (postgresql+psycopg2://user:pw@host:port/NAME?opts).
DB_URL="$(grep -E '^DATABASE_URL=' "$PROJECT_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"'"'")"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^[^/]*//[^/]*/##; s#\?.*$##')"
if [ -z "$DB_NAME" ]; then
    echo "$(date -u '+%F %T') ERROR: could not read the database name from DATABASE_URL in $PROJECT_DIR/.env" >&2
    exit 1
fi

mkdir -p "$DUMP_DIR"
STAMP="$(date -u +%F)"
BASE="$DUMP_DIR/mavlit_$STAMP"

# Never leave half-written files behind (they would also be picked up by nothing, but they waste space).
cleanup() { rm -f "$BASE.dump.tmp" "$BASE.sql.gz.tmp"; }
trap cleanup EXIT

# Leftovers of earlier failed runs.
find "$DUMP_DIR" -maxdepth 1 -name 'mavlit_*.tmp' -size 0 -delete

cd /tmp   # postgres cannot read root's working directory
sudo -u postgres pg_dump -d "$DB_NAME" -Fc --no-owner --no-privileges > "$BASE.dump.tmp"
sudo -u postgres pg_dump -d "$DB_NAME" -Fp --no-owner --no-privileges | gzip > "$BASE.sql.gz.tmp"

for f in "$BASE.dump.tmp" "$BASE.sql.gz.tmp"; do
    if [ "$(stat -c %s "$f")" -lt "$MIN_BYTES" ]; then
        echo "$(date -u '+%F %T') ERROR: $f is only $(stat -c %s "$f") bytes - dump looks incomplete" >&2
        exit 1
    fi
done

# Write to temp names first so a half-written dump is never picked up by the download script.
mv "$BASE.dump.tmp" "$BASE.dump"
mv "$BASE.sql.gz.tmp" "$BASE.sql.gz"

find "$DUMP_DIR" -maxdepth 1 -name 'mavlit_*.dump' -mtime +"$KEEP_DAYS" -delete
find "$DUMP_DIR" -maxdepth 1 -name 'mavlit_*.sql.gz' -mtime +"$KEEP_DAYS" -delete

echo "$(date -u '+%F %T') OK: $(du -h "$BASE.dump" | cut -f1) $BASE.dump, $(du -h "$BASE.sql.gz" | cut -f1) $BASE.sql.gz"
