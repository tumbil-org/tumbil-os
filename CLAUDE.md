# TumbilOS - Founder/Company Dashboard

> Also read `~/tumbil/CLAUDE.md` for org-wide rules and `~/tumbil/.shared-memory/project_briefings/global.md`.

## What this is

The founder/company dashboard for Cliff and Matt. Live order pace, customer mix, new-customer source mix, referrals, the daily analyst brief from TGE, and the Product/Finance/Eng/AI Infra priorities board.

Different from `admin.tumbil.com` (that's the order operations console for ops staff).

URL: https://tumbil-org.github.io/tumbil-os/ (password gated)

## Architecture

- **Repo:** `tumbil-org/tumbil-os` (public). Source on `main`, encrypted static bundle published to `gh-pages`.
- **Hosted by:** GitHub Pages from `gh-pages` branch.
- **Source split:** TumbilOS = dashboard + sync + deploy. TGE (`~/tumbil/tge/`) = the daily analyst brief that TumbilOS consumes from `tge/reports/*-analysis.json`.

## Where everything lives

| Path | Purpose |
|------|---------|
| `dashboard/index.html` | Static dashboard (HTML + inline JS). |
| `dashboard/data.json` | Daily analyst brief payload. |
| `dashboard/live.json` | Today-to-date live metrics (refreshed every 5 min). |
| `dashboard/customers.json` | Rolling customer drill-down. |
| `dashboard/service-details.json` | Rolling tips/ratings drill-down. |
| `dashboard/priorities.json` | Priority-board snapshot (mutable via priority API). |
| `dashboard/priorities-audit.jsonl` | Append-only priority edit log. |
| `dashboard-deploy/` | Shallow clone of `gh-pages` for publishing. Gitignored. |
| `scripts/deploy.sh` | Full daily deploy. |
| `scripts/deploy_live.sh` | 5-minute live deploy. |
| `scripts/sync_data.py` | Reads latest TGE analysis -> `dashboard/data.json`. |
| `scripts/sync_live_dashboard_data.py` | DB + GA4 + AppsFlyer -> `dashboard/live.json`. |
| `scripts/sync_customer_details.py` | DB + GA4 -> `dashboard/customers.json`. |
| `scripts/sync_service_details.py` | DB -> `dashboard/service-details.json`. |
| `scripts/encrypt_dashboard_payloads.js` | staticrypt-compatible payload encryption. |
| `scripts/tumbilos_priority_api.py` | Token-authenticated priority write API (always-on systemd service). |
| `scripts/test_tumbilos.sh` | Playwright regression gate. |
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

## Secrets

All secrets read from `~/.config/tge/tge-env`:
- `TGE_DB_PASSWORD` - DB readonly
- `TUMBILOS_DASHBOARD_PASSWORD` - staticrypt password (gates the dashboard)
- `TUMBILOS_PRIORITY_TOKEN` - bearer token for the priority API

## Regression gate

`scripts/deploy.sh` runs the full Playwright suite after sync and before encryption/push. Set `TUMBILOS_SKIP_TESTS=1` only for emergency manual deploys. Every user-found dashboard regression should become a named test in `tests/tumbilos_playwright.spec.js`.

## Data rules

- DB is authoritative for order counts, order value, and customer type.
- Placed orders use `order_timelines.timestamp` where `type='placed'`, with ET day boundaries.
- Customer type labels: New = 0 prior completed orders. Second = 1. Habitual = 2+.
- GA4/BigQuery attribution is best-effort labeling only - never for order counts or revenue.
- AppsFlyer Pull API attribution is aggregate app purchase data by media source/campaign. Not joined to individual DB orders.
- Referral promo counts: `promo_code_usages` joined to `promo_codes.type='referral'`.
- Monetary values are CAD ex. HST unless explicitly labeled otherwise.
