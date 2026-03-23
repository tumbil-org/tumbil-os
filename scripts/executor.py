#!/usr/bin/env python3
"""
TumbilOS Executor - Reads TGE recommendations and acts on them.

Runs daily after TGE pipeline completes. For each recommendation:
1. Determines if it's auto-executable or needs approval
2. Auto-executes safe actions (investigations, ticket creation, emails)
3. Posts results to Slack #tumbil-ops
4. Updates recommendation status

Auto-executable categories (no human approval needed):
  - retention: Mailchimp reactivation/referral emails (draft + send test first)
  - revenue_leaks: DB investigations, anomaly checks
  - conversion_funnel: ClickUp ticket creation for dev work

Needs approval (posted to Slack, C0 follows up):
  - paid_acquisition: Google Ads budget changes
  - organic_growth: Content changes to live site
  - expansion: New programs, B2B outreach

Usage:
    python3 executor.py              # normal run
    python3 executor.py --dry-run    # show what would happen, don't execute
    python3 executor.py --force      # re-execute even if already processed today
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add scripts dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from actions.slack_post import post_message, post_recommendation

TGE_REPORTS = Path.home() / "tumbil" / "tge" / "reports"
STATE_FILE = Path.home() / "tumbil" / "tumbil-os" / "data" / "executor_state.json"
LOG_FILE = Path.home() / "tumbil" / "tumbil-os" / "data" / "executor.log"

# Categories that can be auto-executed without human approval
AUTO_EXEC_CATEGORIES = {
    "revenue_leaks",       # Investigations, DB checks
    "conversion_funnel",   # ClickUp ticket creation, UX audit flagging
}

# Categories that need approval but we draft the action
DRAFT_CATEGORIES = {
    "retention",           # Mailchimp emails - draft and send test
    "expansion",           # Referral programs - draft and post
}

# Categories that need explicit approval before any action
APPROVAL_CATEGORIES = {
    "paid_acquisition",    # Google Ads changes
    "organic_growth",      # Content/SEO changes to live site
}


def log(msg):
    """Log to file and stdout."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_state():
    """Load executor state (last processed date, results)."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_processed": None, "history": []}


def save_state(state):
    """Save executor state."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_latest_analysis():
    """Find and parse the latest TGE analysis."""
    today = datetime.now()
    for days_back in range(3):
        check_date = today - timedelta(days=days_back)
        date_str = check_date.strftime("%Y-%m-%d")
        path = TGE_REPORTS / f"{date_str}-analysis.json"
        if path.exists():
            with open(path) as f:
                raw = json.load(f)
            # Extract structured output from CLI wrapper
            if "structured_output" in raw:
                return date_str, raw["structured_output"]
            return date_str, raw
    return None, None


def execute_investigation(rec, analysis, dry_run=False):
    """Handle revenue_leaks investigations - report findings to Slack."""
    summary = rec.get("recommendation_summary", rec.get("summary", ""))
    data_signal = ""

    # Find matching candidate for data signal
    candidates = analysis.get("candidates", [])
    for c in candidates:
        if c.get("category") == "revenue_leaks":
            data_signal = c.get("data_signal", "")
            break

    if dry_run:
        log(f"  [DRY RUN] Would post investigation findings to Slack")
        return "dry_run"

    msg = (
        f":mag: *Revenue Leaks Investigation*\n\n"
        f"*Finding:* {summary}\n\n"
        f"*Signal:* {data_signal}\n\n"
        f"*Status:* Auto-flagged by TumbilOS. @C0 please investigate and report back."
    )
    ts = post_message(msg, username="TumbilOS", icon_emoji=":mag:")
    return "posted_to_slack" if ts else "slack_failed"


def execute_ticket_creation(rec, analysis, dry_run=False):
    """Handle conversion_funnel items - create ClickUp tickets."""
    summary = rec.get("recommendation_summary", rec.get("summary", ""))

    if dry_run:
        log(f"  [DRY RUN] Would post ticket request to Slack")
        return "dry_run"

    msg = (
        f":ticket: *Conversion Funnel - Ticket Needed*\n\n"
        f"*Recommendation:* {summary}\n\n"
        f"*Action:* @C0 please create a ClickUp ticket in Backlog for this.\n"
        f"Tag: `feature` | Assign to: Hotshots\n\n"
        f"_Auto-flagged by TumbilOS - this rec has appeared multiple times without action._"
    )
    ts = post_message(msg, username="TumbilOS", icon_emoji=":ticket:")
    return "posted_to_slack" if ts else "slack_failed"


