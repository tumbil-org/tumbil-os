#!/usr/bin/env python3
"""Small TumbilOS-native priorities API.

This is standalone Tumbil infrastructure, not a Hotshots backend change.
Writes are token-authenticated and persisted to dashboard/priorities.json plus
an append-only audit log.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_DIR / "dashboard"
PRIORITIES_FILE = DASHBOARD_DIR / "priorities.json"
AUDIT_FILE = DASHBOARD_DIR / "priorities-audit.jsonl"
LOCAL_TZ = ZoneInfo("America/Toronto")

COLUMNS = [
    "BACKBURNER / ONGOING",
    "NEW FOR DISCUSSION",
    "BACKLOG",
    "IN PROGRESS",
    "DONE FOR ENG/DISCUSS",
    "DONE & ARCHIVED",
]
AREAS = ["Product", "Finance", "Eng", "AI Infra"]
PRIORITIES = ["P0", "P1", "P2", "P3"]


def now_iso() -> str:
    return datetime.now(LOCAL_TZ).isoformat()


def default_payload() -> dict:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "columns": COLUMNS,
        "areas": AREAS,
        "items": [],
    }


def read_payload() -> dict:
    if not PRIORITIES_FILE.exists():
        write_payload(default_payload(), actor="system", action="init", detail={})
    try:
        data = json.loads(PRIORITIES_FILE.read_text())
    except json.JSONDecodeError:
        data = default_payload()
    if not isinstance(data.get("columns"), list) or not data["columns"]:
        data["columns"] = COLUMNS
    if not isinstance(data.get("areas"), list) or not data["areas"]:
        data["areas"] = AREAS
    data.setdefault("items", [])
    return data


def append_audit(actor: str, action: str, detail: dict) -> None:
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": now_iso(),
        "actor": actor,
        "action": action,
        "detail": detail,
    }
    with AUDIT_FILE.open("a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def write_payload(data: dict, actor: str, action: str, detail: dict) -> dict:
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    data["version"] = 1
    data["updated_at"] = now_iso()
    if not isinstance(data.get("columns"), list) or not data["columns"]:
        data["columns"] = COLUMNS
    if not isinstance(data.get("areas"), list) or not data["areas"]:
        data["areas"] = AREAS
    PRIORITIES_FILE.write_text(json.dumps(data, indent=2))
    append_audit(actor, action, detail)
    return data


def normalize_item(
    raw: dict,
    existing: dict | None = None,
    columns: list[str] | None = None,
    areas: list[str] | None = None,
) -> dict:
    existing = existing or {}
    columns = columns or COLUMNS
    areas = areas or AREAS
    status = raw.get("status", existing.get("status", columns[0]))
    area = raw.get("area", existing.get("area", "Product"))
    priority = raw.get("priority", existing.get("priority", "P1"))
    if status not in columns:
        raise ValueError(f"status must be one of {', '.join(columns)}")
    if area not in areas:
        raise ValueError(f"area must be one of {', '.join(areas)}")
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")

    title = str(raw.get("title", existing.get("title", ""))).strip()
    if not title:
        raise ValueError("title is required")

    return {
        "id": existing.get("id") or raw.get("id") or uuid.uuid4().hex,
        "title": title,
        "owner": str(raw.get("owner", existing.get("owner", "Unassigned"))).strip() or "Unassigned",
        "area": area,
        "priority": priority,
        "status": status,
        "target_date": raw.get("target_date", existing.get("target_date")),
        "why": str(raw.get("why", existing.get("why", ""))).strip(),
        "links": raw.get("links", existing.get("links", [])) or [],
        "notes": str(raw.get("notes", existing.get("notes", ""))).strip(),
        "sort": int(raw.get("sort", existing.get("sort", 0)) or 0),
        "created_at": existing.get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "updated_by": str(raw.get("updated_by", existing.get("updated_by", "unknown"))).strip() or "unknown",
    }


def bearer_token(header: str | None) -> str:
    if not header:
        return ""
    prefix = "Bearer "
    return header[len(prefix):].strip() if header.startswith(prefix) else header.strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "TumbilOSPriorityAPI/1.0"

    def end_headers(self) -> None:
        origin = os.environ.get("TUMBILOS_CORS_ORIGIN", "https://tumbil-org.github.io")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self.json_response({"status": "ok", "time": now_iso()})
            return
        if path == "/v1/priorities":
            if not self.authorized():
                return
            self.json_response(read_payload())
            return
        self.error_response(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        if not self.authorized():
            return
        path = urlparse(self.path).path
        if path == "/v1/priorities":
            self.create_item()
            return
        if path == "/v1/priorities/reorder":
            self.reorder_items()
            return
        self.error_response(HTTPStatus.NOT_FOUND, "Not found")

    def do_PATCH(self) -> None:
        if not self.authorized():
            return
        path = urlparse(self.path).path
        prefix = "/v1/priorities/"
        if not path.startswith(prefix):
            self.error_response(HTTPStatus.NOT_FOUND, "Not found")
            return
        self.update_item(path[len(prefix):])

    def do_DELETE(self) -> None:
        if not self.authorized():
            return
        path = urlparse(self.path).path
        prefix = "/v1/priorities/"
        if not path.startswith(prefix):
            self.error_response(HTTPStatus.NOT_FOUND, "Not found")
            return
        self.delete_item(path[len(prefix):])

    def authorized(self) -> bool:
        expected = os.environ.get("TUMBILOS_PRIORITY_TOKEN")
        if not expected:
            self.error_response(HTTPStatus.SERVICE_UNAVAILABLE, "TUMBILOS_PRIORITY_TOKEN is not set")
            return False
        supplied = bearer_token(self.headers.get("Authorization"))
        if not secrets.compare_digest(supplied, expected):
            self.error_response(HTTPStatus.UNAUTHORIZED, "Unauthorized")
            return False
        return True

    def actor(self, body: dict | None = None) -> str:
        body = body or {}
        return str(body.get("updated_by") or self.headers.get("X-TumbilOS-Actor") or "unknown")

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}")

    def create_item(self) -> None:
        try:
            body = self.read_body()
        except ValueError as exc:
            self.error_response(HTTPStatus.BAD_REQUEST, str(exc))
            return
        data = read_payload()
        try:
            item = normalize_item(body, columns=data["columns"], areas=data["areas"])
        except ValueError as exc:
            self.error_response(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if item["sort"] == 0:
            same_status = [i for i in data["items"] if i.get("status") == item["status"]]
            item["sort"] = len(same_status) + 1
        data["items"].append(item)
        data = write_payload(data, actor=self.actor(body), action="create", detail={"id": item["id"], "title": item["title"]})
        self.json_response(data, status=HTTPStatus.CREATED)

    def update_item(self, item_id: str) -> None:
        try:
            body = self.read_body()
        except ValueError as exc:
            self.error_response(HTTPStatus.BAD_REQUEST, str(exc))
            return
        data = read_payload()
        for idx, item in enumerate(data["items"]):
            if item.get("id") == item_id:
                try:
                    data["items"][idx] = normalize_item(body, existing=item, columns=data["columns"], areas=data["areas"])
                except ValueError as exc:
                    self.error_response(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                data = write_payload(data, actor=self.actor(body), action="update", detail={"id": item_id})
                self.json_response(data)
                return
        self.error_response(HTTPStatus.NOT_FOUND, "Priority item not found")

    def reorder_items(self) -> None:
        try:
            body = self.read_body()
        except ValueError as exc:
            self.error_response(HTTPStatus.BAD_REQUEST, str(exc))
            return
        order = body.get("order", [])
        if not isinstance(order, list):
            self.error_response(HTTPStatus.BAD_REQUEST, "order must be a list of ids")
            return
        position = {item_id: idx + 1 for idx, item_id in enumerate(order)}
        data = read_payload()
        for item in data["items"]:
            if item["id"] in position:
                item["sort"] = position[item["id"]]
                item["updated_at"] = now_iso()
        data = write_payload(data, actor=self.actor(body), action="reorder", detail={"count": len(order)})
        self.json_response(data)

    def delete_item(self, item_id: str) -> None:
        data = read_payload()
        before = len(data["items"])
        data["items"] = [item for item in data["items"] if item.get("id") != item_id]
        if len(data["items"]) == before:
            self.error_response(HTTPStatus.NOT_FOUND, "Priority item not found")
            return
        data = write_payload(data, actor=self.actor(), action="delete", detail={"id": item_id})
        self.json_response(data)

    def json_response(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error_response(self, status: HTTPStatus, message: str) -> None:
        self.json_response({"error": message}, status=status)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def main() -> None:
    host = os.environ.get("TUMBILOS_PRIORITY_HOST", "127.0.0.1")
    port = int(os.environ.get("TUMBILOS_PRIORITY_PORT", "8765"))
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    read_payload()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"TumbilOS priority API listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
