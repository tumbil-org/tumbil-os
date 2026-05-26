#!/usr/bin/env python3
"""Build TumbilOS live company metrics.

The output is static JSON refreshed by timer, not a browser-to-DB live query.
DB remains authoritative for placed orders, order value, and customer type.
GA4 is used only for best-effort new-customer source labeling.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_DIR / "dashboard"
OUTPUT_FILE = DASHBOARD_DIR / "live.json"
PRIORITIES_FILE = DASHBOARD_DIR / "priorities.json"
APPSFLYER_CREDENTIALS_FILE = Path.home() / ".config" / "appsflyer" / "credentials.json"
APPSFLYER_CACHE_FILE = DASHBOARD_DIR / "appsflyer-aggregate-cache.json"
APPSFLYER_CACHE_TTL_MINUTES = 30

LOCAL_TZ = ZoneInfo("America/Toronto")
UTC = ZoneInfo("UTC")
TARGET_PLACED_ORDERS = 20

_HOME = Path.home()
for _libs in (_HOME / "tumbil" / "infrastructure" / "libs", _HOME / "infrastructure" / "libs"):
    if _libs.is_dir() and str(_libs) not in sys.path:
        sys.path.insert(0, str(_libs))
        break
import query_db  # noqa: E402
from ga4_attribution import classify_ga4_rows, fetch_ga4_purchase_attribution, related_ga4_rows  # noqa: E402


def et_bounds_for_window(local_start: datetime, local_end: datetime) -> tuple[str, str]:
    """Return UTC SQL timestamp strings for an ET local window."""
    start = local_start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    end = local_end.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return start, end


def customer_type(prior_completed: int) -> str:
    if prior_completed <= 0:
        return "brand_new"
    if prior_completed == 1:
        return "second_order"
    return "regular"


def customer_type_label(kind: str) -> str:
    return {
        "brand_new": "New Customers",
        "second_order": "Second-Order Customers",
        "regular": "Habitual Customers",
    }.get(kind, kind)


def fetch_placed_orders(conn, start_utc: str, end_utc: str, tz_offset: str) -> list[dict]:
    cur = conn.cursor()
    cur.execute(f"""
        SELECT
            o.id AS order_id,
            o.user_id,
            ot.timestamp AS placed_utc,
            DATE_FORMAT(CONVERT_TZ(ot.timestamp, '+00:00', %s), '%%Y-%%m-%%d %%H:%%i:%%s') AS placed_et,
            COALESCE(od.estimated_amount, 0) AS order_value_gross_cents,
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
            pc.owner_id AS referral_owner_id,
            CONCAT(COALESCE(u.firstname, ''), ' ', COALESCE(u.lastname, '')) AS referral_owner_name
        FROM payments p
        JOIN orders o ON o.id = p.order_id
        JOIN order_timelines ot ON ot.order_id = p.order_id AND ot.type = 'placed'
        LEFT JOIN order_details od ON od.order_id = o.id
        LEFT JOIN promo_code_usages pcu ON pcu.order_id = o.id
        LEFT JOIN promo_codes pc ON pc.id = pcu.promo_code_id AND pc.type = 'referral'
        LEFT JOIN users u ON u.id = pc.owner_id
        WHERE {query_db.PLACED_FILTER}
          AND ot.timestamp >= %s AND ot.timestamp < %s
        ORDER BY ot.timestamp ASC
    """, (tz_offset, start_utc, end_utc))
    rows = []
    for row in cur.fetchall():
        prior = int(row["prior_completed"] or 0)
        rows.append({
            "order_id": int(row["order_id"]),
            "user_id": int(row["user_id"]),
            "placed_utc": row["placed_utc"].strftime("%Y-%m-%d %H:%M:%S")
                if hasattr(row["placed_utc"], "strftime") else str(row["placed_utc"]),
            "placed_et": row["placed_et"],
            "order_value_cad": query_db.cents_to_net_cad(row["order_value_gross_cents"]),
            "prior_completed": prior,
            "customer_type": customer_type(prior),
            "referral_code": row.get("referral_code"),
            "referral_owner_id": row.get("referral_owner_id"),
            "referral_owner_name": (row.get("referral_owner_name") or "").strip() or None,
        })
    cur.close()
    return rows


def aggregate_orders(orders: list[dict]) -> dict:
    total_value = round(sum(o["order_value_cad"] for o in orders), 2)
    counts = Counter(o["customer_type"] for o in orders)
    total = len(orders)
    return {
        "placed_orders": total,
        "order_value_cad": total_value,
        "aov_cad": round(total_value / total, 2) if total else 0.0,
        "customer_mix": {
            "brand_new": counts.get("brand_new", 0),
            "second_order": counts.get("second_order", 0),
            "regular": counts.get("regular", 0),
            "returning_regular_total": counts.get("second_order", 0) + counts.get("regular", 0),
        },
    }


def fetch_same_time_7d(conn, now_et: datetime) -> dict:
    samples = []
    for days_back in range(1, 8):
        sample_end = now_et - timedelta(days=days_back)
        sample_start = sample_end.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc, end_utc = et_bounds_for_window(sample_start, sample_end)
        tz_offset = query_db.tz_offset_for(sample_start.strftime("%Y-%m-%d"))
        orders = fetch_placed_orders(conn, start_utc, end_utc, tz_offset)
        agg = aggregate_orders(orders)
        samples.append({
            "date": sample_start.strftime("%Y-%m-%d"),
            "placed_orders": agg["placed_orders"],
            "order_value_cad": agg["order_value_cad"],
            "new_customers": agg["customer_mix"]["brand_new"],
        })

    avg_orders = round(sum(s["placed_orders"] for s in samples) / len(samples), 1) if samples else 0.0
    avg_value = round(sum(s["order_value_cad"] for s in samples) / len(samples), 2) if samples else 0.0
    avg_new = round(sum(s["new_customers"] for s in samples) / len(samples), 1) if samples else 0.0
    return {
        "avg_placed_orders": avg_orders,
        "avg_order_value_cad": avg_value,
        "avg_new_customers": avg_new,
        "samples": samples,
    }


def fetch_referral_summary(conn, now_et: datetime) -> dict:
    cur = conn.cursor()
    windows = {
        "today": 0,
        "seven_day": 6,
        "thirty_day": 29,
    }
    summary: dict[str, dict] = {}
    end_et = now_et

    for name, days_back in windows.items():
        start_et = (now_et - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc, end_utc = et_bounds_for_window(start_et, end_et)
        cur.execute(f"""
            SELECT
                COUNT(*) AS orders,
                COUNT(DISTINCT o.user_id) AS customers,
                COALESCE(SUM(od.estimated_amount), 0) AS order_value_gross_cents
            FROM payments p
            JOIN orders o ON o.id = p.order_id
            JOIN order_timelines ot ON ot.order_id = p.order_id AND ot.type = 'placed'
            LEFT JOIN order_details od ON od.order_id = o.id
            JOIN promo_code_usages pcu ON pcu.order_id = o.id
            JOIN promo_codes pc ON pc.id = pcu.promo_code_id AND pc.type = 'referral'
            WHERE {query_db.PLACED_FILTER}
              AND ot.timestamp >= %s AND ot.timestamp < %s
        """, (start_utc, end_utc))
        row = cur.fetchone()
        summary[name] = {
            "orders": int(row["orders"] or 0),
            "customers": int(row["customers"] or 0),
            "order_value_cad": query_db.cents_to_net_cad(row["order_value_gross_cents"]),
        }

    thirty_start_et = (now_et - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_start_utc, end_utc = et_bounds_for_window(thirty_start_et, end_et)
    cur.execute(f"""
        SELECT
            pc.code,
            pc.owner_id,
            CONCAT(COALESCE(u.firstname, ''), ' ', COALESCE(u.lastname, '')) AS owner_name,
            COUNT(*) AS orders,
            COALESCE(SUM(od.estimated_amount), 0) AS order_value_gross_cents
        FROM payments p
        JOIN orders o ON o.id = p.order_id
        JOIN order_timelines ot ON ot.order_id = p.order_id AND ot.type = 'placed'
        LEFT JOIN order_details od ON od.order_id = o.id
        JOIN promo_code_usages pcu ON pcu.order_id = o.id
        JOIN promo_codes pc ON pc.id = pcu.promo_code_id AND pc.type = 'referral'
        LEFT JOIN users u ON u.id = pc.owner_id
        WHERE {query_db.PLACED_FILTER}
          AND ot.timestamp >= %s AND ot.timestamp < %s
        GROUP BY pc.id, pc.code, pc.owner_id, owner_name
        ORDER BY orders DESC, order_value_gross_cents DESC
        LIMIT 10
    """, (thirty_start_utc, end_utc))
    top_codes = [{
        "code": row["code"],
        "owner_id": row["owner_id"],
        "owner_name": (row["owner_name"] or "").strip() or None,
        "orders": int(row["orders"] or 0),
        "order_value_cad": query_db.cents_to_net_cad(row["order_value_gross_cents"]),
    } for row in cur.fetchall()]
    cur.close()

    return {**summary, "top_codes_30d": top_codes}


def fetch_deliveries_today(conn, start_utc: str, end_utc: str) -> dict:
    """Orders delivered within the ET window, with revenue net of HST.

    Uses the same "delivered" definition as the TGE daily brief
    (query-db.py "yesterday_delivered"): keyed on payments.captured_at,
    base_charge + captured, excluding cancelled orders, revenue from
    order_details.actual_amount / 1.13. Keeping the definition identical
    means the dashboard's delivery count never disagrees with the brief.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(DISTINCT p.order_id) AS deliveries,
            COALESCE(SUM(od.actual_amount), 0) AS revenue_gross_cents
        FROM payments p
        JOIN orders o ON o.id = p.order_id
        LEFT JOIN order_details od ON od.order_id = o.id
        WHERE p.transaction_type = 'base_charge'
          AND p.status = 'captured'
          AND o.status != 'cancelled'
          AND p.captured_at >= %s AND p.captured_at < %s
    """, (start_utc, end_utc))
    row = cur.fetchone()
    cur.close()
    count = int(row["deliveries"] or 0)
    revenue = query_db.cents_to_net_cad(row["revenue_gross_cents"])
    return {
        "count": count,
        "revenue_cad": revenue,
        "aov_cad": round(revenue / count, 2) if count else 0.0,
    }


LTV_COHORT_DAYS = 180
# Trailing 3-month plat_rev / net_rev_ex_HST from Tumbil Monthly Revenue sheet,
# 'Summary (Official)' tab columns Q and J. Last refreshed 2026-05-26 (Feb-Apr 2026).
# Re-derive after each month close: run finance/month_close.py, then average Q/J
# across the latest 3 closed months. The ratio has held ~22-23% since 2025-08.
LTV_CONTRIBUTION_MARGIN = 0.225


LTV_SENSITIVITY_CUTOFFS = [0, 30, 60, 90, 120, 180, 270, 365]


def _per_customer_lifetime_rows(conn) -> list[dict]:
    """One row per customer: their first placed-order UTC + lifetime totals.

    Reused by the cohort breakdown and the sensitivity sweep so we hit the
    DB once instead of once per cutoff.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT
            o.user_id,
            MIN(ot.timestamp) AS first_placed_utc,
            COUNT(DISTINCT p.order_id) AS total_orders,
            COALESCE(SUM(od.actual_amount), 0) AS rev_gross_cents
        FROM payments p
        JOIN orders o ON o.id = p.order_id
        JOIN order_timelines ot ON ot.order_id = o.id AND ot.type = 'placed'
        LEFT JOIN order_details od ON od.order_id = o.id
        WHERE p.transaction_type = 'base_charge'
          AND p.status = 'captured'
          AND o.status != 'cancelled'
        GROUP BY o.user_id
    """)
    rows = cur.fetchall()
    cur.close()
    return rows


def _aggregate(per_cust_rows: list[dict]) -> tuple[int, int, float]:
    """Return (customers, total_orders, revenue_net_cad)."""
    customers = len(per_cust_rows)
    total_orders = sum(int(r["total_orders"] or 0) for r in per_cust_rows)
    rev_gross = sum(int(r["rev_gross_cents"] or 0) for r in per_cust_rows)
    return customers, total_orders, query_db.cents_to_net_cad(rev_gross)


def fetch_lifetime_value(conn) -> dict:
    """Lifetime revenue + contribution per "mature" customer.

    Mature = first placed order is older than LTV_COHORT_DAYS (180). Customers
    inside the window are excluded so the average isn't dragged down by new
    customers who haven't had time to re-order.

    Revenue is net of HST and cancellations, from order_details.actual_amount
    on captured base_charge payments (tips excluded). Contribution applies
    LTV_CONTRIBUTION_MARGIN, the plat_rev/net_rev ratio from the monthly close
    sheet (accounts for WashPro labour, Stripe fees, refunds, and WP bonuses).

    Also returns a cohort_breakdown (per-month-since-first-order slice for the
    drill-down chart) and a sensitivity sweep across cutoff windows so the user
    can see how the headline number reacts to the cutoff choice.
    """
    rows = _per_customer_lifetime_rows(conn)
    if not rows:
        return {
            "customers": 0, "total_orders": 0, "total_revenue_cad": 0.0,
            "ltv_cad": 0.0, "orders_per_customer": 0.0,
            "contribution_per_customer_cad": 0.0,
            "contribution_margin": LTV_CONTRIBUTION_MARGIN,
            "cohort_days": LTV_COHORT_DAYS,
            "cohort_breakdown": [],
            "sensitivity": [],
        }

    now_utc = datetime.now(UTC).replace(tzinfo=None)
    for r in rows:
        first = r["first_placed_utc"]
        delta_days = (now_utc - first).days if first else 0
        r["_days_since_first"] = delta_days
        r["_months_since_first"] = delta_days // 30

    # Headline aggregate at the configured cutoff.
    mature = [r for r in rows if r["_days_since_first"] >= LTV_COHORT_DAYS]
    customers, total_orders, revenue_net = _aggregate(mature)
    ltv_cad = round(revenue_net / customers, 2) if customers else 0.0

    # Cohort breakdown: bucket by months-since-first-order.
    by_month: dict[int, list[dict]] = {}
    for r in rows:
        by_month.setdefault(r["_months_since_first"], []).append(r)
    cohort_breakdown = []
    for months_ago in sorted(by_month.keys()):
        bucket = by_month[months_ago]
        b_cust, b_orders, b_rev = _aggregate(bucket)
        if not b_cust:
            continue
        cohort_breakdown.append({
            "months_since_first_order": months_ago,
            "customers": b_cust,
            "total_orders": b_orders,
            "orders_per_customer": round(b_orders / b_cust, 2),
            "revenue_per_customer_cad": round(b_rev / b_cust, 2),
            "ltv_per_customer_cad": round(b_rev / b_cust * LTV_CONTRIBUTION_MARGIN, 2),
        })

    # Sensitivity sweep across candidate cutoffs.
    sensitivity = []
    for cutoff in LTV_SENSITIVITY_CUTOFFS:
        subset = rows if cutoff == 0 else [r for r in rows if r["_days_since_first"] >= cutoff]
        s_cust, s_orders, s_rev = _aggregate(subset)
        s_rev_pc = round(s_rev / s_cust, 2) if s_cust else 0.0
        sensitivity.append({
            "cohort_days": cutoff,
            "customers": s_cust,
            "total_orders": s_orders,
            "orders_per_customer": round(s_orders / s_cust, 2) if s_cust else 0.0,
            "revenue_per_customer_cad": s_rev_pc,
            "ltv_per_customer_cad": round(s_rev_pc * LTV_CONTRIBUTION_MARGIN, 2),
        })

    return {
        "customers": customers,
        "total_orders": total_orders,
        "total_revenue_cad": revenue_net,
        "ltv_cad": ltv_cad,
        "orders_per_customer": round(total_orders / customers, 2) if customers else 0.0,
        "contribution_per_customer_cad": round(ltv_cad * LTV_CONTRIBUTION_MARGIN, 2),
        "contribution_margin": LTV_CONTRIBUTION_MARGIN,
        "cohort_days": LTV_COHORT_DAYS,
        "cohort_breakdown": cohort_breakdown,
        "sensitivity": sensitivity,
    }


def bq_table_suffixes(now_et: datetime) -> list[str]:
    day = now_et.strftime("%Y%m%d")
    return [day, f"intraday_{day}"]


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
    credentials_file = Path.home() / ".config/gcloud/tumbil-crashlytics-sa.json"
    if not credentials_file.exists():
        return [], {"status": "unavailable", "reason": "BigQuery service account file not found"}
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_file),
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


def fetch_ga4_attribution(new_orders: list[dict], now_et: datetime) -> tuple[list[dict], dict]:
    """Return GA4 rows and a status object. Failure is non-fatal."""
    if not new_orders:
        return [], {"status": "skipped", "reason": "No brand-new orders today"}

    order_ids = sorted({str(o["order_id"]) for o in new_orders})
    user_ids = sorted({str(o["user_id"]) for o in new_orders})
    suffixes = bq_table_suffixes(now_et)
    order_sql = ", ".join(json.dumps(v) for v in order_ids)
    user_sql = ", ".join(json.dumps(v) for v in user_ids)
    suffix_sql = ", ".join(json.dumps(v) for v in suffixes)

    sql = f"""
    WITH events AS (
      SELECT
        'ga4_bigquery' AS source_system,
        event_name,
        TIMESTAMP_MICROS(event_timestamp) AS event_ts,
        platform,
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
      WHERE _TABLE_SUFFIX IN ({suffix_sql})
        AND event_name IN ('purchase', 'sign_up', 'first_open', 'session_start')
    )
    SELECT *
    FROM events
    WHERE order_id IN ({order_sql})
       OR tumbil_id IN ({user_sql})
    ORDER BY event_ts ASC
    """

    start_date = now_et.strftime("%Y-%m-%d")
    data_api_rows, data_api_status = fetch_ga4_purchase_attribution(start_date, start_date, user_ids)

    if not shutil.which("bq"):
        bq_rows, bq_status = run_bigquery_rest(sql)
        rows = data_api_rows + bq_rows
        return rows, {
            "status": "ok" if rows else "partial",
            "primary": "ga4_data_api",
            "ga4_data_api": data_api_status,
            "bigquery": bq_status,
            "rows": len(rows),
        }

    cmd = [
        "bq", "query",
        "--use_legacy_sql=false",
        "--format=json",
        "--project_id=customer-app-ab2c8",
        f"--service_account_credential_file={str(Path.home() / '.config/gcloud/tumbil-crashlytics-sa.json')}",
        sql,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
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
    except json.JSONDecodeError as exc:
        rows = data_api_rows
        return rows, {
            "status": "partial" if rows else "error",
            "primary": "ga4_data_api",
            "rows": len(rows),
            "ga4_data_api": data_api_status,
            "bigquery": {"status": "error", "reason": f"Invalid bq JSON: {exc}"},
        }


def _number_from_csv(value, default=0.0) -> float:
    if value is None:
        return default
    text = str(value).strip().replace(",", "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _int_from_csv(value) -> int:
    return int(round(_number_from_csv(value, 0.0)))


def fetch_appsflyer_app(app_id: str, token: str, date_et: str) -> tuple[list[dict], dict]:
    params = urllib.parse.urlencode({
        "from": date_et,
        "to": date_et,
        "timezone": "America/Toronto",
    })
    url = f"https://hq1.appsflyer.com/api/agg-data/export/app/{app_id}/partners_by_date_report/v5?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "authorization": f"Bearer {token}",
            "accept": "text/csv",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-500:]
        return [], {"status": "error", "app_id": app_id, "reason": detail}
    except Exception as exc:
        return [], {"status": "error", "app_id": app_id, "reason": str(exc)}

    rows = []
    for raw in csv.DictReader(io.StringIO(body)):
        purchase_events = _int_from_csv(raw.get("af_purchase (Event counter)"))
        unique_users = _int_from_csv(raw.get("af_purchase (Unique users)"))
        sales_cad = round(_number_from_csv(raw.get("af_purchase (Sales in CAD)")), 2)
        if purchase_events <= 0 and unique_users <= 0 and sales_cad <= 0:
            continue
        rows.append({
            "app_id": app_id,
            "date": raw.get("Date") or date_et,
            "media_source": raw.get("Media Source (pid)") or "Unknown",
            "campaign": raw.get("Campaign (c)") or "None",
            "purchase_events": purchase_events,
            "unique_users": unique_users,
            "sales_cad": sales_cad,
        })
    return rows, {"status": "ok", "app_id": app_id, "rows": len(rows)}


def fetch_appsflyer_aggregate(now_et: datetime) -> dict:
    date_et = now_et.strftime("%Y-%m-%d")
    cached = read_appsflyer_cache(date_et)
    if cached and is_appsflyer_cache_fresh(cached, now_et):
        cached["cache_status"] = "hit"
        return cached

    if not APPSFLYER_CREDENTIALS_FILE.exists():
        return {
            "status": "unavailable",
            "reason": f"Missing {APPSFLYER_CREDENTIALS_FILE}",
            "date": date_et,
        }
    try:
        credentials = json.loads(APPSFLYER_CREDENTIALS_FILE.read_text())
    except Exception as exc:
        return {"status": "error", "reason": f"Invalid credentials file: {exc}", "date": date_et}

    token = credentials.get("api_token")
    app_ids = [
        ("ios", credentials.get("ios_app_id")),
        ("android", credentials.get("android_app_id")),
    ]
    if not token:
        return {"status": "error", "reason": "AppsFlyer api_token missing", "date": date_et}

    all_rows = []
    app_status = []
    for platform, app_id in app_ids:
        if not app_id:
            continue
        rows, status = fetch_appsflyer_app(app_id, token, date_et)
        status["platform"] = platform
        app_status.append(status)
        all_rows.extend(rows)

    if not all_rows and cached and all(s.get("status") != "ok" for s in app_status):
        cached["status"] = "stale"
        cached["cache_status"] = "stale_fallback"
        cached["warning"] = "; ".join(
            f"{s.get('platform')}: {s.get('reason')}"
            for s in app_status
            if s.get("reason")
        ) or "AppsFlyer API failed; using cached aggregate"
        return cached

    grouped: dict[tuple[str, str], dict] = {}
    totals = {"purchase_events": 0, "unique_users": 0, "sales_cad": 0.0}
    for row in all_rows:
        key = (row["media_source"], row["campaign"])
        bucket = grouped.setdefault(key, {
            "media_source": row["media_source"],
            "campaign": row["campaign"],
            "purchase_events": 0,
            "unique_users": 0,
            "sales_cad": 0.0,
            "apps": [],
        })
        bucket["purchase_events"] += row["purchase_events"]
        bucket["unique_users"] += row["unique_users"]
        bucket["sales_cad"] = round(bucket["sales_cad"] + row["sales_cad"], 2)
        bucket["apps"] = sorted(set(bucket["apps"]) | {row["app_id"]})
        totals["purchase_events"] += row["purchase_events"]
        totals["unique_users"] += row["unique_users"]
        totals["sales_cad"] = round(totals["sales_cad"] + row["sales_cad"], 2)

    media_sources = sorted(
        grouped.values(),
        key=lambda r: (-r["purchase_events"], r["media_source"], r["campaign"]),
    )
    google_events = sum(
        row["purchase_events"]
        for row in media_sources
        if "google" in row["media_source"].lower()
    )
    statuses = {s.get("status") for s in app_status}
    status = "ok" if statuses == {"ok"} else "partial" if "ok" in statuses else "error"
    result = {
        "status": status,
        "date": date_et,
        "timezone": "America/Toronto",
        "fetched_at": now_et.isoformat(),
        "cache_status": "miss",
        "apps": app_status,
        "totals": totals,
        "google_ads_purchase_events": google_events,
        "media_sources": media_sources,
        "note": "AppsFlyer aggregate app purchase events by media source. These are not joined to DB orders or customers.",
    }
    if status in {"ok", "partial"} and all_rows:
        write_appsflyer_cache(result)
    return result


def read_appsflyer_cache(date_et: str) -> dict | None:
    try:
        cached = json.loads(APPSFLYER_CACHE_FILE.read_text())
    except Exception:
        return None
    if cached.get("date") != date_et:
        return None
    return cached


def is_appsflyer_cache_fresh(cached: dict, now_et: datetime) -> bool:
    fetched_at = cached.get("fetched_at")
    if not fetched_at:
        return False
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=LOCAL_TZ)
    age_minutes = (now_et - fetched.astimezone(LOCAL_TZ)).total_seconds() / 60
    return 0 <= age_minutes < APPSFLYER_CACHE_TTL_MINUTES


def write_appsflyer_cache(result: dict) -> None:
    try:
        APPSFLYER_CACHE_FILE.write_text(json.dumps(result, indent=2))
    except Exception as exc:
        sys.stderr.write(f"  AppsFlyer cache write failed: {exc}\n")


def classify_channel(order: dict, ga4_rows: list[dict]) -> dict:
    return classify_ga4_rows(related_ga4_rows(ga4_rows, order["order_id"], order["user_id"]))


def build_acquisition(
    new_orders: list[dict],
    ga4_rows: list[dict],
    ga4_status: dict,
    appsflyer: dict,
) -> dict:
    classified = []
    counts: Counter[str] = Counter()
    confidence: dict[str, Counter] = {}
    for order in new_orders:
        item = classify_channel(order, ga4_rows)
        classified.append({
            "order_id": order["order_id"],
            "bucket": item["bucket"],
            "confidence": item["confidence"],
            "reason": item["reason"],
            "match_quality": item.get("match_quality"),
            "campaign": item.get("campaign"),
            "referral_code": order.get("referral_code"),
            "referral_owner_name": order.get("referral_owner_name"),
        })
        counts[item["bucket"]] += 1
        confidence.setdefault(item["bucket"], Counter())[item["confidence"]] += 1

    sources = []
    for bucket, orders in counts.most_common():
        conf_counts = confidence[bucket]
        conf = "high" if conf_counts["high"] else "medium" if conf_counts["medium"] else "low"
        sources.append({"bucket": bucket, "orders": orders, "confidence": conf})

    return {
        "new_customer_orders": len(new_orders),
        "sources": sources,
        "classified_orders": classified,
        "unknown_orders": counts.get("Direct / Unknown", 0),
        "ga4_status": ga4_status,
        "appsflyer": appsflyer,
        "caveat": "Order counts and customer status come from DB. Source labels are GA4 attribution-only, with GA4 Data API purchase/tumbil_id matches preferred over BigQuery raw-event fallback. AppsFlyer is included as aggregate app purchase attribution and is not joined to individual customers. Referral promo codes are tracked separately.",
    }


def priority_snapshot_status() -> dict:
    if not PRIORITIES_FILE.exists():
        return {"status": "missing", "path": str(PRIORITIES_FILE)}
    try:
        data = json.loads(PRIORITIES_FILE.read_text())
    except json.JSONDecodeError as exc:
        return {"status": "error", "reason": str(exc)}
    return {
        "status": "ok",
        "updated_at": data.get("updated_at"),
        "item_count": len(data.get("items", [])),
    }


def _fetch_db_sections(conn_factory, now_et, start_utc, now_utc, tz_offset):
    """Run every MySQL query up front, with one reconnect-and-retry on a
    dropped connection.

    Keeping all DB work in this single early phase means the connection is
    never held idle across the slow BigQuery attribution call that follows -
    holding it idle across that ~100s call is what got it reaped mid-query
    (pymysql 2013 'Lost connection during query', 2026-05-14). The retry also
    covers a genuine transient drop anywhere in this phase.
    """
    last_err = None
    for attempt in (1, 2):
        conn = conn_factory()
        try:
            today_orders = fetch_placed_orders(conn, start_utc, now_utc, tz_offset)
            same_time = fetch_same_time_7d(conn, now_et)
            referrals = fetch_referral_summary(conn, now_et)
            deliveries = fetch_deliveries_today(conn, start_utc, now_utc)
            ltv = fetch_lifetime_value(conn)
            return today_orders, same_time, referrals, deliveries, ltv
        except query_db.pymysql.err.OperationalError as exc:
            last_err = exc
            sys.stderr.write(
                f"  DB connection lost on attempt {attempt}/2 ({exc}); reconnecting...\n"
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    raise last_err


def build_live_payload(conn_factory, now_et: datetime | None = None) -> dict:
    now_et = now_et or datetime.now(LOCAL_TZ)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc, now_utc = et_bounds_for_window(start_et, now_et)
    tz_offset = query_db.tz_offset_for(start_et.strftime("%Y-%m-%d"))

    # --- All MySQL queries first, while the connection is fresh. ---
    today_orders, same_time, referrals, deliveries, ltv = _fetch_db_sections(
        conn_factory, now_et, start_utc, now_utc, tz_offset
    )
    today = aggregate_orders(today_orders)

    # --- Then the slow external call. BigQuery can take ~100s, but no MySQL
    #     connection is held open across it anymore. ---
    new_orders = [o for o in today_orders if o["customer_type"] == "brand_new"]
    ga4_rows, ga4_status = fetch_ga4_attribution(new_orders, now_et)
    appsflyer = fetch_appsflyer_aggregate(now_et)

    today["business_date"] = start_et.strftime("%Y-%m-%d")
    today["as_of_et"] = now_et.isoformat()
    today["target_placed_orders"] = TARGET_PLACED_ORDERS
    today["pace_to_target_pct"] = round(today["placed_orders"] / TARGET_PLACED_ORDERS * 100, 1)
    today["same_time_7d_avg_orders"] = same_time["avg_placed_orders"]
    today["same_time_delta_orders"] = round(today["placed_orders"] - same_time["avg_placed_orders"], 1)
    today["same_time_7d_avg_order_value_cad"] = same_time["avg_order_value_cad"]
    today["same_time_7d_avg_new_customers"] = same_time["avg_new_customers"]
    today["deliveries"] = deliveries

    return {
        "version": 1,
        "generated_at": datetime.now(LOCAL_TZ).isoformat(),
        "timezone": "America/Toronto",
        "today": today,
        "customer_type_labels": {
            "brand_new": customer_type_label("brand_new"),
            "second_order": customer_type_label("second_order"),
            "regular": customer_type_label("regular"),
            "returning_regular_total": "Second-Order + Habitual Customers",
        },
        "same_time_7d_samples": same_time["samples"],
        "acquisition": build_acquisition(new_orders, ga4_rows, ga4_status, appsflyer),
        "referrals": referrals,
        "lifetime_value": ltv,
        "data_health": {
            "db": {"status": "ok", "as_of_et": now_et.isoformat()},
            "ga4_attribution": ga4_status,
            "ga4_bigquery": ga4_status.get("bigquery", ga4_status),
            "appsflyer": {
                "status": appsflyer.get("status"),
                "date": appsflyer.get("date"),
                "apps": appsflyer.get("apps", []),
            },
            "priorities": priority_snapshot_status(),
        },
    }


def default_priorities_payload() -> dict:
    now = datetime.now(LOCAL_TZ).isoformat()
    return {
        "version": 1,
        "updated_at": now,
        "items": [],
        "columns": [
            "BACKBURNER / ONGOING",
            "NEW FOR DISCUSSION",
            "BACKLOG",
            "IN PROGRESS",
            "DONE FOR ENG/DISCUSS",
            "DONE & ARCHIVED",
        ],
        "areas": ["Product", "Finance", "Eng", "AI Infra"],
    }


def ensure_priorities_file() -> None:
    if not PRIORITIES_FILE.exists():
        PRIORITIES_FILE.write_text(json.dumps(default_priorities_payload(), indent=2))


def main() -> None:
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    ensure_priorities_file()
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
        payload = build_live_payload(conn_factory)

    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Live dashboard data written to {OUTPUT_FILE}")
    print(f"  Today placed: {payload['today']['placed_orders']}")
    print(f"  New customers: {payload['today']['customer_mix']['brand_new']}")
    print(f"  Deliveries: {payload['today']['deliveries']['count']} "
          f"(${payload['today']['deliveries']['revenue_cad']} ex. HST)")
    print(f"  GA4 attribution: {payload['data_health']['ga4_attribution']['status']}")
    print(f"  AppsFlyer aggregate: {payload['data_health']['appsflyer']['status']}")


if __name__ == "__main__":
    main()
