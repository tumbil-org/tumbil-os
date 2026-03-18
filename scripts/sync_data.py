#!/usr/bin/env python3
"""
TumbilOS Data Sync - Pulls TGE reports into dashboard-ready format.

Reads from ~/tumbil/tge/reports/ and generates dashboard/data.json
with the latest metrics, scorecard, recommendations, and trends.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

TGE_REPORTS = Path.home() / "tumbil" / "tge" / "reports"
DASHBOARD_DIR = Path.home() / "tumbil" / "tumbil-os" / "dashboard"
OUTPUT_FILE = DASHBOARD_DIR / "data.json"


def load_json(path):
    """Load a JSON file, return None if missing or invalid."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def parse_analysis(raw):
    """Extract structured output from TGE analysis JSON (handles CLI wrapper)."""
    if raw is None:
        return None
    # TGE analysis files are wrapped in Claude CLI output format
    if "structured_output" in raw:
        return raw["structured_output"]
    return raw


def get_recommendations_log():
    """Parse the JSONL recommendations log."""
    log_path = TGE_REPORTS / "recommendations-log.jsonl"
    recs = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Only include entries with recommendation data
                    if "recommendation_summary" in entry:
                        recs.append(entry)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return recs


def build_dashboard_data():
    """Build the complete dashboard data payload."""
    today = datetime.now()

    # Find the latest available analysis
    latest_analysis = None
    latest_data = None
    latest_date = None

    for days_back in range(7):
        check_date = today - timedelta(days=days_back)
        date_str = check_date.strftime("%Y-%m-%d")

        analysis_path = TGE_REPORTS / f"{date_str}-analysis.json"
        data_path = TGE_REPORTS / f"{date_str}-data.json"

        if analysis_path.exists() and latest_analysis is None:
            latest_analysis = parse_analysis(load_json(analysis_path))
            latest_date = date_str

        if data_path.exists() and latest_data is None:
            latest_data = load_json(data_path)

        if latest_analysis and latest_data:
            break

    if not latest_analysis:
        print("ERROR: No TGE analysis found in last 7 days")
        sys.exit(1)

    # Get recommendation history
    all_recs = get_recommendations_log()

    # Calculate implementation stats
    total_recs = len(all_recs)
    implemented = sum(1 for r in all_recs
                      if r.get("implementation_status") == "complete"
                      or r.get("yesterday_rec_implemented") is True)
    not_implemented = sum(1 for r in all_recs
                         if r.get("implementation_status") == "not_implemented"
                         or r.get("yesterday_rec_implemented") is False)

    impl_rate = (implemented / total_recs * 100) if total_recs > 0 else 0

    # Build 7-day and 30-day order trends from available data files
    daily_orders = []
    daily_revenue = []
    for days_back in range(30):
        check_date = today - timedelta(days=days_back + 1)
        date_str = check_date.strftime("%Y-%m-%d")
        data = load_json(TGE_REPORTS / f"{date_str}-data.json")
        if data and "db" in data:
            db = data["db"]
            placed = db.get("yesterday_placed", {})
            delivered = db.get("yesterday_delivered", {})
            daily_orders.append({
                "date": placed.get("date", date_str),
                "placed": placed.get("orders", 0),
                "delivered": delivered.get("orders", 0),
                "order_value": placed.get("order_value_cad", 0),
                "revenue": delivered.get("revenue_cad", 0),
                "aov_placed": placed.get("aov_cad", 0),
            })

    daily_orders.reverse()

    # Recent recommendations (last 14 days)
    recent_recs = [r for r in all_recs if r.get("date", "") >= (today - timedelta(days=14)).strftime("%Y-%m-%d")]

    # Category distribution
    category_counts = {}
    for r in all_recs:
        cat = r.get("recommendation_category", "unknown")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    # Build the scorecard from latest analysis
    scorecard = latest_analysis.get("scorecard", []) if latest_analysis else []

    # Build candidates from latest analysis
    candidates = latest_analysis.get("candidates", []) if latest_analysis else []
    winner_index = latest_analysis.get("winner_index", 0) if latest_analysis else 0

    # Get ad spend data from latest data payload
    ad_data = {}
    if latest_data and "google_ads" in latest_data:
        gads = latest_data["google_ads"]
        ad_data = {
            "total_spend_7d": gads.get("seven_day_summary", {}).get("total_cost", 0),
            "total_conversions_7d": gads.get("seven_day_summary", {}).get("total_conversions", 0),
            "campaigns": gads.get("seven_day_summary", {}).get("campaigns", []),
        }

    # Build 30-day stats
    thirty_day = {}
    if latest_data and "db" in latest_data:
        thirty_day = latest_data["db"].get("thirty_day_aggregate", {})

    dashboard = {
        "generated_at": datetime.now().isoformat(),
        "latest_briefing_date": latest_date,
        "executive_summary": latest_analysis.get("executive_summary", "") if latest_analysis else "",

        "scorecard": scorecard,

        "implementation": {
            "total_recommendations": total_recs,
            "implemented": implemented,
            "not_implemented": not_implemented,
            "implementation_rate": round(impl_rate, 1),
            "streak_without_action": _calc_streak(all_recs),
        },

        "today_recommendation": {
            "category": latest_analysis.get("recommendation_meta", {}).get("recommendation_category", "") if latest_analysis else "",
            "summary": latest_analysis.get("recommendation_meta", {}).get("recommendation_summary", "") if latest_analysis else "",
            "impact": latest_analysis.get("winner_expected_impact", "") if latest_analysis else "",
            "time": latest_analysis.get("winner_time", "") if latest_analysis else "",
            "steps": latest_analysis.get("winner_steps", []) if latest_analysis else [],
            "rationale": latest_analysis.get("winner_rationale", "") if latest_analysis else "",
        },

        "candidates": candidates,
        "winner_index": winner_index,

        "trends": {
            "daily_orders": daily_orders,
        },

        "thirty_day": thirty_day,

        "ad_performance": ad_data,

        "recent_recommendations": [{
            "date": r.get("date"),
            "category": r.get("recommendation_category"),
            "summary": r.get("recommendation_summary"),
            "impact": r.get("estimated_impact"),
            "implemented": r.get("implementation_status", "unknown"),
            "time": r.get("implementation_time"),
        } for r in recent_recs],

        "category_distribution": category_counts,

        "notable_signals": latest_analysis.get("notable_signals", []) if latest_analysis else [],
    }

    return dashboard


def _calc_streak(recs):
    """Calculate consecutive days without an implemented recommendation."""
    streak = 0
    for r in reversed(recs):
        if r.get("implementation_status") == "complete" or r.get("yesterday_rec_implemented") is True:
            break
        if r.get("recommendation_summary"):  # Only count actual recs
            streak += 1
    return streak


def main():
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    data = build_dashboard_data()

    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Dashboard data written to {OUTPUT_FILE}")
    print(f"  Latest briefing: {data['latest_briefing_date']}")
    print(f"  Scorecard items: {len(data['scorecard'])}")
    print(f"  Implementation rate: {data['implementation']['implementation_rate']}%")
    print(f"  Days without action: {data['implementation']['streak_without_action']}")
    print(f"  Daily trend points: {len(data['trends']['daily_orders'])}")


if __name__ == "__main__":
    main()
