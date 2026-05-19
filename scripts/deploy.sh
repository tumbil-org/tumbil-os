#!/bin/bash
# TumbilOS Deploy - Syncs analyst brief + live data and pushes to GitHub Pages.
# Reads the daily analyst brief from ~/tumbil/tge/reports/ (TGE writes, TumbilOS reads).
# Uses a shallow clone of tumbil-org/tumbil-os gh-pages for publishing.

set -e

# Ensure node/npx available on ThinkPad
if [ "$(uname)" != "Darwin" ]; then
    export PATH="$HOME/.nvm/versions/node/v22.22.0/bin:$PATH"
    export PYTHONPATH="$HOME/.local/lib/python3.12/site-packages:/usr/lib/python3/dist-packages:${PYTHONPATH:-}"
fi

# Pull env from the shared tge-env when running manually over SSH.
if [ -f "$HOME/.config/tge/tge-env" ]; then
    if [ -z "${TGE_DB_PASSWORD:-}" ]; then
        TGE_DB_PASSWORD_LINE=$(grep '^TGE_DB_PASSWORD=' "$HOME/.config/tge/tge-env" || true)
        export TGE_DB_PASSWORD="${TGE_DB_PASSWORD_LINE#TGE_DB_PASSWORD=}"
    fi
    if [ -z "${TUMBILOS_DASHBOARD_PASSWORD:-}" ]; then
        TUMBILOS_PW_LINE=$(grep '^TUMBILOS_DASHBOARD_PASSWORD=' "$HOME/.config/tge/tge-env" || true)
        export TUMBILOS_DASHBOARD_PASSWORD="${TUMBILOS_PW_LINE#TUMBILOS_DASHBOARD_PASSWORD=}"
    fi
fi

TUMBILOS_DIR="$HOME/tumbil/tumbil-os"
DASHBOARD_DIR="$TUMBILOS_DIR/dashboard"
DEPLOY_REPO="$TUMBILOS_DIR/dashboard-deploy"
PASSWORD="${TUMBILOS_DASHBOARD_PASSWORD:?TUMBILOS_DASHBOARD_PASSWORD must be set (in env or ~/.config/tge/tge-env)}"

echo "[TumbilOS] Starting deploy at $(date)"

# Step 1: Sync data from TGE reports + live dashboard data
echo "[TumbilOS] Syncing dashboard payloads..."
SYNC_PYTHON="$TUMBILOS_DIR/.venv/bin/python3"
[ ! -f "$SYNC_PYTHON" ] && SYNC_PYTHON="$HOME/tumbil/tge/.venv/bin/python3"
[ ! -f "$SYNC_PYTHON" ] && SYNC_PYTHON="python3"
$SYNC_PYTHON "$TUMBILOS_DIR/scripts/sync_data.py"
$SYNC_PYTHON "$TUMBILOS_DIR/scripts/sync_live_dashboard_data.py"
$SYNC_PYTHON "$TUMBILOS_DIR/scripts/sync_customer_details.py"
$SYNC_PYTHON "$TUMBILOS_DIR/scripts/sync_service_details.py"

# Step 1.5: Browser-level regression gate before publishing
if [ "${TUMBILOS_SKIP_TESTS:-0}" = "1" ]; then
    echo "[TumbilOS] Skipping regression tests because TUMBILOS_SKIP_TESTS=1"
else
    echo "[TumbilOS] Running regression tests..."
    "$TUMBILOS_DIR/scripts/test_tumbilos.sh" full
fi

# Step 2: Encrypt the dashboard HTML with password
echo "[TumbilOS] Encrypting dashboard..."
TMPDIR=$(mktemp -d)
npx --yes staticrypt "$DASHBOARD_DIR/index.html" -p "$PASSWORD" -d "$TMPDIR" --remember 30 --title "TumbilOS" -t "$DASHBOARD_DIR/password_template.html" --template-button "UNLOCK" --template-instructions "Enter the dashboard password." --short 2>/dev/null
cp "$DASHBOARD_DIR/data.json" "$TMPDIR/"
STATICRYPT_PASSWORD="$PASSWORD" node "$TUMBILOS_DIR/scripts/encrypt_dashboard_payloads.js" "$TMPDIR" live.json priorities.json customers.json service-details.json

# Step 3: Ensure deploy repo exists (shallow clone, gh-pages only)
if [ ! -d "$DEPLOY_REPO/.git" ]; then
    echo "[TumbilOS] Cloning deploy repo..."
    rm -rf "$DEPLOY_REPO"
    git clone --single-branch --branch gh-pages --depth 1 \
        git@github-tumbil-os:tumbil-org/tumbil-os.git "$DEPLOY_REPO"
    cd "$DEPLOY_REPO"
    git config user.email "cliffpeskin@gmail.com"
    git config user.name "Cliff Peskin"
else
    cd "$DEPLOY_REPO"
    git pull --rebase origin gh-pages 2>/dev/null || true
fi

# Step 4: Update files and push
cp "$TMPDIR/index.html" "$DEPLOY_REPO/"
cp "$TMPDIR/data.json" "$DEPLOY_REPO/"
cp "$TMPDIR/live.json" "$DEPLOY_REPO/"
cp "$TMPDIR/priorities.json" "$DEPLOY_REPO/"
cp "$TMPDIR/customers.json" "$DEPLOY_REPO/"
cp "$TMPDIR/service-details.json" "$DEPLOY_REPO/"
cp -R "$DASHBOARD_DIR/fonts" "$DEPLOY_REPO/"
cp "$DASHBOARD_DIR/favicon.svg" "$DEPLOY_REPO/"
rm -rf "$TMPDIR"

cd "$DEPLOY_REPO"
if git diff --quiet index.html data.json live.json priorities.json customers.json service-details.json fonts favicon.svg 2>/dev/null; then
    echo "[TumbilOS] No changes to deploy."
else
    git add index.html data.json live.json priorities.json customers.json service-details.json fonts favicon.svg
    git commit -m "Update dashboard data $(date +%Y-%m-%d)"
    git push origin gh-pages
    echo "[TumbilOS] Deployed successfully."
fi

# Step 5: Commit fresh dashboard payload plaintexts back to main so other
# consumers of the tumbil-os repo see the same content that the encrypted
# bundle on gh-pages was built from. Without this step, the periodic git
# pull on ThinkPad wipes the freshly-generated files and Mac's auto-sync
# pushes a stale dashboard/data.json that lives on forever in main while
# gh-pages drifts ahead.
cd "$TUMBILOS_DIR"
if git diff --quiet dashboard/data.json dashboard/live.json dashboard/customers.json dashboard/service-details.json 2>/dev/null; then
    echo "[TumbilOS] Dashboard payloads already match main; nothing to commit."
else
    git add dashboard/data.json dashboard/live.json dashboard/customers.json dashboard/service-details.json
    git commit -m "Refresh dashboard payloads $(date +%Y-%m-%d)" --no-gpg-sign
    if git push origin main 2>&1; then
        echo "[TumbilOS] Dashboard payloads pushed to main."
    else
        echo "[TumbilOS] WARN: dashboard payloads commit landed locally but push to main failed; will retry next deploy."
    fi
fi

# Executor runs on ThinkPad (X1 Carbon) via systemd cron, not here.
# It fetches data.json from GitHub Pages after this deploy publishes it.

echo "[TumbilOS] Done at $(date)"
