#!/usr/bin/env python3
"""Build rolling customer drill-down payload for TumbilOS."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_DIR / "dashboard"
OUTPUT_FILE = DASHBOARD_DIR / "customers.json"
LOCAL_TZ = ZoneInfo("America/Toronto")
UTC = ZoneInfo("UTC")
ROLLING_DAYS = 45
RECENT_SOURCE_LOOKBACK_DAYS = 7
BQ_CREDENTIALS = Path.home() / ".config/gcloud/tumbil-crashlytics-sa.json"

_HOME = Path.home()
for _libs in (_HOME / "tumbil" / "infrastructure" / "libs", _HOME / "infrastructure" / "libs"):
    if _libs.is_dir() and str(_libs) not in sys.path:
        sys.path.insert(0, str(_libs))
        break
import query_db  # noqa: E402
from ga4_attribution import classify_ga4_rows, fetch_ga4_purchase_attribution, related_ga4_rows  # noqa: E402


def et_bounds_for_window(local_start: datetime, local_end: datetime) -> tuple[str, str]:
    return (
        local_start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        local_end.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
    )


def customer_type(prior_completed: int) -> str:
    if prior_completed <= 0:
        return "brand_new"
    if prior_completed == 1:
        return "second_order"
    return "regular"


def source_for_customer(kind: str) -> dict:
    if kind != "brand_new":
        return {
            "bucket": "Returning customer",
            "confidence": "high",
            "detail": "Source is only classified on first-order acquisition.",
        }
    return {
        "bucket": "Direct / Unknown",
        "confidence": "low",
        "detail": "Historical source data is not retained in the DB.",
    }


def fetch_recent_ga4_events(customers: list[dict], start_suffix: str, end_suffix: str) -> tuple[list[dict], dict]:
    recent_new = [row for row in customers if row["customer_type"] == "brand_new"]
    if not recent_new:
        return [], {"status": "skipped", "reason": "No recent brand-new customers"}

    order_sql = ", ".join(json.dumps(str(row["order_id"])) for row in recent_new)
    user_sql = ", ".join(json.dumps(str(row["customer_id"])) for row in recent_new)
    start_date = datetime.strptime(start_suffix, "%Y%m%d").strftime("%Y-%m-%d")
    end_date = datetime.strptime(end_suffix, "%Y%m%d").strftime("%Y-%m-%d")
    user_ids = [str(row["customer_id"]) for row in recent_new]
    data_api_rows, data_api_status = fetch_ga4_purchase_attribution(start_date, end_date, user_ids)

    sql = f"""
    WITH events AS (
      SELECT
        'ga4_bigquery' AS source_system,
        event_name,
        TIMESTAMP_MICROS(event_timestamp) AS event_ts,
        platform,
        user_pseudo_id,
        traffic_source.source AS traffic_source,
        traffic_source.medium AS traffic_medium,
        traffic_source.name AS traffic_campaign,
        session_traffic_source_last_click.cross_channel_campaign.source_platform AS session_source_platform,
        session_traffic_source_last_click.google_ads_campaign.campaign_name AS session_google_ads_campaign_name,
        session_traffic_source_last_click.cross_channel_campaign.source AS session_source,
        session_traffic_source_last_click.cross_channel_campaign.medium AS session_medium,
        session_traffic_source_last_click.cross_channel_campaign.campaign_name AS session_campaign,
        session_traffic_source_last_click.cross_channel_campaign.default_channel_group AS session_default_channel_group,
        (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'order_id') AS order_id,
        COALESCE(
          (SELECT value.string_value FROM UNNEST(user_properties) WHERE key = 'tumbil_id'),
          CAST((SELECT value.int_value FROM UNNEST(user_properties) WHERE key = 'tumbil_id') AS STRING)
        ) AS tumbil_id,
        (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'source') AS event_source,
        (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'medium') AS event_medium,
        (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'campaign') AS event_campaign,
        COALESCE(
          collected_traffic_source.gclid,
          (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'gclid')
        ) AS gclid
      FROM `customer-app-ab2c8.analytics_489942104.events_*`
      WHERE _TABLE_SUFFIX BETWEEN {json.dumps(start_suffix)} AND {json.dumps(end_suffix)}
        AND event_name IN ('purchase', 'sign_up', 'first_open', 'session_start')
    )
    SELECT *
    FROM events
    WHERE order_id IN ({order_sql})
       OR tumbil_id IN ({user_sql})
    ORDER BY tumbil_id ASC, event_ts ASC
    """

    if not shutil.which("bq"):
        bq_rows, bq_status = run_bigquery_rest(sql)
        rows = data_api_rows + bq_rows
        return rows, {
            "status": "ok" if rows else "partial",
            "primary": "ga4_data_api",
            "rows": len(rows),
            "ga4_data_api": data_api_status,
            "bigquery": bq_status,
        }

    cmd = [
        "bq", "query",
        "--use_legacy_sql=false",
        "--format=json",
        "--project_id=customer-app-ab2c8",
        f"--service_account_credential_file={BQ_CREDENTIALS}",
        sql,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return [], {"status": "error", "reason": str(exc)}
    if result.returncode != 0:
        return data_api_rows, {
            "status": "partial" if data_api_rows else "error",
            "primary": "ga4_data_api",
            "rows": len(data_api_rows),
            "ga4_data_api": data_api_status,
            "bigquery": {"status": "error", "reason": result.stderr[-500:]},
        }
    try:
        bq_rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return data_api_rows, {
            "status": "partial" if data_api_rows else "error",
            "primary": "ga4_data_api",
            "rows": len(data_api_rows),
            "ga4_data_api": data_api_status,
            "bigquery": {"status": "error", "reason": f"Invalid bq JSON: {exc}"},
        }
    rows = data_api_rows + bq_rows
    bq_status = {"status": "ok", "rows": len(bq_rows), "method": "bq"}
    ok_sources = [
        name for name, status in (("ga4_data_api", data_api_status), ("bigquery", bq_status))
        if status.get("status") == "ok"
    ]
    return rows, {
        "status": "ok" if ok_sources else "error",
        "primary": "ga4_data_api",
        "ok_sources": ok_sources,
        "rows": len(rows),
        "ga4_data_api": data_api_status,
        "bigquery": bq_status,
    }


def parse_bq_rest_rows(response: dict) -> list[dict]:
    fields = response.get("schema", {}).get("fields", [])
    rows = []
    for row in response.get("rows", []) or []:
        values = row.get("f", [])
        parsed = {}
        for idx, field in enumerate(fields):
            parsed[field.get("name", f"field_{idx}")] = values[idx].get("v") if idx < len(values) else None
        rows.append(parsed)
    return rows


def run_bigquery_rest(sql: str) -> tuple[list[dict], dict]:
    if not BQ_CREDENTIALS.exists():
        return [], {"status": "unavailable", "reason": "BigQuery service account file not found"}
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            str(BQ_CREDENTIALS),
            scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
        )
        credentials.refresh(Request())
        body = json.dumps({
            "query": sql,
            "useLegacySql": False,
            "timeoutMs": 90000,
        }).encode("utf-8")
        request = urllib.request.Request(
            "https://bigquery.googleapis.com/bigquery/v2/projects/customer-app-ab2c8/queries",
            data=body,
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=100) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except ImportError as exc:
        return [], {"status": "unavailable", "reason": f"Google auth library unavailable: {exc}"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-500:]
        return [], {"status": "error", "reason": detail}
    except Exception as exc:
        return [], {"status": "error", "reason": str(exc)}

    if not payload.get("jobComplete", True):
        return [], {"status": "error", "reason": "BigQuery job did not complete within timeout"}
    rows = parse_bq_rest_rows(payload)
    return rows, {"status": "ok", "rows": len(rows), "method": "rest"}


def build_recent_source_map(customers: list[dict], ga4_rows: list[dict]) -> dict[tuple[int, int], dict]:
    source_map: dict[tuple[int, int], dict] = {}
    for customer in customers:
        if customer["customer_type"] != "brand_new":
            continue

        related = related_ga4_rows(ga4_rows, customer["order_id"], customer["customer_id"])
        if not related:
            source_map[(customer["customer_id"], customer["order_id"])] = {
                "bucket": "Direct / Unknown",
                "confidence": "low",
                "detail": "No GA4 attribution match for this user in the retained event window. AppsFlyer is not joined into this dashboard yet.",
                "match_quality": "unmatched",
            }
            continue

        classification = classify_ga4_rows(related)
        source_map[(customer["customer_id"], customer["order_id"])] = {
            "bucket": classification["bucket"],
            "confidence": classification["confidence"],
            "detail": classification["reason"],
            "match_quality": classification.get("match_quality"),
            "campaign": classification.get("campaign"),
        }
    return source_map


def fmt_et(value, fmt: str = "%Y-%m-%d %I:%M %p") -> str | None:
    if value is None:
        return None
    if hasattr(value, "tzinfo") and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TZ).strftime(fmt)


def fetch_customer_rows(conn, start_utc: str, end_utc: str) -> list[dict]:
    cur = conn.cursor()
    cur.execute(f"""
        SELECT
            o.id AS order_id,
            o.user_id,
            ot.timestamp AS placed_utc,
            CONCAT(COALESCE(u.firstname, ''), ' ', COALESCE(u.lastname, '')) AS customer_name,
            u.firstname,
            u.lastname,
            u.email,
            u.mobile,
            u.created_at AS signup_utc,
            o.status,
            o.delivery_type,
            o.pickup_by AS pickup_by_utc,
            o.delivery_by AS delivery_by_utc,
            o.small_bags_count,
            o.regular_bags_count,
            o.large_bags_count,
            o.oversize_items_count,
            COALESCE(od.weight, 0) AS weight_lbs,
            COALESCE(od.estimated_amount, 0) AS estimated_amount_gross_cents,
            (
                SELECT COUNT(*)
                FROM orders o2
                JOIN order_timelines delivered2
                  ON delivered2.order_id = o2.id AND delivered2.type = 'delivered'
                WHERE o2.user_id = o.user_id
                  AND o2.status = 'completed'
                  AND delivered2.timestamp < ot.timestamp
            ) AS prior_completed,
            pc.code AS referral_code,
            CONCAT(COALESCE(ref_user.firstname, ''), ' ', COALESCE(ref_user.lastname, '')) AS referral_owner_name
        FROM payments p
        JOIN orders o ON o.id = p.order_id
        JOIN order_timelines ot ON ot.order_id = p.order_id AND ot.type = 'placed'
        JOIN users u ON u.id = o.user_id
        LEFT JOIN order_details od ON od.order_id = o.id
        LEFT JOIN promo_code_usages pcu ON pcu.order_id = o.id
        LEFT JOIN promo_codes pc ON pc.id = pcu.promo_code_id AND pc.type = 'referral'
        LEFT JOIN users ref_user ON ref_user.id = pc.owner_id
        WHERE {query_db.PLACED_FILTER}
          AND ot.timestamp >= %s AND ot.timestamp < %s
        ORDER BY ot.timestamp DESC, o.id DESC
    """, (start_utc, end_utc))
    rows = []
    for row in cur.fetchall():
        prior_completed = int(row["prior_completed"] or 0)
        kind = customer_type(prior_completed)
        referral_owner_name = (row.get("referral_owner_name") or "").strip() or None
        referral_code = row.get("referral_code")
        source = source_for_customer(kind)
        rows.append({
            "date": fmt_et(row.get("placed_utc"), "%Y-%m-%d"),
            "customer_type": kind,
            "customer_id": int(row["user_id"]),
            "order_id": int(row["order_id"]),
            "name": (row.get("customer_name") or "").strip() or "Unknown",
            "email": row.get("email"),
            "mobile": row.get("mobile"),
            "placed_at_et": fmt_et(row.get("placed_utc")),
            "signup_at_et": fmt_et(row.get("signup_utc")),
            "source": source,
            "referral_code": referral_code,
            "referral": {
                "code": referral_code,
                "owner_name": referral_owner_name,
            } if referral_code else None,
            "status": row.get("status"),
            "delivery_type": row.get("delivery_type"),
            "pickup_by_et": fmt_et(row.get("pickup_by_utc")),
            "delivery_by_et": fmt_et(row.get("delivery_by_utc")),
            "bags": {
                "small": int(row.get("small_bags_count") or 0),
                "regular": int(row.get("regular_bags_count") or 0),
                "large": int(row.get("large_bags_count") or 0),
                "oversize": int(row.get("oversize_items_count") or 0),
            },
            "weight_lbs": round(float(row.get("weight_lbs") or 0), 1),
            "order_value_cad": query_db.cents_to_net_cad(row.get("estimated_amount_gross_cents")),
            "prior_completed_orders": prior_completed,
        })
    cur.close()
    return rows


def build_payload(conn_factory, now_et: datetime | None = None) -> dict:
    now_et = now_et or datetime.now(LOCAL_TZ)
    start_et = (now_et - timedelta(days=ROLLING_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc, end_utc = et_bounds_for_window(start_et, now_et)
    recent_start_et = (now_et - timedelta(days=RECENT_SOURCE_LOOKBACK_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    conn = conn_factory()
    try:
        rows = fetch_customer_rows(conn, start_utc, end_utc)
    finally:
        conn.close()

    recent_customers = [row for row in rows if row["date"] >= recent_start_et.strftime("%Y-%m-%d")]
    ga4_start_suffix = recent_start_et.strftime("%Y%m%d")
    ga4_end_suffix = now_et.strftime("%Y%m%d")
    ga4_rows, ga4_status = fetch_recent_ga4_events(recent_customers, ga4_start_suffix, ga4_end_suffix)
    recent_source_map = build_recent_source_map(recent_customers, ga4_rows)
    for row in rows:
        key = (row["customer_id"], row["order_id"])
        if key in recent_source_map:
            row["source"] = recent_source_map[key]

    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[row["date"]][row["customer_type"]].append({k: v for k, v in row.items() if k not in {"date", "customer_type"}})

    # Emit a row for every day in the rolling window (zero-filled when no
    # orders were placed), mirroring sync_service_details.py. This guarantees
    # the live date is always covered even on a zero-activity day, so the
    # dashboard data contract (which requires the live date in customers.json)
    # passes and the live deploy uploads instead of dying before upload_to_render.
    days = []
    cursor = start_et
    while cursor.date() <= now_et.date():
        date = cursor.strftime("%Y-%m-%d")
        bucket = grouped.get(date, {})
        customers_by_type = {
            "brand_new": bucket.get("brand_new", []),
            "second_order": bucket.get("second_order", []),
            "regular": bucket.get("regular", []),
        }
        days.append({
            "date": date,
            "counts": {key: len(val) for key, val in customers_by_type.items()},
            "customers_by_type": customers_by_type,
        })
        cursor += timedelta(days=1)

    return {
        "version": 1,
        "generated_at": datetime.now(LOCAL_TZ).isoformat(),
        "timezone": "America/Toronto",
        "coverage_days": ROLLING_DAYS,
        "recent_source_window_days": RECENT_SOURCE_LOOKBACK_DAYS,
        "ga4_source_status": ga4_status,
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
    print(f"Customer detail payload written to {OUTPUT_FILE}")
    print(f"  Days: {len(payload['days'])}")
    print(f"  Latest date: {payload['days'][-1]['date'] if payload['days'] else 'n/a'}")


if __name__ == "__main__":
    main()
