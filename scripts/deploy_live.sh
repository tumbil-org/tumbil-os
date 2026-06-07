#!/bin/bash
# TumbilOS Live Deploy - refreshes live.json/priorities.json and POSTs them
# to the Render service (os.tumbil.com). Render serves the dashboard HTML
# directly, no encryption layer required.

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

echo "[TumbilOS Live] Starting deploy at $(date)"

SYNC_PYTHON="$TUMBILOS_DIR/.venv/bin/python3"
[ ! -f "$SYNC_PYTHON" ] && SYNC_PYTHON="$HOME/tumbil/tge/.venv/bin/python3"
[ ! -f "$SYNC_PYTHON" ] && SYNC_PYTHON="python3"
"$SYNC_PYTHON" "$TUMBILOS_DIR/scripts/sync_live_dashboard_data.py"
"$SYNC_PYTHON" "$TUMBILOS_DIR/scripts/sync_customer_details.py"
"$SYNC_PYTHON" "$TUMBILOS_DIR/scripts/sync_service_details.py"

# The live deploy also uploads data.json, so it must not publish a new live
# date unless the prior business day exists in dashboard history.
"$SYNC_PYTHON" "$TUMBILOS_DIR/scripts/check_dashboard_data_contract.py" \
    --repair --sync-python "$SYNC_PYTHON"

if [ -n "${TUMBILOS_RENDER_URL:-}" ] && [ -n "${TUMBILOS_RENDER_UPLOAD_TOKEN:-}" ]; then
    "$TUMBILOS_DIR/scripts/upload_to_render.sh"
    echo "[TumbilOS Live] Deployed successfully."
else
    echo "[TumbilOS Live] WARN: Render credentials missing; data refreshed locally but not uploaded." >&2
fi

echo "[TumbilOS Live] Done at $(date)"
