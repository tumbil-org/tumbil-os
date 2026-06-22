#!/usr/bin/env python3
"""TumbilOS autonomous incident solver (Boris loop - general-intelligence step).

When tumbilos_selfheal.py exhausts its DETERMINISTIC fixes (creds, units, redeploy)
and the dashboard is still broken, it hands the incident here. This runs a headless,
full-tool Claude Code session that reasons from live state (journalctl, /health, the
code) the way a human operator would - the capability that actually resolves
unknown-shape failures, where the constrained Foreman pipeline stalls.

The safety model is the synthesis of guard + boris: **the LLM proposes, a deterministic
verifier disposes.** The agent is only allowed to EDIT files; it never commits, pushes,
or deploys. This wrapper then grades the result and ships ONLY if every check passes:

  Trigger:        self-heal escalation (deterministic fixes exhausted)
  Scope/run:      ONE incident = the structured diagnosis from self-heal
  Agent step:     headless `claude -p` full-tool session, edits repo files only
  Verification:   diff confined to the dashboard-pipeline allowlist
                  AND scripts/test_tumbilos.sh green (contract + coverage guard + Playwright)
                  AND deploy_live.sh restores os.tumbil.com/health to fresh
  Ship decision:  all three pass -> commit + push (Render auto-deploys); else reset + park
  Stop condition: fixed, OR no usable fix, OR MAX_ATTEMPTS -> park for a human
  Human-parked:   anything outside the allowlist, anything unverifiable, anything
                  external/money/customer. Fails safe by parking a ready-to-apply diff.

Set TUMBILOS_INCIDENT_AUTOPUSH=0 to force park-only (propose a diff, never push).
Run with --dry-run to exercise the agent + gate without any commit/push/deploy.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import tumbilos_selfheal as sh  # reuse verified /health, deploy, Slack, Foreman helpers

NVM_BIN = Path.home() / ".nvm" / "versions" / "node" / "v22.22.0" / "bin"
REPORT_PATH = Path("/tmp/tumbilos-incident-report.md")
LOCK = Path("/tmp/tumbilos-incident-agent.lock")
COOLDOWN_SEC = 1800            # don't re-fire the agent more than ~half-hourly
AGENT_TIMEOUT = 900            # max wall-clock for one headless session
MAX_ATTEMPTS = 2
GATE = SCRIPTS_DIR / "test_tumbilos.sh"

# The agent may only change files inside this dashboard-pipeline allowlist. A diff
# touching anything else is out of proven scope -> park for a human, never auto-push.
ALLOW_GLOBS = [
    "scripts/sync_*.py",
    "scripts/check_*.py",
    "scripts/deploy*.sh",
    "scripts/upload_to_render.sh",
    "scripts/tumbilos_priority_api.py",
    "dashboard/index.html",
]
# Never let the agent rewrite its own controllers.
DENY = {"scripts/tumbilos_selfheal.py", "scripts/tumbilos_incident_agent.py"}

AUTOPUSH = os.environ.get("TUMBILOS_INCIDENT_AUTOPUSH", "1") != "0"
_DRY = False  # set by main(); when True, never notify/push - verification only


def log(msg: str) -> None:
    print(f"[incident-agent] {msg}", flush=True)


def agent_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = str(NVM_BIN) + os.pathsep + env.get("PATH", "")
    return env


def git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT_DIR), *args],
                          capture_output=True, text=True, timeout=timeout)


# ---- cooldown ----------------------------------------------------------------

def cooled_down() -> bool:
    if LOCK.exists():
        try:
            if time.time() - LOCK.stat().st_mtime < COOLDOWN_SEC:
                return False
        except OSError:
            pass
    try:
        LOCK.write_text(str(time.time()))
    except OSError:
        pass
    return True


# ---- the agent step ----------------------------------------------------------

def build_prompt(diagnosis: str) -> str:
    return f"""You are an UNATTENDED incident-response engineer for TumbilOS, the founder \
