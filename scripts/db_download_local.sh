#!/usr/bin/env bash
# Download the newest daily database dump from the server — run LOCALLY.
#
# Copies the latest mavlit_YYYY-MM-DD.dump and .sql.gz (made by
# scripts/db_backup_server.sh) into <project>/production_dump/ (git-ignored —
# it is a full copy of production data) if not already there,
# and deletes local copies older than KEEP_DAYS. Needs SSH key access to the
# server (same as `ssh root@167.233.245.141`).
#
# Run by hand:      ./scripts/db_download_local.sh
# Or daily (crontab -e locally), e.g. 09:00 every day:
#   0 9 * * * /home/axioware/Desktop/MAVLIT-enrichment/scripts/db_download_local.sh >> /home/axioware/Desktop/MAVLIT-enrichment/production_dump/download.log 2>&1
set -euo pipefail

SERVER="${SERVER:-root@167.233.245.141}"
REMOTE_DIR="${REMOTE_DIR:-/root/db_dumps}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="${LOCAL_DIR:-$PROJECT_DIR/production_dump}"
KEEP_DAYS="${KEEP_DAYS:-14}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)

mkdir -p "$LOCAL_DIR"

# Newest finished dump on the server (temp .tmp files never match).
LATEST="$(ssh "${SSH_OPTS[@]}" "$SERVER" "ls -1t $REMOTE_DIR/mavlit_????-??-??.dump 2>/dev/null | head -1")"
if [ -z "$LATEST" ]; then
    echo "$(date '+%F %T') ERROR: no dump found in $SERVER:$REMOTE_DIR" >&2
    exit 1
fi
BASE="$(basename "$LATEST" .dump)"

# The server's nightly backup can fail silently (it did from Oct 14 to Oct 23, 2026): warn loudly
# when the newest dump is more than MAX_AGE_DAYS old. Still downloads it below.
MAX_AGE_DAYS="${MAX_AGE_DAYS:-2}"
DUMP_DAY="${BASE#mavlit_}"
AGE_DAYS=$(( ( $(date -u +%s) - $(date -u -d "$DUMP_DAY" +%s) ) / 86400 ))
if [ "$AGE_DAYS" -gt "$MAX_AGE_DAYS" ]; then
    MSG="newest server dump is $AGE_DAYS days old ($BASE) - the nightly backup on the server is failing; see /root/db_dumps/backup.log"
    echo "$(date '+%F %T') WARNING: $MSG" >&2
    command -v notify-send >/dev/null && DISPLAY="${DISPLAY:-:0}" notify-send -u critical "MAVLIT DB backup stale" "$MSG" || true
fi

for ext in dump sql.gz; do
    if [ -s "$LOCAL_DIR/$BASE.$ext" ]; then
        echo "$(date '+%F %T') already have $BASE.$ext"
    else
        scp -q "${SSH_OPTS[@]}" "$SERVER:$REMOTE_DIR/$BASE.$ext" "$LOCAL_DIR/$BASE.$ext.part"
        mv "$LOCAL_DIR/$BASE.$ext.part" "$LOCAL_DIR/$BASE.$ext"
        echo "$(date '+%F %T') downloaded $BASE.$ext ($(du -h "$LOCAL_DIR/$BASE.$ext" | cut -f1))"
    fi
done

find "$LOCAL_DIR" -maxdepth 1 -name 'mavlit_????-??-??.*' -mtime +"$KEEP_DAYS" -delete
