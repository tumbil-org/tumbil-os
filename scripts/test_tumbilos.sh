#!/bin/bash
# TumbilOS regression harness. Runs browser-level invariants against the
# static dashboard before a deploy is allowed to publish.

set -euo pipefail

if [ "$(uname)" != "Darwin" ]; then
    export PATH="$HOME/.nvm/versions/node/v22.22.0/bin:$PATH"
fi

TUMBILOS_DIR="${TUMBILOS_DIR:-$HOME/tumbil/tumbil-os}"
SCOPE="${1:-quick}"

cd "$TUMBILOS_DIR"

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
