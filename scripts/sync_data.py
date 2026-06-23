#!/usr/bin/env python3
"""Build the daily TumbilOS dashboard payload from TGE reports.

This sync intentionally excludes the old recommendation/candidate surface.
TGE is now an analyst brief, and TumbilOS renders that brief alongside live
company metrics from live.json.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TGE_REPORTS = Path.home() / "tumbil" / "tge" / "reports"
DASHBOARD_DIR = Path.home() / "tumbil" / "tumbil-os" / "dashboard"
OUTPUT_FILE = DASHBOARD_DIR / "data.json"
CUSTOMERS_FILE = DASHBOARD_DIR / "customers.json"
SERVICE_DETAILS_FILE = DASHBOARD_DIR / "service-details.json"
LOCAL_TZ = ZoneInfo("America/Toronto")


def load_json(path: Path) -> dict | None:
    """Load a JSON file, returning None if it is missing or invalid."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def parse_analysis(raw: dict | None) -> dict | None:
    """Extract structured output from the Claude CLI wrapper if present."""
    if raw is None:
        return None
    return raw.get("structured_output", raw)


def latest_report(max_days_back: int = 90) -> tuple[str, dict, dict | None]:
    """Return (briefing_date, analysis, raw_data) for the newest valid report."""
    today = datetime.now(LOCAL_TZ)
    for days_back in range(max_days_back):
        date_str = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
        analysis = parse_analysis(load_json(TGE_REPORTS / f"{date_str}-analysis.json"))
        if not analysis:
            continue
        data = load_json(TGE_REPORTS / f"{date_str}-data.json")
        return date_str, analysis, data

    print(f"ERROR: No TGE analysis found in last {max_days_back} days", file=sys.stderr)
    sys.exit(1)


def guard_against_stale_history(briefing_date: str, now: datetime | None = None) -> None:
    """Do not let an old local reports folder overwrite the live dashboard."""
    now = now or datetime.now(LOCAL_TZ)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if briefing_date < today:
        if briefing_date < yesterday:
            print(
                f"ERROR: Latest local TGE briefing is {briefing_date}, but today is {today} ET. "
                "Refusing to write stale dashboard history.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"WARN: Latest local TGE briefing is {briefing_date}, but today is {today} ET. "
            "Rebuilding history from the latest briefing plus DB-derived detail payloads.",
            file=sys.stderr,
        )

    existing = load_json(OUTPUT_FILE)
    existing_briefing = str((existing or {}).get("latest_briefing_date") or "")
    if existing_briefing and existing_briefing > briefing_date:
        print(
            f"ERROR: Existing dashboard history is newer ({existing_briefing}) than local report "
            f"({briefing_date}). Refusing to downgrade dashboard/data.json.",
            file=sys.stderr,
        )
        sys.exit(1)


def build_daily_trends(anchor_date: datetime | None = None) -> list[dict]:
    """Build a 30-day placed/delivered trend from archived data files."""
    anchor_date = anchor_date or datetime.now()
    daily_orders = []
    for days_back in range(30):
        date_str = (anchor_date - timedelta(days=days_back + 1)).strftime("%Y-%m-%d")
        data = load_json(TGE_REPORTS / f"{date_str}-data.json")
        db = (data or {}).get("db", {})
        placed = db.get("yesterday_placed", {})
        delivered = db.get("yesterday_delivered", {})
        if not placed and not delivered:
            continue
        daily_orders.append({
            "date": placed.get("date", date_str),
            "placed": placed.get("orders", 0),
            "delivered": delivered.get("orders", 0),
            "order_value_cad": placed.get("order_value_cad", 0),
            "revenue_cad": delivered.get("revenue_cad", 0),
            "aov_placed_cad": placed.get("aov_cad", 0),
        })
    return list(reversed(daily_orders))