dashboard served at https://os.tumbil.com. It is CURRENTLY BROKEN and automated \
deterministic self-heal (creds restore, unit restart, redeploy) did NOT fix it.

Self-heal's structured diagnosis:
  {diagnosis}

Your job: find the ROOT CAUSE and FIX IT by editing source files in this repo \
({PROJECT_DIR}). Reason from live evidence, like a human operator would.

Useful evidence (read-only):
- curl -s https://os.tumbil.com/health   (per-file updated_at; live.json should be <10 min old)
- journalctl --user -u tumbilos-live-deploy.service -n 80 --no-pager -o cat
- The pipeline: scripts/sync_*.py build dashboard/*.json; scripts/check_dashboard_data_contract.py \
and scripts/check_payload_coverage.py gate the data; scripts/deploy_live.sh syncs+uploads to Render.
- Known runbooks live in this project's agent memory: the failure modes named \
"tumbilos-no-data-render-creds" (creds) and "tumbilos-stale-contract-blocks-upload" \
(deploy blocked before upload by the data contract).

HARD RULES - violating these makes your fix get thrown away:
1. Edit ONLY files inside this repo. Do NOT touch ~/.config (secrets), systemd units, \
or anything outside {PROJECT_DIR}. If the real fix needs those, do NOT attempt it - \
explain why in your report and stop.
2. Stay within the dashboard data/deploy pipeline (scripts/sync_*, scripts/check_*, \
scripts/deploy*.sh, scripts/upload_to_render.sh, dashboard/index.html). Do NOT edit \
tumbilos_selfheal.py or tumbilos_incident_agent.py.
3. Do NOT run git commit, git push, or any deploy/upload script. Just edit the source. \
A separate verifier runs the regression gate and decides whether to ship your change.
4. Do NOT contact anyone, send anything external, or touch money/customer data.
5. Prefer the smallest correct fix. If you cannot find a confident root-cause fix, make \
NO changes and say so.

When finished, write a concise report (what was broken, what you changed and why, or why \
you could not fix it) to {REPORT_PATH}."""


def run_agent(prompt: str) -> int:
    log("launching headless claude session (full tools, edit-only mandate) ...")
    try:
        res = subprocess.run(
            ["claude", "-p", prompt, "--dangerously-skip-permissions"],
            cwd=str(PROJECT_DIR), env=agent_env(),
            capture_output=True, text=True, timeout=AGENT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log("agent timed out")
        return 124
    tail = (res.stdout + res.stderr).strip().splitlines()[-4:]
    log("agent tail: " + " | ".join(tail))
    return res.returncode


# ---- deterministic verifier --------------------------------------------------

def changed_files() -> list[str]:
    out = git("status", "--porcelain").stdout
    files = []
    for line in out.splitlines():
        if len(line) > 3:
            files.append(line[3:].strip().strip('"'))
    return files


def is_confined(files: list[str]) -> bool:
    for f in files:
        if f in DENY:
            return False
        if not any(fnmatch.fnmatch(f, g) for g in ALLOW_GLOBS):
            return False
    return bool(files)


def gate_passes() -> bool:
    log("running regression gate (test_tumbilos.sh quick) ...")
    res = subprocess.run(["bash", str(GATE), "quick"], cwd=str(PROJECT_DIR),
                         env=agent_env(), capture_output=True, text=True, timeout=600)
    ok = res.returncode == 0
    tail = (res.stdout + res.stderr).strip().splitlines()[-3:]
    log(f"gate {'PASS' if ok else 'FAIL'}: " + " | ".join(tail))
    return ok


def report_text() -> str:
    try:
        return REPORT_PATH.read_text().strip()[:1500]
    except OSError:
        return "(no report written)"


def reset_changes(files: list[str]) -> None:
    tracked = [f for f in files if git("ls-files", "--error-unmatch", f).returncode == 0]
    if tracked:
        git("checkout", "--", *tracked)
    untracked = [f for f in files if f not in tracked]
    for f in untracked:
        try:
            (PROJECT_DIR / f).unlink()
        except OSError:
            pass
    log(f"reset working tree ({len(files)} file(s))")


def commit_push(files: list[str], diagnosis: str) -> bool:
    git("add", *files)
    msg = ("Autonomous incident fix: restore TumbilOS dashboard\n\n"
           f"Diagnosis: {diagnosis}\n\n"
           "Applied by tumbilos_incident_agent.py after a full-tool agent fix passed the "
           "regression gate, the diff stayed within the dashboard-pipeline allowlist, and "
           "deploy_live.sh restored os.tumbil.com/health to fresh.\n\n"
           "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>")
    if git("commit", "-m", msg).returncode != 0:
        log("commit failed")
        return False
    if git("push", "origin", "main", timeout=120).returncode != 0:
        log("push failed (committed locally)")
        return False
    log("committed + pushed fix to origin/main")
    return True


def park(diagnosis: str, files: list[str], reason: str) -> None:
    if _DRY:
        log(f"[dry-run] would park: {reason}")
        reset_changes(files)
        return
    diff = git("diff").stdout[:2500] or "(no diff / agent made no change)"
    report = report_text()
    sh.slack_notify(
        ":robot_face::warning: TumbilOS incident agent could NOT auto-resolve - human needed.\n"
        f"Diagnosis: {diagnosis}\nReason parked: {reason}\n"
        f"Agent report: {report}\n"
        f"Proposed diff (not applied):\n```\n{diff}\n```"
    )
    sh.escalate_to_foreman(f"{diagnosis} | incident-agent parked: {reason}")
    reset_changes(files)


# ---- main --------------------------------------------------------------------

def solve(diagnosis: str, dry_run: bool) -> int:
    run_agent(build_prompt(diagnosis))
    files = changed_files()
    log(f"agent changed {len(files)} file(s): {files}")

    if not files:
        park(diagnosis, files, "agent produced no fix")
        return 1
    if not is_confined(files):
        park(diagnosis, files, "diff touched files outside the dashboard-pipeline allowlist")
        return 1
    if not gate_passes():
        park(diagnosis, files, "regression gate failed on the agent's fix")
        return 1

    if dry_run or not AUTOPUSH:
        mode = "dry-run" if dry_run else "autopush-disabled"
        log(f"{mode}: gate green + diff confined; WOULD ship.")
        park(diagnosis, files, f"{mode}: proposed diff ready for review")
        return 0

    # Final real-world grade: does the fix actually restore the live dashboard?
    sh.run_deploy_live()
    fresh = False
    for _ in range(6):
        time.sleep(10)
        if not sh.fetch_health().get("stale"):
            fresh = True
            break
    if not fresh:
        park(diagnosis, files, "deploy ran but os.tumbil.com/health did not go fresh")
        return 1

    if commit_push(files, diagnosis):
        sh.clear_alert_state()
        sh.slack_notify(":white_check_mark: TumbilOS incident auto-resolved by the incident agent. "
                        f"Diagnosis: {diagnosis}. Fix passed the gate and /health is fresh.")
        log("INCIDENT AUTO-RESOLVED")
        return 0
    park(diagnosis, files, "fix verified but commit/push failed")
    return 1


def main(argv: list[str]) -> int:
    global _DRY
    dry_run = "--dry-run" in argv
    _DRY = dry_run
    args = [a for a in argv if not a.startswith("-")]
    diagnosis = args[0] if args else "TumbilOS dashboard stale; cause unknown (manual invocation)"
    if not dry_run and not cooled_down():
        log("cooldown active; skipping")
        return 0
    log(f"incident agent start (dry_run={dry_run}, autopush={AUTOPUSH}); diagnosis: {diagnosis}")
    return solve(diagnosis, dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
