#!/usr/bin/env python3
"""Build rolling tips and ratings drill-down payload for TumbilOS."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_DIR / "dashboard"
OUTPUT_FILE = DASHBOARD_DIR / "service-details.json"
LOCAL_TZ = ZoneInfo("America/Toronto")
UTC = ZoneInfo("UTC")
ROLLING_DAYS = 45

_HOME = Path.home()
for _libs in (
    _HOME / "tumbil" / "dev-ops" / "libs",
    _HOME / "tumbil" / "infrastructure" / "libs",
    _HOME / "infrastructure" / "libs",
):
    if _libs.is_dir() and str(_libs) not in sys.path:
        sys.path.insert(0, str(_libs))
        break
import query_db  # noqa: E402


def et_bounds_for_window(local_start: datetime, local_end: datetime) -> tuple[str, str]:
    return (
        local_start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        local_end.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
    )


def fmt_et(value, fmt: str = "%Y-%m-%d %I:%M %p") -> str | None:
    if value is None:
        return None
    if hasattr(value, "tzinfo") and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TZ).strftime(fmt)


def full_name(first: str | None, last: str | None, fallback: str) -> str:
    value = f"{first or ''} {last or ''}".strip()
    return value or fallback


def fetch_tip_rows(conn, start_utc: str, end_utc: str) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT
            p.order_id,
            p.transaction_amount AS tip_cents,
            p.captured_at AS tipped_utc,
            o.user_id AS customer_id,
            o.washpro_id,
            o.status,
            o.delivery_type,
            c.firstname AS customer_firstname,
            c.lastname AS customer_lastname,
            w.firstname AS washpro_firstname,
            w.lastname AS washpro_lastname
        FROM payments p
        JOIN orders o ON o.id = p.order_id
        LEFT JOIN users c ON c.id = o.user_id
        LEFT JOIN users w ON w.id = o.washpro_id
        WHERE p.transaction_type = 'tips'
          AND p.status = 'captured'
          AND p.captured_at >= %s AND p.captured_at < %s
        ORDER BY p.captured_at DESC, p.order_id DESC
    """, (start_utc, end_utc))
    rows = []
    for row in cur.fetchall():
        rows.append({
            "date": fmt_et(row.get("tipped_utc"), "%Y-%m-%d"),
            "order_id": int(row["order_id"]),
            "customer_id": int(row["customer_id"]),
            "washpro_id": int(row["washpro_id"]) if row.get("washpro_id") is not None else None,
            "customer_name": full_name(row.get("customer_firstname"), row.get("customer_lastname"), "Unknown customer"),
            "washpro_name": full_name(row.get("washpro_firstname"), row.get("washpro_lastname"), "Unassigned WashPro"),
            "tip_cad": query_db.cents_to_cad(row.get("tip_cents")),
            "captured_at_et": fmt_et(row.get("tipped_utc")),
            "status": row.get("status"),
            "delivery_type": row.get("delivery_type"),
        })
    cur.close()
    return rows


def fetch_rating_rows(conn, start_utc: str, end_utc: str) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT
            r.order_id,
            r.rating,
            r.created_at AS rated_utc,
            o.user_id AS customer_id,
            o.washpro_id,
            o.status,
            o.delivery_type,
            c.firstname AS customer_firstname,
            c.lastname AS customer_lastname,
            w.firstname AS washpro_firstname,
            w.lastname AS washpro_lastname
        FROM order_ratings r
        JOIN orders o ON o.id = r.order_id
        LEFT JOIN users c ON c.id = o.user_id
        LEFT JOIN users w ON w.id = o.washpro_id
        WHERE r.created_at >= %s AND r.created_at < %s
        ORDER BY r.created_at DESC, r.order_id DESC
    """, (start_utc, end_utc))
    rows = []
    for row in cur.fetchall():
        rating = int(row["rating"] or 0)
        rows.append({
            "date": fmt_et(row.get("rated_utc"), "%Y-%m-%d"),
            "order_id": int(row["order_id"]),
            "customer_id": int(row["customer_id"]),
            "washpro_id": int(row["washpro_id"]) if row.get("washpro_id") is not None else None,
            "customer_name": full_name(row.get("customer_firstname"), row.get("customer_lastname"), "Unknown customer"),
            "washpro_name": full_name(row.get("washpro_firstname"), row.get("washpro_lastname"), "Unassigned WashPro"),
            "rating": rating,
            "submitted_at_et": fmt_et(row.get("rated_utc")),
            "status": row.get("status"),
            "delivery_type": row.get("delivery_type"),
            "is_low_rating": rating <= 3,
            "is_five_star": rating == 5,
        })
    cur.close()
    return rows


def build_payload(conn_factory, now_et: datetime | None = None) -> dict:
    now_et = now_et or datetime.now(LOCAL_TZ)
    start_et = (now_et - timedelta(days=ROLLING_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc, end_utc = et_bounds_for_window(start_et, now_et)

    conn = conn_factory()
    try:
        tip_rows = fetch_tip_rows(conn, start_utc, end_utc)
        rating_rows = fetch_rating_rows(conn, start_utc, end_utc)
    finally:
        conn.close()

    tip_by_date: dict[str, list[dict]] = defaultdict(list)
    for row in tip_rows:
        tip_by_date[row["date"]].append({k: v for k, v in row.items() if k != "date"})

    rating_by_date: dict[str, list[dict]] = defaultdict(list)
    for row in rating_rows:
        rating_by_date[row["date"]].append({k: v for k, v in row.items() if k != "date"})

    days = []
    cursor = start_et
    while cursor.date() <= now_et.date():
        date = cursor.strftime("%Y-%m-%d")
        tips = tip_by_date.get(date, [])
        ratings = rating_by_date.get(date, [])
        rating_count = len(ratings)
        five_star_count = sum(1 for row in ratings if row["is_five_star"])
        low_rating_count = sum(1 for row in ratings if row["is_low_rating"])
        avg_rating = round(sum(row["rating"] for row in ratings) / rating_count, 2) if rating_count else 0.0
        days.append({
            "date": date,
            "tips": {
                "tip_count": len(tips),
                "total_tips_cad": round(sum(row["tip_cad"] for row in tips), 2),
                "rows": tips,
            },
            "ratings": {
                "rating_count": rating_count,
                "avg_rating": avg_rating,
                "five_star_count": five_star_count,
                "low_rating_count": low_rating_count,
                "rows": ratings,
            },
        })
        cursor += timedelta(days=1)

    return {
        "version": 1,
        "generated_at": datetime.now(LOCAL_TZ).isoformat(),
        "timezone": "America/Toronto",
        "coverage_days": ROLLING_DAYS,
        "days": days,
    }


def main() -> None:
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    password = query_db.get_db_password()

    def conn_factory():
        return query_db.pymysql.connect(
            host="127.0.0.1",
            port=query_db.LOCAL_PORT,
            user=query_db.DB_USER,
            password=password,
            database=query_db.DB_NAME,
            cursorclass=query_db.pymysql.cursors.DictCursor,
            connect_timeout=10,
        )

    with query_db.ssh_tunnel():
        payload = build_payload(conn_factory)

    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Service detail payload written to {OUTPUT_FILE}")
    print(f"  Days: {len(payload['days'])}")
    print(f"  Latest date: {payload['days'][-1]['date'] if payload['days'] else 'n/a'}")


if __name__ == "__main__":
    main()