def draft_email_campaign(rec, analysis, dry_run=False):
    """Handle retention/expansion - draft Mailchimp campaign details."""
    summary = rec.get("recommendation_summary", rec.get("summary", ""))
    steps = analysis.get("winner_steps", [])

    if dry_run:
        log(f"  [DRY RUN] Would post email campaign draft to Slack")
        return "dry_run"

    steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(steps)) if steps else "  See TGE briefing for details."

    msg = (
        f":email: *Email Campaign Ready to Execute*\n\n"
        f"*Campaign:* {summary}\n\n"
        f"*Steps:*\n{steps_text}\n\n"
        f"*Impact:* {analysis.get('winner_expected_impact', 'See briefing')}\n\n"
        f":point_right: *Cliff/Matt:* Reply with :white_check_mark: to approve sending, "
        f"or :x: to skip.\n\n"
        f"_This recommendation has appeared {_count_category_recs(analysis, rec)} times. "
        f"TumbilOS can execute via Mailchimp once approved._"
    )
    ts = post_message(msg, username="TumbilOS", icon_emoji=":email:")
    return "awaiting_approval" if ts else "slack_failed"


def request_approval(rec, analysis, dry_run=False):
    """Handle paid_acquisition/organic_growth - post for approval."""
    category = rec.get("recommendation_category", rec.get("category", ""))
    summary = rec.get("recommendation_summary", rec.get("summary", ""))
    impact = rec.get("estimated_impact", analysis.get("winner_expected_impact", ""))
    time_est = rec.get("implementation_time", analysis.get("winner_time", ""))

    if dry_run:
        log(f"  [DRY RUN] Would post approval request to Slack")
        return "dry_run"

    emoji_map = {
        "paid_acquisition": ":chart_with_upwards_trend:",
        "organic_growth": ":seedling:",
    }
    emoji = emoji_map.get(category, ":clipboard:")

    msg = (
        f"{emoji} *{category.replace('_', ' ').title()} - Needs Approval*\n\n"
        f"*Recommendation:* {summary}\n\n"
        f"*Impact:* {impact}\n"
        f"*Time:* {time_est}\n\n"
        f":point_right: Reply :white_check_mark: to approve, :x: to skip.\n\n"
        f"_TGE has recommended this {_count_category_recs(analysis, rec)} times. "
        f"<https://tumbil-org.github.io/tumbil-os/|View Dashboard>_"
    )
    ts = post_message(msg, username="TumbilOS", icon_emoji=emoji)
    return "awaiting_approval" if ts else "slack_failed"


def _count_category_recs(analysis, rec):
    """Count how many times this category has been recommended recently."""
    reviews = analysis.get("review_and_reflect", [])
    category = rec.get("recommendation_category", rec.get("category", ""))
    count = sum(1 for r in reviews if r.get("category") == category)
    return count + 1  # +1 for today


def execute_recommendation(date_str, analysis, dry_run=False):
    """Route the winning recommendation to the appropriate handler."""
    rec_meta = analysis.get("recommendation_meta", {})
    category = rec_meta.get("recommendation_category", "")
    summary = rec_meta.get("recommendation_summary", "")

    log(f"Processing recommendation for {date_str}")
    log(f"  Category: {category}")
    log(f"  Summary: {summary[:100]}...")

    # Route to handler based on category
    if category in AUTO_EXEC_CATEGORIES:
        if category == "revenue_leaks":
            result = execute_investigation(rec_meta, analysis, dry_run)
        elif category == "conversion_funnel":
            result = execute_ticket_creation(rec_meta, analysis, dry_run)
        else:
            result = "unknown_auto_category"
    elif category in DRAFT_CATEGORIES:
        result = draft_email_campaign(rec_meta, analysis, dry_run)
    elif category in APPROVAL_CATEGORIES:
        result = request_approval(rec_meta, analysis, dry_run)
    else:
        log(f"  Unknown category: {category}")
        result = "unknown_category"

    log(f"  Result: {result}")

    # Also post the formatted recommendation card
    if not dry_run:
        rec_display = {
            "category": category,
            "summary": summary,
            "impact": rec_meta.get("estimated_impact", ""),
            "time": rec_meta.get("implementation_time", ""),
            "steps": analysis.get("winner_steps", []),
        }
        post_recommendation(rec_display, exec_result=result if result not in ("awaiting_approval",) else None)

    return result


def main():
    parser = argparse.ArgumentParser(description="TumbilOS Executor")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("--force", action="store_true", help="Re-execute even if already processed")
    args = parser.parse_args()

    log("=" * 60)
    log("TumbilOS Executor starting")

    state = load_state()
    date_str, analysis = get_latest_analysis()

    if not analysis:
        log("ERROR: No TGE analysis found in last 3 days")
        sys.exit(1)

    log(f"Latest analysis: {date_str}")

    # Check if already processed
    if state["last_processed"] == date_str and not args.force:
        log(f"Already processed {date_str}. Use --force to re-run.")
        return

    # Execute
    result = execute_recommendation(date_str, analysis, dry_run=args.dry_run)

    # Update state
    if not args.dry_run:
        state["last_processed"] = date_str
        state["history"].append({
            "date": date_str,
            "category": analysis.get("recommendation_meta", {}).get("recommendation_category", ""),
            "result": result,
            "executed_at": datetime.now().isoformat(),
        })
        # Keep last 90 days of history
        state["history"] = state["history"][-90:]
        save_state(state)

    log(f"Done. Result: {result}")


if __name__ == "__main__":
    main()
