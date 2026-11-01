---
name: verify-example
description: Template for an app's own verify-<app> skill. Drives a work copy made by wt-new.sh
  in isolation and captures proof (screenshots, DOM snapshots, DB rows) before calling a screen
  or flow change done. Copy this folder to <app>/.claude/skills/verify-<app>/ and fill in the
  app-specific pieces marked TODO. The journeys below are placeholders for two unrelated
  businesses (appointment booking, local inventory lookup) only to show the shape of a
  feature file - replace them with the app's own.
---

# verify-example

A verify-<app> skill is what `wt-land.sh` runs automatically after a successful deploy (step
6b in docs/v3-wt-interface.md §5): launch -> doctor -> one journey, against the landed work
copy, never production. It is also what a person or agent runs by hand before calling any
screen or flow change done, and to reproduce a reported bug.

Every helper below takes the work copy path (`H=<copy>/.claude/skills/verify-<app>`). A work
copy is made by `wt-new.sh <app> <name>` (docs/v3-wt-interface.md §1): its own port, its own
test-DB slot, never the production database or a production port.

## Launch

`bash $H/launch.sh <copy>` (TODO: fill in). Should:
1. Re-point any dangling relative `node_modules` symlink inside the copy at the app folder's
   real target (`wt-new.sh` hard-link-copies `node_modules`; a `file:../relative/package` link
   can point one level too shallow inside the copy - see docs/v3-wt-interface.md §1).
2. Start the app's dev server through `wt-run.sh <copy> --bg -- <command>` (or `--dev`) with
   the fake environment from `<copy>/.claude/verify-env.json` (TODO: write this file - every
   external key the app reads, given a fake value of the same shape: a number stays a number,
   a URL becomes a dead one, a boolean becomes `false`; see docs/v3-adapters.md "사본 환경").
3. Wait for a cheap readiness signal (a health path returning 200) before returning.

## Doctor

`bash $H/doctor.sh <copy>` (TODO: fill in, read-only, exit 0 means "safe to drive"). Should
check: the copy's port is not in the app's production port range, every unit recorded in
`.wt.json` is active, the port is listening, the health path returns 200, and the database the
app is actually talking to is the copy's test-DB slot (never production) with no job of the
app's own mid-flight (a running job usually means a worker from another copy is attached to
this slot by mistake).

## Drive

`node $H/drive.mjs <copy> <journey> [args]` (TODO: fill in) runs one user journey against the
copy's own port with a private browser instance - never a shared browser session, which other
agents may also be driving. Journeys are named verbs scoped to one user-visible outcome, not to
a screen. Two placeholder examples (different, unrelated industries, to show the shape only):

- `book-appointment [serviceId]` - see [features/book-appointment.md](features/book-appointment.md)
- `check-stock <sku>` - see [features/check-stock.md](features/check-stock.md)

Real selectors, accessible names and placeholders belong in the feature file, read from the
actual running screen - never guessed from a mockup or from memory. Wait for the app's real
"ready" signal (network idle, a specific heading) before any click; a click sent before
hydration finishes posts nothing and the proof will look like a pass for the wrong reason.

## Evidence

Each drive should write `<copy>/.verify/<journey>-<UTC timestamp>/`: before/after screenshots
at two breakpoints (desktop and mobile widths), an accessibility-tree snapshot, a
`result.json`, and a `failure.png` only on failure. A screenshot alone is not proof of a side
effect - add the database rows the journey should have changed (a `sql.sh` helper that takes
only the copy's test-DB slot, never production, is the usual shape) into the same evidence
folder as `db-after.txt`. See [features/README.md](features/README.md) for the full
proof-and-skip convention this template expects every feature file to follow.

## Cleanup

`bash /path/to/_shared/wt-stop.sh <copy>` stops only the systemd units this copy's own
`.wt.json` recorded - never a process-name kill (`pkill`/`killall` are refused structurally,
see docs/v3-wt-interface.md §7). Evidence under `<copy>/.verify/` is left in place for whoever
looks at the report next; `wt-gc.sh` removes it later, only once the copy is marked done.

## External (excluded from verification)

List here, per app, what this skill cannot exercise locally and why (a third-party API with no
sandbox, a paid SMS send, anything that needs login to a real account) - mark those features
"외부 — 검증 제외" in their feature file rather than silently skipping them.
