#!/bin/bash
# Upload TumbilOS data JSON files to the Render service.
# Replaces (or runs alongside) the git push to gh-pages.

set -e

DASHBOARD_DIR="$HOME/tumbil/tumbil-os/dashboard"

if [ -z "${TUMBILOS_RENDER_URL:-}" ] || [ -z "${TUMBILOS_RENDER_UPLOAD_TOKEN:-}" ]; then
  echo "[render-upload] TUMBILOS_RENDER_URL and TUMBILOS_RENDER_UPLOAD_TOKEN must be set" >&2
  exit 1
fi

upload_file() {
  local filename="$1"
  local local_path="$DASHBOARD_DIR/$filename"
  if [ ! -f "$local_path" ]; then
    echo "[render-upload] skip $filename (not found)"
    return 0
  fi
  local size=$(wc -c < "$local_path")
  local http_code
  http_code=$(curl -sS -o /tmp/render-upload-resp.json -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer $TUMBILOS_RENDER_UPLOAD_TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary "@$local_path" \
    "$TUMBILOS_RENDER_URL/api/upload/$filename" || echo "000")
  if [ "$http_code" = "200" ]; then
    echo "[render-upload] $filename ($size bytes) -> 200"
  else
    echo "[render-upload] $filename ($size bytes) -> FAILED $http_code" >&2
    cat /tmp/render-upload-resp.json >&2 || true
    return 1
  fi
}

for f in live.json customers.json service-details.json priorities.json data.json; do
  upload_file "$f" || true
done
