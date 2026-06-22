#!/usr/bin/env python3
"""TumbilOS self-heal sensor (Boris loop).

Runs every ~10 min on the ThinkPad. Detects the dashboard-data failure modes we have
actually seen - Render serving stale/no data, deploy units down, missing upload creds
after a host rebuild - applies deterministic fixes, and re-verifies via
os.tumbil.com/health. A human is pulled in ONLY when autonomous repair genuinely fails.

Loop shape:
  Trigger:        systemd timer, every ~10 min
  Scope/run:      one health check (Render /health + ThinkPad units + env keys)
  Agent step:     deterministic heal (restore secrets from GCP Secret Manager, install/
                  restart units, re-run the live deploy); if still broken -> Foreman
                  (project=infra) takes over to diagnose + fix
  Verification:   re-poll /health and confirm live.json is fresh again
  Stop condition: healthy, OR fixes exhausted -> hand to Foreman + ping ops Slack
  Human-parked:   ops-only fixes auto-run; nothing money/customer/outbound

Secrets are restored from GCP Secret Manager secret `tumbilos-env` (the durable,
rebuild-proof source of truth), never from a local copy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HEALTH_URL = "https://os.tumbil.com/health"
STALE_MINUTES = 20  # live deploy runs every 5 min; >20 min old = clearly broken
ENV_FILE = Path.home() / ".config" / "tge" / "tge-env"
SECRET_NAME = "tumbilos-env"
REQUIRED_KEYS = [
    "TUMBILOS_RENDER_URL",
    "TUMBILOS_RENDER_UPLOAD_TOKEN",
    "TUMBILOS_PRIORITY_TOKEN",
]
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEPLOY_LIVE = PROJECT_DIR / "scripts" / "deploy_live.sh"
SYSTEMD_SRC = PROJECT_DIR / "scripts" / "systemd"
FOREMAN = Path.home() / "tumbil" / "foreman" / "foreman.py"
FOREMAN_LOCK = Path("/tmp/tumbilos-selfheal-foreman.lock")
FOREMAN_COOLDOWN_SEC = 3600
ALERT_STATE = Path("/tmp/tumbilos-selfheal-alert.json")
ALERT_COOLDOWN_SEC = 3600  # don't re-post the same diagnosis to Slack more than hourly
SLACK_LIB = Path.home() / "tumbil" / "infrastructure" / "libs" / "tumbil-slack"
C0_SECRET = "c0-credentials"

# unit name -> the "should be active" object we check
UNITS = {
    "tumbilos-priority-api.service": "tumbilos-priority-api.service",
    "tumbilos-live-deploy.timer": "tumbilos-live-deploy.timer",
}


def log(msg: str) -> None:
    print(f"[selfheal] {msg}", flush=True)


def gcloud() -> str:
    return shutil.which("gcloud") or "/snap/bin/gcloud"


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def systemctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return run(["systemctl", "--user", *args], timeout=timeout)


# ---- detection ---------------------------------------------------------------

def fetch_health() -> dict:
    """Return {reachable, stale, age_min, files, updated_at} for Render /health."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": str(e), "stale": True, "age_min": None}
    upd = (data.get("updated_at") or {}).get("live.json")
    age_min = None
    stale = True
    if upd:
        try:
            ts = datetime.fromisoformat(upd.replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            stale = age_min > STALE_MINUTES
        except ValueError:
            pass
    return {
        "reachable": True,
        "files": data.get("files", []),
        "updated_at": upd,
        "age_min": age_min,
        "stale": stale or not data.get("files"),
    }


def read_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def missing_keys(env: dict) -> list[str]:
    return [k for k in REQUIRED_KEYS if not env.get(k)]


def inactive_units() -> list[str]:
    down = []
    for unit in UNITS:
        if systemctl("is-active", unit).stdout.strip() != "active":
            down.append(unit)
    return down


# ---- deterministic fixes -----------------------------------------------------

def restore_env_from_secret(missing: list[str]) -> list[str]:
    """Add any missing required keys from GCP Secret Manager. Returns keys restored."""
    res = run([gcloud(), "secrets", "versions", "access", "latest",
               f"--secret={SECRET_NAME}"], timeout=60)
    if res.returncode != 0:
        log(f"secret fetch FAILED: {res.stderr.strip()[:200]}")
        return []
    secret = {}
    for line in res.stdout.splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            secret[k.strip()] = v.strip()
    restored = []
    lines = []
    for k in missing:
        if secret.get(k):
            lines.append(f"{k}={secret[k]}")
            restored.append(k)
    if lines:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o077)
        try:
            with ENV_FILE.open("a") as f:
                if ENV_FILE.stat().st_size and not ENV_FILE.read_text().endswith("\n"):
                    f.write("\n")
                f.write("\n".join(lines) + "\n")
        finally:
            os.umask(old)
        log(f"restored env keys from Secret Manager: {restored}")
    return restored


def ensure_unit(unit: str) -> str:
    """Install (if missing) + enable + (re)start a unit. Returns action taken."""
    enabled = systemctl("is-enabled", unit).stdout.strip()
    actions = []
    if enabled in ("not-found", ""):
        src = SYSTEMD_SRC / unit
        if src.exists():
            dest = Path.home() / ".config" / "systemd" / "user" / unit
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            systemctl("daemon-reload")
            systemctl("enable", unit)
            actions.append("installed+enabled")
    is_timer = unit.endswith(".timer")
    systemctl("restart" if not is_timer else "start", unit)
    actions.append("started" if is_timer else "restarted")
    return f"{unit}: {', '.join(actions)}"


