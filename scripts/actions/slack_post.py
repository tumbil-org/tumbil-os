#!/usr/bin/env python3
"""
Post messages to Slack #tumbil-ops channel.
Uses the Claude Code Slack bot token from macOS Keychain.
"""

import json
import subprocess
import sys
import urllib.request
import urllib.error

CHANNEL = "C0AHPVDNBDK"  # #tumbil-ops


def get_slack_token():
    """Retrieve Slack bot token from macOS Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password",
         "-a", "claude-code-slack",
         "-s", "claude-code-slack-bot", "-w"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to get Slack token from Keychain")
    return result.stdout.strip()


def post_message(text, thread_ts=None, blocks=None, username=None, icon_emoji=None):
    """Post a message to #tumbil-ops."""
    token = get_slack_token()

    payload = {
        "channel": CHANNEL,
        "text": text,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks
    if username:
        payload["username"] = username
    if icon_emoji:
        payload["icon_emoji"] = icon_emoji

    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                print(f"Slack API error: {result.get('error')}", file=sys.stderr)
                return None
            return result.get("ts")  # message timestamp for threading
    except urllib.error.URLError as e:
        print(f"Failed to post to Slack: {e}", file=sys.stderr)
        return None


def post_recommendation(rec_data, exec_result=None):
    """Post a formatted TGE recommendation to Slack."""
    category = rec_data.get("category", "unknown").replace("_", " ").title()
    summary = rec_data.get("summary", "No summary")
    impact = rec_data.get("impact", "Unknown")
    time_est = rec_data.get("time", "Unknown")
    steps = rec_data.get("steps", [])

    status_emoji = ":robot_face:" if exec_result else ":mega:"
    status_text = f"*Auto-executed:* {exec_result}" if exec_result else "*Awaiting action*"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"TGE Daily Recommendation - {category}"}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{status_emoji} {summary}"}
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Impact:*\n{impact}"},
                {"type": "mrkdwn", "text": f"*Time:*\n{time_est}"},
            ]
        },
    ]

    if steps:
        steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Steps:*\n{steps_text}"}
        })

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": status_text}
    })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Posted by TumbilOS | <https://tumbil-org.github.io/tumbil-os/|Dashboard>"}]
    })

    fallback = f"[TGE] {category}: {summary} | Impact: {impact} | {status_text}"

    return post_message(
        text=fallback,
        blocks=blocks,
        username="TumbilOS",
        icon_emoji=":gear:",
    )


if __name__ == "__main__":
    # Quick test
    ts = post_message(
        "TumbilOS executor online. Testing Slack connection.",
        username="TumbilOS",
        icon_emoji=":gear:",
    )
    if ts:
        print(f"Message posted (ts: {ts})")
    else:
        print("Failed to post message")
        sys.exit(1)
