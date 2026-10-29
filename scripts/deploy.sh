#!/usr/bin/env bash
# Runs ON THE SERVER: pull the latest main and restart the API.
# Triggered by .github/workflows/deploy.yml on every push to main; can also be run by hand.
#
# DEFAULT (DEPLOY_RUN_MIGRATIONS unset or 0): exactly the original behaviour - pull, install requirements if they
# changed, restart, check the service is active. "Already up to date" exits without restarting.
#
# OPT-IN (DEPLOY_RUN_MIGRATIONS=1, e.g. `DEPLOY_RUN_MIGRATIONS=1 bash deploy.sh`; nothing sets it automatically):
# migrations are part of the deploy and a deploy only counts as done once everything has passed:
#   pull -> requirements -> `python -m pipeline.migrate` -> restart -> service active -> health check -> write the marker
# The marker ($DEPLOY_STATE_DIR/last_success_commit) holds the last commit that went through ALL of that. Git being
# "already up to date" never skips a step: if the marker is not the current commit - because a migration, the restart
# or the health check failed last time - the whole sequence runs again at the same commit. A failed migration stops
# the script BEFORE the restart (the old API keeps running) and exits non-zero. Not atomic: see MIGRATIONS.md.
#
# The health probe of the opt-in path is GET /brand-niches plus GET /health/db, not /health: /health never touches the database, so it
# would stay green with a broken runtime role (missing grant, wrong URL). /brand-niches reads content_creator_re; /health/db reads
# creator_profiles and brands_raw. DEPLOY_HEALTH_URL may hold several URLs separated by spaces; all must return 200.
#
# Test/override variables (defaults are the production values): MAVLIT_APP_DIR, MAVLIT_SERVICE, MAVLIT_BRANCH,
# SYSTEMCTL, JOURNALCTL, PYTHON_BIN, PIP_BIN, LOCK_FILE, DEPLOY_STATE_DIR, DEPLOY_HEALTH_URL, DEPLOY_HEALTH_CMD,
# DEPLOY_HEALTH_RETRIES, DEPLOY_HEALTH_INTERVAL, DEPLOY_RESTART_WAIT.
set -euo pipefail

APP_DIR="${MAVLIT_APP_DIR:-/opt/MAVLIT-enrichment}"
SERVICE="${MAVLIT_SERVICE:-mavlit.service}"
BRANCH="${MAVLIT_BRANCH:-main}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
JOURNALCTL="${JOURNALCTL:-journalctl}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
PIP_BIN="${PIP_BIN:-.venv/bin/pip}"
RESTART_WAIT="${DEPLOY_RESTART_WAIT:-5}"

case "${DEPLOY_RUN_MIGRATIONS:-0}" in
    0|1) ;;
    *) echo "ERROR: DEPLOY_RUN_MIGRATIONS must be 0 or 1" >&2; exit 2 ;;
esac

cd "$APP_DIR"
exec 9>"${LOCK_FILE:-/tmp/mavlit-deploy.lock}"
flock -n 9 || { echo "another deploy is running"; exit 1; }

before=$(git rev-parse HEAD)
git fetch origin "$BRANCH"
git merge --ff-only "origin/$BRANCH"     # fails (and stops) if the server has local changes in the way
after=$(git rev-parse HEAD)

service_ok() {
    "$SYSTEMCTL" is-active --quiet "$SERVICE"
}

if [ "${DEPLOY_RUN_MIGRATIONS:-0}" != "1" ]; then
    # ---- original behaviour, unchanged -------------------------------------------------------------------------
    if [ "$before" = "$after" ]; then
        echo "already up to date ($after)"
        exit 0
    fi

    if git diff --name-only "$before" "$after" | grep -qx "requirements.txt"; then
        echo "requirements.txt changed - installing"
        "$PIP_BIN" install -r requirements.txt
    fi

    "$SYSTEMCTL" restart "$SERVICE"
    sleep "$RESTART_WAIT"
    service_ok || { echo "$SERVICE failed to start"; "$JOURNALCTL" -u "$SERVICE" -n 30 --no-pager; exit 1; }
    echo "deployed $before -> $after"
    exit 0
fi

# ---- migrations enabled ------------------------------------------------------------------------------------------
STATE_DIR="${DEPLOY_STATE_DIR:-/var/lib/mavlit-deploy}"
MARKER="$STATE_DIR/last_success_commit"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
last_ok=$(cat "$MARKER" 2>/dev/null || true)

if [ "$last_ok" = "$after" ] && service_ok; then
    echo "commit $after is already deployed and verified"
    exit 0
fi
if [ -n "$last_ok" ] && [ "$last_ok" != "$after" ]; then
    echo "last verified deploy was $last_ok; deploying $after"
elif [ -z "$last_ok" ]; then
    echo "no verified deploy recorded yet; deploying $after"
else
    echo "commit $after was not fully deployed last time (or the service is down); running every step again"
fi

base="$last_ok"
[ -n "$base" ] || base="$before"
if ! git cat-file -e "$base^{commit}" 2>/dev/null || git diff --name-only "$base" "$after" | grep -qx "requirements.txt"; then
    echo "requirements.txt changed (or the previous commit is unknown) - installing"
    "$PIP_BIN" install -r requirements.txt
fi

echo "running migrations for $after"
if ! "$PYTHON_BIN" -m pipeline.migrate; then
    echo "ERROR: the migrations failed at commit $after. The service was NOT restarted and this deploy is NOT marked complete." >&2
    echo "Fix the cause (see the migration output above) and run this script again: it will retry the migrations at the same commit." >&2
    exit 1
fi

if ! "$SYSTEMCTL" restart "$SERVICE"; then
    echo "ERROR: restarting $SERVICE failed. The migrations are applied but this deploy is NOT marked complete; run this script again." >&2
    exit 1
fi
sleep "$RESTART_WAIT"
if ! service_ok; then
    echo "ERROR: $SERVICE is not active after the restart. This deploy is NOT marked complete; run this script again." >&2
    "$JOURNALCTL" -u "$SERVICE" -n 30 --no-pager || true
    exit 1
fi

health_ok() {
    if [ -n "${DEPLOY_HEALTH_CMD:-}" ]; then
        bash -c "$DEPLOY_HEALTH_CMD"
    else
        # every URL must answer 200: /brand-niches reads content_creator_re, /health/db reads creator_profiles and brands_raw with the
        # API's own database role, so a missing grant or a wrong URL keeps the deploy from being marked complete
        # shellcheck disable=SC2086
        "$PYTHON_BIN" -c 'import sys, urllib.request; sys.exit(0 if all(urllib.request.urlopen(u, timeout=5).status == 200 for u in sys.argv[1:]) else 1)' \
            ${DEPLOY_HEALTH_URL:-http://127.0.0.1:8000/brand-niches http://127.0.0.1:8000/health/db}
    fi
}
attempt=0
until health_ok >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "${DEPLOY_HEALTH_RETRIES:-10}" ]; then
        echo "ERROR: the health check still fails after $attempt attempts. This deploy is NOT marked complete; run this script again." >&2
        exit 1
    fi
    sleep "${DEPLOY_HEALTH_INTERVAL:-2}"
done

printf '%s\n' "$after" > "$MARKER.tmp" && mv "$MARKER.tmp" "$MARKER"
echo "deployed $before -> $after (migrations applied, service healthy)"