def extract_contract_finding(output: str) -> str | None:
    """Pull the specific failure out of deploy_live output so the escalation says
    WHAT broke (e.g. customers.json missing the live date) instead of just
    'data stale'. deploy_live already prints the contract findings; we were
    throwing them away. Returns None if no actionable finding is present."""
    highs = []
    for line in output.splitlines():
        if "[HIGH]" in line:
            cleaned = line.strip()
            if cleaned not in highs:
                highs.append(cleaned)
    if highs:
        return "deploy_live blocked before upload: " + "; ".join(highs[:3])
    if "data contract: DEGRADED" in output or "contract failed" in output:
        return "deploy_live blocked before upload: data contract DEGRADED (see journal)"
    return None


def run_deploy_live() -> tuple[bool, str | None]:
    log("running deploy_live.sh to push fresh data ...")
    res = run(["bash", str(DEPLOY_LIVE)], timeout=300)
    out = res.stdout + res.stderr
    ok = res.returncode == 0 and "render-upload" in out
    tail = out.strip().splitlines()[-3:]
    log("deploy_live tail: " + " | ".join(tail))
    finding = None if ok else extract_contract_finding(out)
    if finding:
        log(f"deploy_live actionable finding: {finding}")
    return ok, finding


# ---- escalation --------------------------------------------------------------

def slack_notify(msg: str) -> None:
    try:
        res = run([gcloud(), "secrets", "versions", "access", "latest",
                   f"--secret={C0_SECRET}"], timeout=60)
        creds = json.loads(res.stdout)
        sys.path.insert(0, str(SLACK_LIB))
        from tumbil_slack import SlackClient  # type: ignore
        SlackClient(creds["slack_bot_token"]).post_message(creds["slack_channel"], msg)
        log("posted escalation to ops Slack")
    except Exception as e:  # noqa: BLE001
        log(f"slack notify failed (non-fatal): {e}")


def escalate_to_foreman(diagnosis: str) -> None:
    if FOREMAN_LOCK.exists():
        try:
            age = time.time() - FOREMAN_LOCK.stat().st_mtime
            if age < FOREMAN_COOLDOWN_SEC:
                log(f"Foreman handoff skipped (cooldown, {int(age)}s ago)")
                return
        except OSError:
            pass
    FOREMAN_LOCK.write_text(str(time.time()))
    task = (f"TumbilOS dashboard (os.tumbil.com) is still broken after automated "
            f"self-heal. Diagnosis: {diagnosis}. Investigate and fix on the ThinkPad; "
            f"verify os.tumbil.com/health shows live.json fresh (<10 min). "
            f"See memory tumbilos-no-data-render-creds for the known runbook.")
    if not FOREMAN.exists():
        log("Foreman not found; relying on Slack escalation only")
        return
    log("handing off to Foreman (project=infra) in background")
    subprocess.Popen(
        ["python3", str(FOREMAN), "--project", "infra", task],
        stdout=open("/tmp/tumbilos-selfheal-foreman.out", "a"),
        stderr=subprocess.STDOUT, start_new_session=True,
    )


# ---- main --------------------------------------------------------------------

def main() -> int:
    health = fetch_health()
    env = read_env()
    miss = missing_keys(env)
    down = inactive_units()

    healthy = health.get("reachable") and not health.get("stale") and not miss and not down
    if healthy:
        log(f"healthy (live.json {health['age_min']:.1f} min old, "
            f"{len(health.get('files', []))} files, units up)")
        return 0

    diag = []
    if not health.get("reachable"):
        diag.append(f"Render /health unreachable ({health.get('error')})")
    elif health.get("stale"):
        diag.append(f"data stale (live.json age={health.get('age_min')})")
    if miss:
        diag.append(f"missing env keys {miss}")
    if down:
        diag.append(f"units down {down}")
    diagnosis = "; ".join(diag)
    log(f"UNHEALTHY: {diagnosis}")

    actions = []
    if miss:
        actions += [f"restored {k}" for k in restore_env_from_secret(miss)]
    for unit in down:
        actions.append(ensure_unit(unit))
    if miss or (health.get("reachable") and health.get("stale")):
        if run_deploy_live():
            actions.append("ran deploy_live.sh")

    # verify: re-check every signal until fully healthy (or give up)
    recovered = False
    for _ in range(6):
        time.sleep(10)
        h = fetch_health()
        if (h.get("reachable") and not h.get("stale")
                and not missing_keys(read_env()) and not inactive_units()):
            recovered = True
            health = h
            break

    if recovered:
        # Success is exactly when a human is NOT needed -> journal only, no Slack noise.
        log(f"SELF-HEALED. actions: {actions}; live.json now "
            f"{health['age_min']:.1f} min old")
        return 0

    log(f"STILL BROKEN after deterministic heal. actions tried: {actions}")
    slack_notify(f":rotating_light: TumbilOS dashboard still broken after self-heal.\n"
                 f"Diagnosis: {diagnosis}\nTried: {', '.join(actions) or 'n/a'}\n"
                 f"Handing to Foreman (project=infra).")
    escalate_to_foreman(diagnosis)
    return 1


if __name__ == "__main__":
    sys.exit(main())
