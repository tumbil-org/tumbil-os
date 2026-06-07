#!/usr/bin/env python3
"""Validate TumbilOS dashboard payload date contracts.

This is the cheap guard that prevents os.tumbil.com from showing a live date
whose "Back" target is absent from the historical payload.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD_DIR = PROJECT_DIR / "dashboard"
REPAIR_SCRIPTS = (
    "sync_data.py",
    "sync_customer_details.py",
    "sync_service_details.py",
)


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None, f"{path.name} is missing"
    except json.JSONDecodeError as exc:
        return None, f"{path.name} is not valid JSON: {exc}"
    except OSError as exc:
        return None, f"{path.name} could not be read: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path.name} root must be an object"
    return payload, None


def parse_date(value: Any, label: str, findings: list[dict[str, Any]]) -> date | None:
    if not isinstance(value, str) or not value:
        findings.append(finding(label, f"{label} is missing", severity="high"))
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        findings.append(finding(label, f"{label} is not YYYY-MM-DD: {value!r}", severity="high"))
        return None


def finding(component: str, evidence: str, severity: str = "high") -> dict[str, Any]:
    return {
        "issue_type": "tumbilos_dashboard_data_contract",
        "component": component,
        "severity": severity,
        "evidence": evidence,
        "interpretation": {
            "action_class": "auto_repair_then_alert",
            "auto_fixable": True,
            "recommended_command": "run the TumbilOS sync scripts, then rerun this contract check",
        },
    }


def day_dates(payload: dict[str, Any] | None) -> set[str]:
    days = payload.get("days") if isinstance(payload, dict) else None
    if not isinstance(days, list):
        return set()
    return {
        row.get("date")
        for row in days
        if isinstance(row, dict) and isinstance(row.get("date"), str)
    }


def history_dates(payload: dict[str, Any] | None) -> set[str]:
    history = payload.get("history") if isinstance(payload, dict) else None
    days = history.get("days") if isinstance(history, dict) else None
    if not isinstance(days, list):
        return set()
    return {
        row.get("date")
        for row in days
        if isinstance(row, dict) and isinstance(row.get("date"), str)
    }


def missing_dates(label: str, required: set[str], present: set[str]) -> list[dict[str, Any]]:
    missing = sorted(required - present)
    if not missing:
        return []
    sample = ", ".join(missing[:8])
    if len(missing) > 8:
        sample += f", ... ({len(missing)} total)"
    return [finding(label, f"{label} is missing date coverage for {sample}")]


def evaluate(dashboard_dir: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    payloads: dict[str, dict[str, Any] | None] = {}

    for filename in ("data.json", "live.json", "customers.json", "service-details.json"):
        payload, error = load_json(dashboard_dir / filename)
        payloads[filename] = payload
        if error:
            findings.append(finding(filename, error))

    data_payload = payloads["data.json"] or {}
    live_payload = payloads["live.json"] or {}
    customers_payload = payloads["customers.json"] or {}
    service_payload = payloads["service-details.json"] or {}

    live_today = live_payload.get("today") if isinstance(live_payload.get("today"), dict) else {}
    live_date = parse_date(live_today.get("business_date"), "live.json today.business_date", findings)
    data_date = parse_date(data_payload.get("data_date"), "data.json data_date", findings)
    briefing_date = parse_date(
        data_payload.get("latest_briefing_date"),
        "data.json latest_briefing_date",
        findings,
    )

    expected_history_date: date | None = None
    if live_date:
        expected_history_date = live_date - timedelta(days=1)

    data_history = history_dates(data_payload)
    customer_days = day_dates(customers_payload)
    service_days = day_dates(service_payload)

    if expected_history_date:
        expected = expected_history_date.isoformat()
        if expected not in data_history:
            findings.append(finding(
                "data.json history.days",
                f"live date {live_date.isoformat()} requires prior date {expected}; "
                "Back from the live Overview would skip or fail without it",
            ))
        if data_date and data_date != expected_history_date:
            findings.append(finding(
                "data.json data_date",
                f"data_date is {data_date.isoformat()}, expected {expected} for live date "
                f"{live_date.isoformat()}",
            ))
        if briefing_date and briefing_date != live_date:
            findings.append(finding(
                "data.json latest_briefing_date",
                f"latest_briefing_date is {briefing_date.isoformat()}, expected live date "
                f"{live_date.isoformat()}",
            ))

    exposed_dates = set(data_history)
    if live_date:
        exposed_dates.add(live_date.isoformat())

    findings.extend(missing_dates("customers.json days", exposed_dates, customer_days))
    findings.extend(missing_dates("service-details.json days", exposed_dates, service_days))

    status = "healthy" if not findings else "degraded"
    summary = "dashboard date contract healthy"
    if findings:
        summary = f"dashboard date contract failed with {len(findings)} finding(s)"

    return {
        "status": status,
        "summary": summary,
        "dashboard_dir": str(dashboard_dir),
        "live_date": live_date.isoformat() if live_date else None,
        "expected_history_date": expected_history_date.isoformat() if expected_history_date else None,
        "data_date": data_date.isoformat() if data_date else None,
        "latest_briefing_date": briefing_date.isoformat() if briefing_date else None,
        "history_count": len(data_history),
        "customer_day_count": len(customer_days),
        "service_day_count": len(service_days),
        "findings": findings,
    }


def run_repair(sync_python: str, project_dir: Path) -> tuple[bool, str | None]:
    for script_name in REPAIR_SCRIPTS:
        script_path = project_dir / "scripts" / script_name
        if not script_path.exists():
            return False, f"repair script missing: {script_path}"
        print(f"[TumbilOS Contract] Repair running {script_name}...")
        proc = subprocess.run([sync_python, str(script_path)], cwd=project_dir)
        if proc.returncode != 0:
            return False, f"{script_name} exited {proc.returncode}"
    return True, None


def format_report(envelope: dict[str, Any]) -> str:
    lines = [f"TumbilOS dashboard data contract: {envelope['status'].upper()}"]
    lines.append(f"Summary: {envelope['summary']}")
    lines.append(f"Live date: {envelope.get('live_date') or 'unknown'}")
    lines.append(f"Expected prior date: {envelope.get('expected_history_date') or 'unknown'}")
    lines.append(f"Data date: {envelope.get('data_date') or 'unknown'}")
    lines.append(f"Briefing date: {envelope.get('latest_briefing_date') or 'unknown'}")
    if envelope["findings"]:
        lines.append("")
        for item in envelope["findings"]:
            lines.append(f"  [{item['severity'].upper()}] {item['component']}: {item['evidence']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate TumbilOS dashboard payload date contracts")
    parser.add_argument("--dashboard-dir", type=Path, default=DEFAULT_DASHBOARD_DIR)
    parser.add_argument("--project-dir", type=Path, default=PROJECT_DIR)
    parser.add_argument("--repair", action="store_true", help="Run sync scripts once if the check fails")
    parser.add_argument("--sync-python", default=sys.executable, help="Python executable to use for repair scripts")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    envelope = evaluate(args.dashboard_dir)
    repaired = False
    repair_error = None

    if envelope["status"] != "healthy" and args.repair:
        print(format_report(envelope), file=sys.stderr)
        repaired, repair_error = run_repair(args.sync_python, args.project_dir)
        envelope = evaluate(args.dashboard_dir)

    envelope["repaired"] = repaired
    if repair_error:
        envelope["repair_error"] = repair_error
        envelope["findings"].append(finding("repair", repair_error))
        envelope["status"] = "degraded"
        envelope["summary"] = f"dashboard date contract repair failed: {repair_error}"

    if args.json:
        print(json.dumps(envelope, indent=2))
    else:
        print(format_report(envelope))

    return 0 if envelope["status"] == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
