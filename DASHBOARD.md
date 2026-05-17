# TumbilOS Dashboard Publishing

TGE owns the static TumbilOS dashboard published to GitHub Pages.

## Role

`admin.tumbil.com` is the order operations console. TumbilOS is the founder/company dashboard for Cliff and Matt: live order pace, customer mix, new-customer source mix, referrals, the daily analyst brief, and Product/Finance/Eng/AI Infra priorities.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/sync_data.py` | Reads latest TGE analysis and generates `dashboard/data.json` for the analyst brief, scorecard, trends, and data health. |
| `scripts/sync_live_dashboard_data.py` | Queries prod DB for today-to-date placed orders, customer mix, referrals, best-effort GA4 source attribution, and aggregate AppsFlyer app purchase attribution. GA4 Data API purchase/tumbil_id rows are preferred; BigQuery raw events are fallback only. Generates `dashboard/live.json`. |
| `scripts/ga4_attribution.py` | Shared GA4 attribution helper. Uses GA4 only for source labels and campaign names; DB remains authoritative for orders, customer status, and revenue. |
| `scripts/tumbilos_priority_api.py` | Standalone editable priorities API. Writes `dashboard/priorities.json` and `dashboard/priorities-audit.jsonl`. |
| `scripts/test_tumbilos.sh` | Playwright regression gate for dashboard routing, date navigation, priority-board behavior, and mobile overflow. |
| `scripts/deploy.sh` | Full deploy: sync data, encrypt `index.html`, copy JSON payloads, push `gh-pages`. |
| `scripts/deploy_live.sh` | 5-minute deploy for live dashboard payloads: `live.json`, `customers.json`, `service-details.json`, and `priorities.json`. |

## Files

| Path | Purpose |
|------|---------|
| `dashboard/index.html` | Static HTML/JS dashboard. |
| `dashboard/data.json` | Daily analyst brief payload. |
| `dashboard/live.json` | Today-to-date live metrics payload. |
| `dashboard/appsflyer-aggregate-cache.json` | Same-day AppsFlyer aggregate cache used by the 5-minute live deploy to avoid Pull API rate limits. |
| `dashboard/priorities.json` | Latest priority-board snapshot. |
| `dashboard/priorities-audit.jsonl` | Append-only priority edit audit log. |
| `dashboard-deploy/` | Shallow clone of `tumbil-org/tumbil-os` `gh-pages` branch. |

## Schedule

All TGE runs on ThinkPad.

- `tge.timer`: 6:00 AM ET daily analyst brief
- `tge-deploy.timer`: 6:15 AM ET full dashboard deploy
- `tumbilos-live-deploy.timer`: every 5 minutes for live metrics
- `tumbilos-priority-api.service`: always-on local priority write API

## Regression Gate

Run `npm run test:tumbilos:quick` after any dashboard UI/routing change and `npm run test:tumbilos:full` before merging or deploying. `scripts/deploy.sh` runs the full Playwright suite after data sync and before encryption/push; set `TUMBILOS_SKIP_TESTS=1` only for emergency manual deploys.

Every user-found dashboard regression should become a named Playwright test in `tests/tumbilos_playwright.spec.js` before or during the fix. Current critical coverage includes route/date navigation, customer/service detail back buttons, browser back/forward behavior, primary view rendering, and mobile priority-board overflow.

## Data Rules

- DB is authoritative for order counts, order value, and customer type.
- Placed orders use `order_timelines.timestamp` where `type='placed'`, with ET day boundaries.
- Customer type labels follow TGE: New Customers = 0 prior completed orders; Second-Order Customers = 1 prior completed order; Habitual Customers = 2+ prior completed orders.
- GA4/BigQuery attribution is only a best-effort label for where new customers came from. It must not be used for order counts or revenue.
- AppsFlyer Pull API attribution is aggregate app purchase attribution by media source/campaign. It is not joined to DB orders or customers without Data Locker/raw export access.
- AppsFlyer credentials live at `~/.config/appsflyer/credentials.json` on the machine running `tumbilos-live-deploy.timer`.
- Referral promo counts come from `promo_code_usages` joined to `promo_codes.type='referral'`.
- Monetary values are CAD ex. HST unless explicitly labeled otherwise.

## Dashboard URL

https://tumbil-org.github.io/tumbil-os/ (password: muskokasummit)