def historical_day_from_report(briefing_date: str, analysis: dict, raw_data: dict | None) -> dict | None:
    raw_data = raw_data or {}
    db = raw_data.get("db", {}) if isinstance(raw_data.get("db"), dict) else {}
    placed = db.get("yesterday_placed", {}) if isinstance(db.get("yesterday_placed"), dict) else {}
    customer_type = db.get("yesterday_customer_type", {}) if isinstance(db.get("yesterday_customer_type"), dict) else {}
    if not placed:
        return None

    business_date = placed.get("date")
    if not business_date:
        business_date = (datetime.strptime(briefing_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    computed = raw_data.get("computed", {}) if isinstance(raw_data.get("computed"), dict) else {}
    google_ads = raw_data.get("google_ads", {}) if isinstance(raw_data.get("google_ads"), dict) else {}
    seven_day = db.get("seven_day_trend", {}) if isinstance(db.get("seven_day_trend"), dict) else {}
    seven_day_totals = seven_day.get("totals", {}) if isinstance(seven_day.get("totals"), dict) else {}

    new_orders = customer_type.get("new_customer_orders", customer_type.get("new", 0)) or 0
    second_orders = customer_type.get("second_order_orders", customer_type.get("second_order", 0)) or 0
    habitual_orders = customer_type.get("habitual_orders", customer_type.get("habitual", 0)) or 0

    return {
        "date": business_date,
        "briefing_date": briefing_date,
        "placed_orders": placed.get("orders", 0) or 0,
        "order_value_cad": placed.get("order_value_cad", 0) or 0,
        "aov_cad": placed.get("aov_cad", 0) or 0,
        "customer_mix": {
            "brand_new": new_orders,
            "second_order": second_orders,
            "regular": habitual_orders,
            "returning_regular_total": second_orders + habitual_orders,
        },
        "same_time_7d_avg_orders": seven_day_totals.get("avg_orders_per_day", computed.get("orders_placed_7d_avg", 0)) or 0,
        "same_time_delta_orders": round((placed.get("orders", 0) or 0) - (seven_day_totals.get("avg_orders_per_day", computed.get("orders_placed_7d_avg", 0)) or 0), 1),
        "target_placed_orders": 20,
        "pace_to_target_pct": round((placed.get("orders", 0) or 0) / 20 * 100, 1),
        "acquisition": {
            "new_customer_orders": new_orders,
            "ad_spend_cad": google_ads.get("yesterday_spend_cad", computed.get("yesterday_ad_spend_cad")),
            "ad_clicks": google_ads.get("yesterday_clicks"),
            "ad_impressions": google_ads.get("yesterday_impressions"),
            "ad_conversions_registrations": google_ads.get("yesterday_conversions_registrations"),
            "cost_per_new_customer_cad": computed.get("cpa_per_new_customer"),
            "cost_per_new_customer_7d_avg_cad": computed.get("cpa_per_new_customer_7d_avg"),
            "source_note": "Historical source mix is limited to TGE daily acquisition metrics. Live same-day source labels use best-effort GA4/BigQuery joins.",
        },
        "executive_summary": analysis.get("executive_summary", ""),
        "analyst_brief": analysis.get("analyst_brief", ""),
        "scorecard": sanitize_scorecard(analysis.get("scorecard", [])),
    }


def historical_day_from_detail_payload(customer_day: dict, service_day: dict | None = None) -> dict | None:
    """Build a history row from customer/service detail payloads.

    After a host rebuild, old TGE analysis reports may be absent while the
    DB-derived customer and service detail payloads still cover the rolling
    dashboard window. This keeps date navigation useful without inventing an
    analyst brief for those dates.
    """
    if not isinstance(customer_day, dict):
        return None
    date = customer_day.get("date")
    if not date:
        return None

    customers_by_type = customer_day.get("customers_by_type", {})
    if not isinstance(customers_by_type, dict):
        customers_by_type = {}
    counts = customer_day.get("counts", {})
    if not isinstance(counts, dict):
        counts = {}

    rows = []
    for bucket in ("brand_new", "second_order", "regular"):
        bucket_rows = customers_by_type.get(bucket, [])
        if isinstance(bucket_rows, list):
            rows.extend(bucket_rows)

    placed_orders = len(rows) or sum(int(counts.get(bucket, 0) or 0) for bucket in ("brand_new", "second_order", "regular"))
    order_value = round(sum(float(row.get("order_value_cad") or 0) for row in rows), 2)
    aov = round(order_value / placed_orders, 2) if placed_orders else 0

    service_day = service_day if isinstance(service_day, dict) else {}
    tips = service_day.get("tips", {}) if isinstance(service_day.get("tips"), dict) else {}
    ratings = service_day.get("ratings", {}) if isinstance(service_day.get("ratings"), dict) else {}
    delivered_count = max(
        len(tips.get("rows", []) if isinstance(tips.get("rows"), list) else []),
        len(ratings.get("rows", []) if isinstance(ratings.get("rows"), list) else []),
    )

    return {
        "date": date,
        "briefing_date": None,
        "placed_orders": placed_orders,
        "order_value_cad": order_value,
        "aov_cad": aov,
        "customer_mix": {
            "brand_new": int(counts.get("brand_new", 0) or 0),
            "second_order": int(counts.get("second_order", 0) or 0),
            "regular": int(counts.get("regular", 0) or 0),
            "returning_regular_total": int(counts.get("second_order", 0) or 0) + int(counts.get("regular", 0) or 0),
        },
        "same_time_7d_avg_orders": 0,
        "same_time_delta_orders": 0,
        "target_placed_orders": 20,
        "pace_to_target_pct": round(placed_orders / 20 * 100, 1),
        "deliveries": {
            "count": delivered_count,
            "revenue_cad": 0,
            "aov_cad": 0,
        },
        "acquisition": {
            "new_customer_orders": int(counts.get("brand_new", 0) or 0),
            "source_note": "Rebuilt from DB-derived customer detail payload after ThinkPad restore; historical ad-spend metrics require archived TGE reports.",
        },
        "executive_summary": "",
        "analyst_brief": "",
        "scorecard": [],
    }


def fallback_historical_days_from_detail_payloads() -> list[dict]:
    customers = load_json(CUSTOMERS_FILE) or {}
    services = load_json(SERVICE_DETAILS_FILE) or {}
    customer_days = customers.get("days", []) if isinstance(customers.get("days"), list) else []
    service_days = {
        day.get("date"): day
        for day in (services.get("days", []) if isinstance(services.get("days"), list) else [])
        if isinstance(day, dict) and day.get("date")
    }

    days = []
    for customer_day in customer_days:
        day = historical_day_from_detail_payload(customer_day, service_days.get(customer_day.get("date")))
        if day:
            days.append(day)
    return sorted(days, key=lambda row: row["date"])


def build_historical_days(anchor_date: datetime | None = None, max_days: int = 44) -> list[dict]:
    anchor_date = anchor_date or datetime.now()
    detail_days = fallback_historical_days_from_detail_payloads()
    detail_dates = {row["date"] for row in detail_days}
    days_by_date = {
        row["date"]: row
        for row in detail_days
    }
    for days_back in range(max_days):
        briefing_date = (anchor_date - timedelta(days=days_back)).strftime("%Y-%m-%d")
        analysis = parse_analysis(load_json(TGE_REPORTS / f"{briefing_date}-analysis.json"))
        if not analysis:
            continue
        raw_data = load_json(TGE_REPORTS / f"{briefing_date}-data.json")
        day = historical_day_from_report(briefing_date, analysis, raw_data)
        if day:
            days_by_date[day["date"]] = day
    if detail_dates:
        days_by_date = {
            day: row
            for day, row in days_by_date.items()
            if day in detail_dates
        }
    return sorted(days_by_date.values(), key=lambda row: row["date"])


def source_status(raw_data: dict | None) -> dict:
    """Summarize freshness/status for dashboard data-health rendering."""
    raw_data = raw_data or {}
    statuses = {}
    for key, label in [
        ("db", "Production DB"),
        ("google_ads", "Google Ads"),
        ("search_console", "Search Console"),
    ]:
        value = raw_data.get(key)
        statuses[key] = {
            "label": label,
            "status": value.get("status", "ok") if isinstance(value, dict) else "missing",
        }
    statuses["tge_analysis"] = {"label": "TGE Analyst Brief", "status": "ok"}
    return statuses


def sanitize_scorecard(scorecard: list[dict]) -> list[dict]:
    """Normalize legacy scorecard labels for the company dashboard."""
    rows = []
    for row in scorecard or []:
        row = dict(row)
        if row.get("metric") == "Blended CPA":
            row["metric"] = "Cost per New Customer"
        elif row.get("metric") == "Returning Customers":
            row["metric"] = "Second-Order Customers"
        elif row.get("metric") == "New / Returning / Habitual":
            row["metric"] = "New / Second-Order / Habitual"
        rows.append(row)
    return rows


def build_dashboard_data() -> dict:
    briefing_date, analysis, raw_data = latest_report()
    guard_against_stale_history(briefing_date)
    briefing_dt = datetime.strptime(briefing_date, "%Y-%m-%d")
    data_date = (briefing_dt - timedelta(days=1)).strftime("%Y-%m-%d")

    return {
        "version": 2,
        "generated_at": datetime.now().isoformat(),
        "latest_briefing_date": briefing_date,
        "data_date": data_date,
        "timezone": "America/Toronto",
        "executive_summary": analysis.get("executive_summary", ""),
        "analyst_brief": analysis.get("analyst_brief", ""),
        "scorecard": sanitize_scorecard(analysis.get("scorecard", [])),
        "validation_notes": analysis.get("validation_notes", ""),
        "data_footnotes": analysis.get("data_footnotes", []),
        "trends": {
            "daily_orders": build_daily_trends(briefing_dt),
        },
        "history": {
            "days": build_historical_days(briefing_dt),
        },
        "data_health": source_status(raw_data),
    }


def main() -> None:
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    data = build_dashboard_data()
    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"Dashboard data written to {OUTPUT_FILE}")
    print(f"  Latest briefing: {data['latest_briefing_date']}")
    print(f"  Scorecard items: {len(data['scorecard'])}")
    print(f"  Daily trend points: {len(data['trends']['daily_orders'])}")


if __name__ == "__main__":
    main()
