#!/usr/bin/env python3
"""Offline regressions for TumbilOS dashboard incident classes.

These checks cover the 2026-06-23 pre-briefing deploy freeze:
live.json advanced to today's business date before the daily TGE briefing
existed, while dashboard history and detail payloads already had safe date
coverage. The deploy must keep publishing live data in that window, but still
block genuinely unsafe navigation gaps.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import check_dashboard_data_contract as contract
import sync_data
import tumbilos_selfheal


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def dashboard_payload_dir(
    root: Path,
    *,
    history_dates: list[str],
    customer_dates: list[str],
    service_dates: list[str],
    live_date: str = "2026-06-23",
    data_date: str = "2026-06-21",
    briefing_date: str = "2026-06-22",
) -> Path:
    write_json(root / "live.json", {"today": {"business_date": live_date}})
    write_json(root / "data.json", {
        "data_date": data_date,
        "latest_briefing_date": briefing_date,
        "history": {"days": [{"date": date} for date in history_dates]},
    })
    write_json(root / "customers.json", {
        "days": [{"date": date} for date in customer_dates],
    })
    write_json(root / "service-details.json", {
        "days": [{"date": date} for date in service_dates],
    })
    return root


def check_contract_prebriefing_window() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = dashboard_payload_dir(
            Path(tmp),
            history_dates=["2026-06-22"],
            customer_dates=["2026-06-22", "2026-06-23"],
            service_dates=["2026-06-22", "2026-06-23"],
        )
        result = contract.evaluate(root)
        if result["status"] != "healthy":
            failures.append(
                "stale briefing metadata with valid live/prior-date coverage must not block deploy"
            )

    with tempfile.TemporaryDirectory() as tmp:
        root = dashboard_payload_dir(
            Path(tmp),
            history_dates=["2026-06-21"],
            customer_dates=["2026-06-21", "2026-06-23"],
            service_dates=["2026-06-21", "2026-06-23"],
        )
        result = contract.evaluate(root)
        components = {item["component"] for item in result["findings"]}
        if result["status"] != "degraded" or "data.json history.days" not in components:
            failures.append("missing prior-day history must still block deploy")

    with tempfile.TemporaryDirectory() as tmp:
        root = dashboard_payload_dir(
            Path(tmp),
            history_dates=["2026-06-22"],
            customer_dates=["2026-06-22", "2026-06-23"],
            service_dates=["2026-06-22", "2026-06-23"],
            data_date="2026-06-24",
            briefing_date="2026-06-24",
        )
        result = contract.evaluate(root)
        components = {item["component"] for item in result["findings"]}
        if not {"data.json data_date", "data.json latest_briefing_date"} <= components:
            failures.append("future data/briefing metadata must still block deploy")

    return failures


def check_sync_data_stale_briefing_guard() -> list[str]:
    failures: list[str] = []
    original_output = sync_data.OUTPUT_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            sync_data.OUTPUT_FILE = Path(tmp) / "data.json"
            sync_data.OUTPUT_FILE.write_text('{"latest_briefing_date":"2026-06-22"}')
            now = datetime(2026, 6, 23, 5, 30, tzinfo=ZoneInfo("America/Toronto"))
            try:
                sync_data.guard_against_stale_history("2026-06-22", now)
            except SystemExit:
                failures.append("one-day stale briefing must be allowed before today's brief exists")

            try:
                sync_data.guard_against_stale_history("2026-06-21", now)
            except SystemExit as exc:
                if exc.code != 1:
                    failures.append("two-day stale briefing should exit with code 1")
            else:
                failures.append("two-day stale briefing must still block deploy")
    finally:
        sync_data.OUTPUT_FILE = original_output
    return failures


def check_history_trimmed_to_detail_window() -> list[str]:
    failures: list[str] = []
    original_fallback = sync_data.fallback_historical_days_from_detail_payloads
    original_load = sync_data.load_json
    original_parse = sync_data.parse_analysis
    try:
        sync_data.fallback_historical_days_from_detail_payloads = lambda: [
            {"date": "2026-05-10", "placed_orders": 0},
            {"date": "2026-06-22", "placed_orders": 3},
            {"date": "2026-06-23", "placed_orders": 1},
        ]

        def fake_load(path: Path) -> dict | None:
            if path.name == "2026-06-22-analysis.json":
                return {"executive_summary": "x", "analyst_brief": "", "scorecard": []}
            if path.name == "2026-06-22-data.json":
                return {"db": {"yesterday_placed": {"date": "2026-05-09", "orders": 2}}}
            return None

        sync_data.load_json = fake_load
        sync_data.parse_analysis = lambda raw: raw
        rows = sync_data.build_historical_days(datetime(2026, 6, 22), max_days=1)
        dates = [row["date"] for row in rows]
        if dates != ["2026-05-10", "2026-06-22", "2026-06-23"]:
            failures.append(f"history dates should be trimmed to detail coverage, got {dates}")
    finally:
        sync_data.fallback_historical_days_from_detail_payloads = original_fallback
        sync_data.load_json = original_load
        sync_data.parse_analysis = original_parse
    return failures


def check_sync_data_no_tge_report_fallback() -> list[str]:
    failures: list[str] = []
    original_fallback = sync_data.fallback_historical_days_from_detail_payloads
    original_load = sync_data.load_json
    try:
        sync_data.fallback_historical_days_from_detail_payloads = lambda: [
            {
                "date": "2026-06-27",
                "placed_orders": 7,
                "order_value_cad": 322.5,
                "aov_cad": 46.07,
                "deliveries": {"count": 5, "revenue_cad": 250.0},
            },
            {
                "date": "2026-06-28",
                "placed_orders": 2,
                "order_value_cad": 99.0,
                "aov_cad": 49.5,
                "deliveries": {"count": 1, "revenue_cad": 55.0},
            },
        ]
        sync_data.load_json = lambda _path: None

        now = datetime(2026, 6, 28, 14, 0, tzinfo=ZoneInfo("America/Toronto"))
        data = sync_data.build_fallback_dashboard_data(now)
        dates = [row["date"] for row in data["history"]["days"]]
        if dates != ["2026-06-27", "2026-06-28"]:
            failures.append(f"no-report fallback should preserve detail dates, got {dates}")
        if data["data_date"] != "2026-06-27":
            failures.append(f"no-report fallback should use prior ET date, got {data['data_date']}")
        if data["data_health"]["tge_analysis"]["status"] != "missing":
            failures.append("no-report fallback must mark TGE analysis missing")
        if not data["trends"]["daily_orders"]:
            failures.append("no-report fallback should build chart trends from history")
    finally:
        sync_data.fallback_historical_days_from_detail_payloads = original_fallback
        sync_data.load_json = original_load
    return failures


def check_selfheal_failed_deploy_reports_attempt() -> list[str]:
    failures: list[str] = []
    slack_messages: list[str] = []
    foreman_diagnoses: list[str] = []

    tumbilos_selfheal.fetch_health = lambda: {
        "reachable": True,
        "stale": True,
        "age_min": 42.0,
        "files": ["live.json"],
    }
    tumbilos_selfheal.read_env = lambda: {
        key: "present" for key in tumbilos_selfheal.REQUIRED_KEYS
    }
    tumbilos_selfheal.inactive_units = lambda: []
    tumbilos_selfheal.run_deploy_live = lambda: (
        False,
        "deploy_live blocked before upload: [HIGH] data.json history.days: missing prior date",
    )
    tumbilos_selfheal.time.sleep = lambda _seconds: None
    tumbilos_selfheal.escalate_to_incident_agent = lambda _diagnosis: False
    tumbilos_selfheal.should_alert = lambda _diagnosis: True
    tumbilos_selfheal.slack_notify = slack_messages.append
    tumbilos_selfheal.escalate_to_foreman = foreman_diagnoses.append

    code = tumbilos_selfheal.main()
    if code != 1:
        failures.append(f"self-heal failure path should exit 1, got {code}")
    if not slack_messages:
        failures.append("self-heal failure path should emit a Slack escalation")
    elif "Tried: ran deploy_live.sh" not in slack_messages[0]:
        failures.append("self-heal Slack escalation must report deploy_live.sh was attempted")
    if not foreman_diagnoses:
        failures.append("self-heal failure path should hand off after deterministic repair fails")
    return failures


def main() -> int:
    failures = []
    failures += check_contract_prebriefing_window()
    failures += check_sync_data_stale_briefing_guard()
    failures += check_history_trimmed_to_detail_window()
    failures += check_sync_data_no_tge_report_fallback()
    failures += check_selfheal_failed_deploy_reports_attempt()

    if failures:
        print("TumbilOS incident regression guard: FAIL")
        for failure in failures:
            print(f"  [HIGH] {failure}")
        return 1

    print("TumbilOS incident regression guard: OK "
          "(pre-briefing deploy window and self-heal alert branch covered)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
