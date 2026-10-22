#!/usr/bin/env bash
# Runs ON THE SERVER: pull the latest main and restart the API.
# Triggered by .github/workflows/deploy.yml on every push to main; can also be run by hand.
set -euo pipefail

APP_DIR="/opt/MAVLIT-enrichment"
SERVICE="mavlit.service"
BRANCH="main"

cd "$APP_DIR"
exec 9>/tmp/mavlit-deploy.lock
flock -n 9 || { echo "another deploy is running"; exit 1; }

before=$(git rev-parse HEAD)
git fetch origin "$BRANCH"
git merge --ff-only "origin/$BRANCH"     # fails (and stops) if the server has local changes in the way
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ]; then
    echo "already up to date ($after)"
    exit 0
fi

if git diff --name-only "$before" "$after" | grep -qx "requirements.txt"; then
    echo "requirements.txt changed - installing"
    .venv/bin/pip install -r requirements.txt
fi

systemctl restart "$SERVICE"
sleep 5
systemctl is-active --quiet "$SERVICE" || { echo "$SERVICE failed to start"; journalctl -u "$SERVICE" -n 30 --no-pager; exit 1; }
echo "deployed $before -> $after"
