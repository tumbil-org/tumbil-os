#!/usr/bin/env python3
"""Guard: rolling drill-down payloads must cover the live date on a ZERO-activity day.

This is the invariant whose violation froze os.tumbil.com for ~8h on 2026-06-22:
right after the midnight ET rollover the new live date had no orders, so
sync_customer_details.py emitted no customers.json row for it, the dashboard data
contract failed [HIGH], and (under `set -e`) deploy_live.sh exited before
upload_to_render.sh - freezing every Render payload while local data stayed fresh.

The producer fix (commit 70f5433) makes both builders zero-fill the full rolling
window. This guard locks that in so the class can never silently come back: it drives
each builder with a fake DB connection that returns NO rows (a zero-activity day) and
asserts the live date is present and the whole window is covered. Pure offline check -
no DB, no network - so it runs in the regression gate before any deploy can publish.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_customer_details as customers  # noqa: E402
import sync_service_details as service  # noqa: E402


class _FakeCursor:
    """Cursor that accepts any query and returns no rows (zero-activity day)."""

    def execute(self, *args, **kwargs):
        return None

    def fetchall(self):
        return []

    def close(self):
        return None


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def close(self):
        return None


def _fake_factory():
    return _FakeConn()


def _check(name: str, build_payload, tz, rolling_days: int) -> list[str]:
    # An arbitrary day with no activity. tz-aware so the builders behave exactly
    # as they do in production.
    now_et = datetime(2026, 1, 15, 9, 0, tzinfo=tz)
    live = now_et.strftime("%Y-%m-%d")
    payload = build_payload(_fake_factory, now_et=now_et)
    days = payload.get("days") or []
    dates = [d.get("date") for d in days]

    failures = []
    if live not in dates:
        failures.append(
            f"{name}: live date {live} is absent from days[] on a zero-activity day "
            f"(would fail the dashboard data contract and block the deploy)"
        )
    if len(days) != rolling_days:
        failures.append(
            f"{name}: expected {rolling_days} contiguous day rows, got {len(days)}"
        )
    if dates and dates[-1] != live:
        failures.append(f"{name}: latest day {dates[-1]} != live date {live}")
    return failures


def main() -> int:
    failures = []
    failures += _check("customers.json", customers.build_payload,
                       customers.LOCAL_TZ, customers.ROLLING_DAYS)
    failures += _check("service-details.json", service.build_payload,
                       service.LOCAL_TZ, service.ROLLING_DAYS)

    if failures:
        print("TumbilOS payload coverage guard: FAIL")
        for f in failures:
            print(f"  [HIGH] {f}")
        return 1

    print("TumbilOS payload coverage guard: OK "
          "(customers.json + service-details.json cover the live date on a zero-activity day)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
