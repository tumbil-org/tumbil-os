#!/bin/bash
# TumbilOS Deploy - Syncs TGE data and pushes to GitHub Pages
# Run after TGE pipeline completes (add to launchd or call from TGE)

set -e

PROJ_DIR="$HOME/tumbil/tumbil-os"
DASHBOARD_DIR="$PROJ_DIR/dashboard"

echo "[TumbilOS] Starting deploy at $(date)"

# Step 1: Sync data from TGE
echo "[TumbilOS] Syncing TGE data..."
python3 "$PROJ_DIR/scripts/sync_data.py"

# Step 2: Update gh-pages branch
echo "[TumbilOS] Deploying to GitHub Pages..."
cd "$PROJ_DIR"

# Copy dashboard files to a temp location
TMPDIR=$(mktemp -d)
cp "$DASHBOARD_DIR/index.html" "$TMPDIR/"
cp "$DASHBOARD_DIR/data.json" "$TMPDIR/"

# Switch to gh-pages, update files, push
git checkout gh-pages
cp "$TMPDIR/index.html" .
cp "$TMPDIR/data.json" .
rm -rf "$TMPDIR"

# Only commit if there are changes
if git diff --quiet index.html data.json 2>/dev/null; then
    echo "[TumbilOS] No changes to deploy."
else
    git add index.html data.json
    git commit -m "Update dashboard data $(date +%Y-%m-%d)"
    git push origin gh-pages
    echo "[TumbilOS] Deployed successfully."
fi

git checkout main
echo "[TumbilOS] Done at $(date)"
