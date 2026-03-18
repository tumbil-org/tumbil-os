# TumbilOS - The Operating System for Tumbil

## The Vibe

We looked at Polsia doing $6M ARR running companies autonomously. We looked at TGE generating 22 days of brilliant recommendations with a 0% implementation rate. We said: enough analysis paralysis. Time to build the machine that actually does things.

TumbilOS is where strategy meets execution. TGE is the brain - it sees everything, analyzes everything, knows exactly what to do. TumbilOS is the body - it tracks what needs doing, shows what's happening, and increasingly does things on its own.

This isn't a dashboard project. This is an operating system for running Tumbil. The dashboard is just the interface.

## What TumbilOS Does

### Phase 1: See (NOW)
- Real-time ops dashboard accessible to Cliff and Matt
- Live metrics from production DB, Google Ads, GSC
- Recommendation tracker showing what TGE says to do and whether it got done
- Implementation rate as the north star metric (currently 0% - unacceptable)

### Phase 2: Track (NEXT)
- Turn TGE recommendations into actionable tasks with owners and deadlines
- Auto-create ClickUp tickets from recommendations
- Track outcomes of implemented recommendations
- Weekly velocity reports

### Phase 3: Do (FUTURE)
- Auto-execute safe recommendations (send Mailchimp emails, adjust ad budgets)
- Auto-create ClickUp tickets for engineering work
- Auto-generate content (city page expansions, blog posts)
- Cold outreach automation (B2B hotels, Airbnb hosts)
- Social media posting

## Architecture

- **Data source:** TGE daily reports (`~/tumbil/tge/reports/`)
- **Sync script:** `scripts/sync_data.py` - pulls latest TGE data into dashboard format
- **Dashboard:** Static HTML deployed to GitHub Pages
- **Auto-update:** launchd runs after TGE daily pipeline completes
- **Access:** Public GitHub Pages URL (no auth needed - metrics aren't secret)

## Data Flow

```
Production DB ─┐
Google Ads ────┤
GSC ───────────┼──> TGE Pipeline (6 AM) ──> TGE Reports
GA4 ───────────┤                                │
Intercom ──────┘                                │
                                                v
                                    TumbilOS Sync Script
                                                │
                                                v
                                    dashboard/data.json
                                                │
                                                v
                                    GitHub Pages Dashboard
                                    (Matt + Cliff access)
```

## Key Files

| File | Purpose |
|------|---------|
| `CLAUDE.md` | This file - project context and instructions |
| `scripts/sync_data.py` | Pulls TGE data into dashboard format |
| `scripts/deploy.sh` | Builds and pushes dashboard to GitHub Pages |
| `dashboard/index.html` | The ops dashboard |
| `dashboard/data.json` | Current metrics data |
| `data/` | Raw data archive |
| `tasks/` | Task definitions and status |

## Rules

1. Keep it simple. Static HTML + JSON. No frameworks, no build tools, no servers to maintain.
2. TGE is the brain, TumbilOS is the body. Don't duplicate TGE's analysis - consume it.
3. The implementation rate metric is sacred. If it stays at 0%, nothing else matters.
4. Every recommendation should have an owner and a deadline within 24 hours of being generated.
5. If something can be automated, automate it. That's the whole point.
6. Matt should be able to open one URL and know exactly where Tumbil stands.
