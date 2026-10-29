#!/usr/bin/env bash
# Removes leaked files from the WHOLE git history. PREPARED, NOT RUN: nothing in this repo calls it.
#
#   1. ROTATE the credentials first (the Apify token; user passwords / JWT_SECRET for the database dump; see SECURITY.md).
#      Rewriting history does not un-leak anything that was already cloned or cached.
#   2. BACK UP the repository (this script makes a mirror backup before it rewrites anything).
#   3. This rewrites every commit id and needs a FORCE-PUSH. Tell everyone first: after the push every clone must be
#      deleted and RE-CLONED (an old clone would push the removed files back). Ask GitHub support to drop cached views
#      of the old commits and check forks.
#   4. Needs git-filter-repo:  pip install git-filter-repo
#
# Usage (from a fresh clone, not your working copy):  CONFIRM_REWRITE=yes bash scripts/purge_history.sh
set -euo pipefail

if [ "${CONFIRM_REWRITE:-}" != "yes" ]; then
    echo "Refusing to run: read the comments above, rotate the credentials, then set CONFIRM_REWRITE=yes." >&2
    exit 1
fi
command -v git-filter-repo >/dev/null || { echo "git-filter-repo is not installed (pip install git-filter-repo)" >&2; exit 1; }

ORIGIN_URL="$(git remote get-url origin)"
BACKUP="../$(basename "$PWD")-backup-$(date +%Y%m%d%H%M%S).git"
git clone --mirror . "$BACKUP"
echo "Backup written to $BACKUP"

git filter-repo --invert-paths \
    --path mavlit_enrichment_test_20260806_171509.dump \
    --path test_instagram_scraper.py \
    --path test_instagram_collabs.py \
    --path test1_instagram_collabs.py

# filter-repo removes the 'origin' remote on purpose; add it back, then push yourself after checking the result:
git remote add origin "$ORIGIN_URL"
echo "History rewritten. Check it (git log --all -- <each path> must print nothing), then:"
echo "    git push --force --all origin && git push --force --tags origin"
echo "and have every collaborator delete their clone and re-clone."
