#!/bin/bash
# TumbilOS Deploy - Syncs the analyst brief + live data and ships them to the
# Render service (os.tumbil.com). Reads the daily analyst brief from
# ~/tumbil/tge/reports/ (TGE writes, TumbilOS reads). Render serves the
# dashboard HTML directly from the tumbilos-service repo, so this script only
# refreshes the JSON payloads here.

set -e

# Ensure node available on ThinkPad (still needed for the regression suite).
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
    if [ -z "${TUMBILOS_RENDER_URL:-}" ]; then
        TUMBILOS_RENDER_URL_LINE=$(grep '^TUMBILOS_RENDER_URL=' "$HOME/.config/tge/tge-env" || true)
        export TUMBILOS_RENDER_URL="${TUMBILOS_RENDER_URL_LINE#TUMBILOS_RENDER_URL=}"
    fi
    if [ -z "${TUMBILOS_RENDER_UPLOAD_TOKEN:-}" ]; then
        TUMBILOS_RENDER_TOKEN_LINE=$(grep '^TUMBILOS_RENDER_UPLOAD_TOKEN=' "$HOME/.config/tge/tge-env" || true)
        export TUMBILOS_RENDER_UPLOAD_TOKEN="${TUMBILOS_RENDER_TOKEN_LINE#TUMBILOS_RENDER_UPLOAD_TOKEN=}"
    fi
fi

TUMBILOS_DIR="$HOME/tumbil/tumbil-os"

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

# Guard the payload contract before browser tests or Render upload. If a late
# TGE repair landed after the first sync, this retries the sync once.
$SYNC_PYTHON "$TUMBILOS_DIR/scripts/check_dashboard_data_contract.py" \
    --repair --sync-python "$SYNC_PYTHON"

# Step 2: Browser-level regression gate before shipping the new payloads.
if [ "${TUMBILOS_SKIP_TESTS:-0}" = "1" ]; then
    echo "[TumbilOS] Skipping regression tests because TUMBILOS_SKIP_TESTS=1"
else
    echo "[TumbilOS] Running regression tests..."
    "$TUMBILOS_DIR/scripts/test_tumbilos.sh" full
fi

# Step 3: Upload fresh JSON payloads to Render.
if [ -n "${TUMBILOS_RENDER_URL:-}" ] && [ -n "${TUMBILOS_RENDER_UPLOAD_TOKEN:-}" ]; then
    "$TUMBILOS_DIR/scripts/upload_to_render.sh"
    echo "[TumbilOS] Deployed to Render."
else
    echo "[TumbilOS] WARN: Render credentials missing; payloads refreshed locally but not uploaded." >&2
fi

# Step 4: Commit fresh dashboard payload plaintexts back to main so other
# consumers of the tumbil-os repo see the same content that Render is serving.
# Without this, the periodic git pull on ThinkPad wipes the freshly-generated
# files and Mac's auto-sync pushes a stale dashboard/data.json that lives on
# forever in main.
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

echo "[TumbilOS] Done at $(date)"
