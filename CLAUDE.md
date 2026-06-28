# TumbilOS - Founder/Company Dashboard

> Also read `~/tumbil/CLAUDE.md` for org-wide rules and `~/tumbil/.shared-memory/project_briefings/global.md`.
> Also read `~/tumbil/.shared-memory/project_briefings/tumbil-os.md` for cross-project context.

## What this is

The founder/company dashboard for Cliff and Matt. Live order pace, customer mix, new-customer source mix, referrals, the daily analyst brief from TGE, and the Product/Finance/Eng/AI Infra priorities board.

Different from `admin.tumbil.com` (that's the order operations console for ops staff).

URL: https://os.tumbil.com (Render). The old GitHub Pages bundle was retired on 2026-05-26 once Render proved stable.

## Architecture

- **Host:** Render Web Service (Express) at `tumbil-org/tumbilos-service` repo (private). Serves dashboard HTML + acts as data API. ThinkPad POSTs fresh JSON every 5-7 min to `POST /api/upload/:filename` with bearer token. Data held in memory, served instantly. No build step on data updates. Custom domain `os.tumbil.com`. Cookie-based auth (30-day lifetime).
- **Dashboard HTML lives in `tumbilos-service/public/index.html`** - that's the only copy that gets served. `tumbil-os/dashboard/index.html` is a working copy kept for the regression suite and the sync scripts to reference; if you change one, mirror to the other.
- **Source split:** TumbilOS = dashboard + sync + deploy. TGE (`~/tumbil/tge/`) = the daily analyst brief that TumbilOS consumes from `tge/reports/*-analysis.json`.

## Where everything lives

| Path | Purpose |
|------|---------|
| `dashboard/index.html` | Static dashboard (HTML + inline JS). |
| `dashboard/data.json` | Daily analyst brief payload. |
| `dashboard/live.json` | Today-to-date live metrics (refreshed every 7 min). |
| `dashboard/customers.json` | Rolling customer drill-down. |
| `dashboard/service-details.json` | Rolling tips/ratings drill-down. |
| `dashboard/priorities.json` | Priority-board snapshot (mutable via priority API). |
| `dashboard/priorities-audit.jsonl` | Append-only priority edit log. |
| `scripts/deploy.sh` | Full daily deploy (sync + tests + Render upload + commit refreshed payloads to main). |
| `scripts/deploy_live.sh` | 5-minute live deploy (sync + Render upload). |
| `scripts/upload_to_render.sh` | POSTs JSON files to the Render `tumbilos-service`. |
| `scripts/sync_data.py` | Reads latest TGE analysis -> `dashboard/data.json`. |
| `scripts/sync_live_dashboard_data.py` | DB + GA4 + AppsFlyer -> `dashboard/live.json`. |
| `scripts/sync_customer_details.py` | DB + GA4 -> `dashboard/customers.json`. |
| `scripts/sync_service_details.py` | DB -> `dashboard/service-details.json`. |
| `scripts/tumbilos_priority_api.py` | Token-authenticated priority write API (always-on systemd service). |
| `scripts/test_tumbilos.sh` | Playwright regression gate. |
| `scripts/check_dashboard_data_contract.py` | Date-contract gate run before upload (live date needs prior history + drill-down coverage). |
| `scripts/check_payload_coverage.py` | Offline guard: drill-down builders must cover the live date on zero-activity days (wired into the regression gate; skips where DB libs absent). |
| `scripts/tumbilos_selfheal.py` | ThinkPad self-heal sensor (every 10 min): deterministic recovery of stale/no-data outages; surfaces the contract finding + throttles Slack; escalates if it can't fix. |
| `scripts/tumbilos_incident_agent.py` | Autonomous incident solver: on self-heal failure, a gated full-tool Claude session fixes + ships only if diff confined + gate-green + /health fresh, else parks a diff. `TUMBILOS_INCIDENT_AUTOPUSH=0` forces park-only. |
| `scripts/systemd/` | systemd units installed on ThinkPad. |
| `tests/tumbilos_playwright.spec.js` | Browser-level regression tests. |

## Shared dependencies

Sync scripts import from `~/tumbil/infrastructure/libs/` (the canonical home for shared Tumbil python modules):
- `query_db` - DB connection + canonical query helpers
- `ga4_attribution` - GA4 purchase attribution helpers
- `tumbil_db` package - SQL fragments and timezone helpers

## Runs on ThinkPad

All TumbilOS deploys run on the ThinkPad via systemd user units:
- `tumbilos-deploy.timer` - 6:15 AM ET daily full deploy
- `tumbilos-live-deploy.timer` - every 5 minutes live data
- `tumbilos-priority-api.service` - always-on priority API

Mac launchd plist `infrastructure/mac/LaunchAgents/com.tumbil.os-deploy.plist` exists as a fallback but is currently disabled.

Temporary incident fallback: `~/Library/LaunchAgents/com.tumbil.os-live-deploy-fallback.plist` was enabled on the Mac on 2026-06-28 after Render lost in-memory payloads while the ThinkPad was offline in Tailscale. It runs `scripts/deploy_live.sh` every 5 minutes and logs to `dashboard/mac-live-fallback.log` / `dashboard/mac-live-fallback-error.log`. Unload it only after `runtime_audit.py` verifies the ThinkPad `tumbilos-live-deploy.timer` and `tumbilos-selfheal.timer` are healthy again.

## Secrets

All secrets read from `~/.config/tge/tge-env`:
- `TGE_DB_PASSWORD` - DB readonly
- `TUMBILOS_RENDER_URL` - base URL of the Render upload API
- `TUMBILOS_RENDER_UPLOAD_TOKEN` - bearer token for `/api/upload/:filename`
- `TUMBILOS_PRIORITY_TOKEN` - bearer token for the priority API

## Regression gate

`scripts/deploy.sh` runs the full Playwright suite after sync and before pushing the refreshed payloads. Set `TUMBILOS_SKIP_TESTS=1` only for emergency manual deploys. Every user-found dashboard regression should become a named test in `tests/tumbilos_playwright.spec.js`.

## Data rules

- DB is authoritative for order counts, order value, and customer type.
- Placed orders use `order_timelines.timestamp` where `type='placed'`, with ET day boundaries.
- Customer type labels: New = 0 prior completed orders. Second = 1. Habitual = 2+.
- GA4/BigQuery attribution is best-effort labeling only - never for order counts or revenue.
- AppsFlyer Pull API attribution is aggregate app purchase data by media source/campaign. Not joined to individual DB orders.
- Referral promo counts: `promo_code_usages` joined to `promo_codes.type='referral'`.
- Monetary values are CAD ex. HST unless explicitly labeled otherwise.
