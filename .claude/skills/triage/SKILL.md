---
name: triage
description: Triage and fix the next tumbil-os (TumbilOS Company Dashboard) task from the workspace queue at ~/tumbil/dispatch/. Pulls latest, fixes dashboard/index.html, mirrors to the served copy in tumbilos-service, runs the Playwright regression gate, commits + pushes both repos (Render auto-deploys), then spawns an independent verifier that re-runs the suite and adds a regression test. Updates state Triage to Patching to QA to Verified. Posts to Slack #tumbil-ops only when a human is needed. Runs on the ThinkPad heartbeat but also works from Mac. Use when the router dispatches a TO- task or the user says /triage TO-NNN.
user_invocable: true
---

# /triage (tumbil-os scope)

Process a TumbilOS Company Dashboard task from Triage to Verified. TumbilOS is the founder dashboard at os.tumbil.com. This skill ONLY processes tasks with `repo: tumbil-os` (prefix `TO-`); the workspace `/triage-router` dispatches here. It mirrors the rapid-order `/triage` skill, adapted for TumbilOS.

**TumbilOS keeps TWO copies of the dashboard HTML and they MUST stay identical:**
- `tumbil-os/dashboard/index.html` - the working copy the Playwright regression suite tests.
- `tumbilos-service/public/index.html` - the ONLY copy Render serves at os.tumbil.com.

Every fix edits the working copy, is tested there, then mirrored (straight `cp`) to the served copy. Render auto-deploys `tumbilos-service` on push to main. Data JSON (`data.json`, `live.json`, etc.) is refreshed by sync scripts and is NOT where layout/UX bugs live.

## Mode detection
- `/triage TO-NNN` - that specific task
- `/triage` / `/triage next` - next TO-* task in Triage or Reopened
- `/triage status` - print TO-* counts
- `/triage verify TO-NNN` - skip fix steps, just run the verifier

## Environment constants
- **Tasks dir:** `~/tumbil/dispatch/tasks/` (TO-* for this skill)
- **Screenshots:** `~/tumbil/dispatch/task-screenshots/`
- **Queue:** `~/tumbil/dispatch/task-queue.md`
- **Workspace repo (task metadata):** Mac `~/tumbil/`, ThinkPad `~/tumbil-workspace/`
- **Working copy + tests:** `~/tumbil/tumbil-os/` (regression gate `scripts/test_tumbilos.sh quick`)
- **Served copy:** `~/tumbil/tumbilos-service/` -> `public/index.html`; Render auto-deploys on push
- **Live URL:** https://os.tumbil.com (cookie auth - do NOT curl-smoke it; trust the test gate)
- **Slack creds (ThinkPad):** `/home/yoygurt/.config/c0/credentials.json`; channel `#tumbil-ops`

## Environment detection
```bash
if [[ "$PWD" == /home/yoygurt/* ]]; then
  ENV=thinkpad
  WORKSPACE=/home/yoygurt/tumbil-workspace      # ~/tumbil is flat on ThinkPad; commits go through the dev-ops clone
  TUMBILOS=/home/yoygurt/tumbil/tumbil-os
  TUMBILOS_SERVICE=/home/yoygurt/tumbil/tumbilos-service
elif [[ "$PWD" == /Users/cliffpeskin/* ]]; then
  ENV=mac
  WORKSPACE=/Users/cliffpeskin/tumbil
  TUMBILOS=/Users/cliffpeskin/tumbil/tumbil-os
  TUMBILOS_SERVICE=/Users/cliffpeskin/tumbil/tumbilos-service
else
  echo "Unknown environment: $PWD" >&2; exit 1
fi
```

## Steps

### 0. Pull latest from all three repos
```bash
(cd "$WORKSPACE" && git pull --rebase origin main) || { echo "workspace pull failed"; exit 1; }
(cd "$TUMBILOS" && git pull --rebase origin main) || { echo "tumbil-os pull failed"; exit 1; }
(cd "$TUMBILOS_SERVICE" && git pull --rebase origin main) || { echo "tumbilos-service pull failed"; exit 1; }
```
If any pull fails (conflicts), STOP and report.

### 1. Select the task
If no ID, pick the next TO-* task: Triage first (oldest by `reported`), then Reopened. Skip Patching (in-flight), QA, Verified, and Awaiting human (unless invoked explicitly with an ID). Read `~/tumbil/dispatch/tasks/TO-NNN.md`. If state is `Awaiting human`, a `## Your answer (...)` section is the human-reply resume signal; treat the most recent answer as authoritative.

### 2. If a screenshot is attached, look at it FIRST - "what is the reporter pointing at?"
Read `~/tumbil/dispatch/task-screenshots/TO-NNN.png`. Answer literally what is highlighted/circled/drawn on - that, not the biggest visual diff, is the bug. Write it under a `## What the reporter is pointing at` heading. If there are markings but you cannot tell what they point at, park (below) rather than guess.

#### 2a. Wrong product -> auto-re-file (do NOT park)
If the screenshot clearly belongs to a DIFFERENT product than the TumbilOS Company Dashboard - the rapid-order chat/checkout, the marketing site, the WashPro/customer/admin app, or the backend - re-file it instead of parking. Map to the prefix (`~/tumbil/dispatch/lib/repo-registry.mjs`): RO=rapid-order, MS=marketing-site, CA=customer app, WP=washpro, AD=admin, BE=backend, IN=infra. Then: create `~/tumbil/dispatch/tasks/<P>-M.md` (next number for `<P>`) with the same title + `## Description` + `## What the reporter is pointing at`, `repo:` = target, **`department:` blank**, `state: Triage`, `related: [TO-NNN]`, plus a `## Re-filed` note; copy the screenshot to `<P>-M.png`; `rm -f` the TO-NNN task file + screenshot; `node $WORKSPACE/dispatch/lib/reconcile-queue.mjs`; commit + push the workspace. Report `re-filed TO-NNN -> <P>-M` and RETURN.

