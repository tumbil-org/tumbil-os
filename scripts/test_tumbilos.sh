#!/bin/bash
# TumbilOS regression harness. Runs browser-level invariants against the
# static dashboard before a deploy is allowed to publish.

set -euo pipefail

if [ "$(uname)" != "Darwin" ]; then
    export PATH="$HOME/.nvm/versions/node/v22.22.0/bin:$PATH"
    # Playwright does not yet label Ubuntu 26.04 as supported, but the
    # Ubuntu 24.04 Chromium build runs cleanly on the rebuilt ThinkPad.
    export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE="${PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-ubuntu24.04-x64}"
fi

TUMBILOS_DIR="${TUMBILOS_DIR:-$HOME/tumbil/tumbil-os}"
SCOPE="${1:-quick}"

cd "$TUMBILOS_DIR"

CHECK_PYTHON="$TUMBILOS_DIR/.venv/bin/python3"
[ ! -f "$CHECK_PYTHON" ] && CHECK_PYTHON="$HOME/tumbil/tge/.venv/bin/python3"
[ ! -f "$CHECK_PYTHON" ] && CHECK_PYTHON="python3"

"$CHECK_PYTHON" "$TUMBILOS_DIR/scripts/check_dashboard_data_contract.py"
"$CHECK_PYTHON" "$TUMBILOS_DIR/scripts/check_dashboard_incident_regressions.py"

# Offline guard: the drill-down builders must cover the live date even on a
# zero-activity day (the 2026-06-22 midnight-rollover freeze). No DB/network.
"$CHECK_PYTHON" "$TUMBILOS_DIR/scripts/check_payload_coverage.py"

if [ ! -d node_modules/@playwright/test ]; then
    echo "[TumbilOS QA] Installing npm dependencies..."
    npm ci
fi

if [ "${TUMBILOS_SKIP_BROWSER_INSTALL:-0}" != "1" ]; then
    echo "[TumbilOS QA] Ensuring Playwright Chromium is installed..."
    npx playwright install chromium >/dev/null
fi

case "$SCOPE" in
    quick)
        npm run test:tumbilos:quick
        ;;
    full)
        npm run test:tumbilos:full
        ;;
    *)
        echo "Usage: scripts/test_tumbilos.sh [quick|full]" >&2
        exit 2
        ;;
esac
