#!/bin/bash
# TumbilOS Live Deploy - refreshes live.json/priorities.json for GitHub Pages.

set -e

if [ "$(uname)" != "Darwin" ]; then
    export PATH="$HOME/.nvm/versions/node/v22.22.0/bin:$PATH"
    export PYTHONPATH="$HOME/.local/lib/python3.12/site-packages:/usr/lib/python3/dist-packages:${PYTHONPATH:-}"
fi

# Match the daily TGE systemd environment when running manually over SSH.
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
: "${TUMBILOS_DASHBOARD_PASSWORD:?TUMBILOS_DASHBOARD_PASSWORD must be set (in env or ~/.config/tge/tge-env)}"

echo "[TumbilOS Live] Starting deploy at $(date)"

SYNC_PYTHON="$TUMBILOS_DIR/.venv/bin/python3"
[ ! -f "$SYNC_PYTHON" ] && SYNC_PYTHON="$HOME/tumbil/tge/.venv/bin/python3"
[ ! -f "$SYNC_PYTHON" ] && SYNC_PYTHON="python3"
"$SYNC_PYTHON" "$TUMBILOS_DIR/scripts/sync_live_dashboard_data.py"
"$SYNC_PYTHON" "$TUMBILOS_DIR/scripts/sync_customer_details.py"
"$SYNC_PYTHON" "$TUMBILOS_DIR/scripts/sync_service_details.py"

# Upload to Render service (the new fast-update host)
if [ -n "${TUMBILOS_RENDER_URL:-}" ] && [ -n "${TUMBILOS_RENDER_UPLOAD_TOKEN:-}" ]; then
    "$TUMBILOS_DIR/scripts/upload_to_render.sh" || echo "[TumbilOS Live] WARN: Render upload failed"
fi

if [ ! -d "$DEPLOY_REPO/.git" ]; then
    echo "[TumbilOS Live] Deploy repo missing; run full deploy first."
    exit 1
fi

cd "$DEPLOY_REPO"
git pull --rebase origin gh-pages 2>/dev/null || true

TMPDIR=$(mktemp -d)
STATICRYPT_PASSWORD="$TUMBILOS_DASHBOARD_PASSWORD" node "$TUMBILOS_DIR/scripts/encrypt_dashboard_payloads.js" "$TMPDIR" live.json priorities.json customers.json service-details.json
cp "$TMPDIR/live.json" "$DEPLOY_REPO/"
cp "$TMPDIR/priorities.json" "$DEPLOY_REPO/"
cp "$TMPDIR/customers.json" "$DEPLOY_REPO/"
cp "$TMPDIR/service-details.json" "$DEPLOY_REPO/"
rm -rf "$TMPDIR"

if git diff --quiet live.json priorities.json customers.json service-details.json 2>/dev/null; then
    echo "[TumbilOS Live] No changes to deploy."
else
    git add live.json priorities.json customers.json service-details.json
    git commit -m "Update live dashboard data $(date +%Y-%m-%dT%H:%M)"
    git push origin gh-pages
    echo "[TumbilOS Live] Deployed successfully."
fi

echo "[TumbilOS Live] Done at $(date)"