### 3. Investigate
The dashboard is one static file: `dashboard/index.html` (HTML + inline JS + inline CSS). Layout / UX / copy / card-grouping bugs live there. Read it plus `DASHBOARD.md` for structure. The sibling JSON files are sync-script output, not where UI bugs live. Fill in `## Reproduction` and `## Root cause` in the task body. If you cannot find a confident root cause, park - do NOT guess on the live founder dashboard.

### 4. Implement the fix
- All edits in `$TUMBILOS/dashboard/index.html` (the tested working copy).
- Conventions: no em or en dashes (use hyphens); Canadian spelling (colour, centre); no dry cleaning references; minimal change (do not refactor surrounding code); no new comments unless the WHY is non-obvious.
- Respect the data rules in `tumbil-os/CLAUDE.md` (DB is authoritative for counts; monetary values are CAD ex. HST; etc.). UI-only changes should not touch data semantics.

### 5. Run the regression gate (MUST pass)
```bash
cd "$TUMBILOS"
TUMBILOS_SKIP_BROWSER_INSTALL=0 bash scripts/test_tumbilos.sh quick 2>&1 | tail -25
```
All @critical Playwright tests must pass. If anything broke, fix it or rethink. Do NOT set `TUMBILOS_SKIP_TESTS`. If you cannot get green, revert your edit and park.

### 6. Mirror to the served copy
The served copy MUST equal the tested copy (they are normally byte-identical):
```bash
cp "$TUMBILOS/dashboard/index.html" "$TUMBILOS_SERVICE/public/index.html"
diff -q "$TUMBILOS/dashboard/index.html" "$TUMBILOS_SERVICE/public/index.html" && echo "mirrored OK"
```

### 7. Commit + push BOTH code repos (Render auto-deploys tumbilos-service)
```bash
(cd "$TUMBILOS" && git add -A dashboard/ && git commit -m "Fix TO-NNN: <one-line title>

<2-3 lines: root cause + what changed>

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" \
  && (git push origin main 2>/dev/null || (git pull --rebase origin main && git push origin main)))
TO_HASH=$(cd "$TUMBILOS" && git rev-parse --short HEAD)
(cd "$TUMBILOS_SERVICE" && git add -A public/ && git commit -m "TO-NNN: mirror dashboard fix to served copy ($TO_HASH)" \
  && (git push origin main 2>/dev/null || (git pull --rebase origin main && git push origin main)))
```
If either push fails after a rebase, STOP, Slack DEPLOY FAILED, leave the task in Patching. Render auto-deploys `tumbilos-service` to os.tumbil.com on push.

### 8. Move to QA
Edit `$WORKSPACE/dispatch/tasks/TO-NNN.md`: `state: QA`, `fix_commit: $TO_HASH`, fill `## Fix`. Then `node $WORKSPACE/dispatch/lib/reconcile-queue.mjs` (moves the queue row) and commit + push the workspace repo.

### 9. Spawn the independent verifier (blind subagent)
Use the Agent tool, `subagent_type: general-purpose`, blind to the fix diff. Prompt: "Independent verifier for TumbilOS task TO-NNN: <title>. Read `~/tumbil/dispatch/tasks/TO-NNN.md` + the screenshot. (1) Run `cd ~/tumbil/tumbil-os && bash scripts/test_tumbilos.sh quick` and confirm ALL @critical tests pass. (2) Write a NEW `@critical` Playwright test in `tests/tumbilos_playwright.spec.js` that exercises THIS specific symptom (tumbil-os/CLAUDE.md: every dashboard regression becomes a named test), run the suite again, confirm your new test passes. You MUST NOT read the git diff/log or dashboard/index.html's history. Report PASS only if the suite is green AND your new test passes; else FAIL with one sentence."

### 10. On verifier verdict
- **PASS:** edit TO-NNN.md `state: Verified`, `verified_by: tests/tumbilos_playwright.spec.js (<test name>)`; `node reconcile-queue.mjs`; commit + push the workspace. Commit the verifier's new test in the tumbil-os repo (`git add -A tests/ && commit && push`), then mirror is NOT needed (test files are not served). Clean pass = SILENT (no Slack).
- **FAIL:** set state back to `Patching`, Slack VERIFIER REJECTED, and either retry the fix or park. Do not mark Verified on a failing verifier.

### 11. Slack - attention-needed ONLY
Post to `#tumbil-ops` only for STUCK, VERIFIER REJECTED, or DEPLOY FAILED, via the `tumbil_slack` helper (creds `/home/yoygurt/.config/c0/credentials.json`, from Mac SSH the ThinkPad to send). Routine progress and clean passes are silent (Cliff's standing preference).

### Parking a ticket (Awaiting human)
When you cannot proceed - vague repro, ambiguous markings, conflicting requirements, or a change you are not confident is safe on the live founder dashboard - park rather than guess. Edit TO-NNN.md: `state: Awaiting human`, a `## What's stuck` section (1-3 sentences, plain English for an operator - no file paths or jargon), and a `## What I need from you` section (1-2 concrete sentences). Then `node $WORKSPACE/dispatch/lib/reconcile-queue.mjs`, commit + push the workspace, and Slack STUCK. The dashboard shows it in the AWAITING HUMAN card; a human reply re-invokes `/triage TO-NNN`.
