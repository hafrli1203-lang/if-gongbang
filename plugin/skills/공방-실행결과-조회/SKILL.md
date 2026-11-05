---
name: 공방-실행결과-조회
description: Look up a work copy's current status read-only - what it has changed, whether it landed, and whether anything is still running. Use on "where did that job get to", "check on that app's work copy", "is anything still pending". Read-only; never changes state.
---

# Check a work copy's status (read-only)

Works for any app this install's work-copy tools manage - no app name is hardcoded here.

## What to read (all read-only)
1. `python3 tools/wt_doctor.py [<app>]` - every live work copy, its scope file, commit count,
   uncommitted files, and whether its dev-server unit is actually up.
2. `cat $WT_ROOT/<app>/<job-name>/.wt.json` - state since creation: `port`, `db`, `units`
   (live systemd scopes, if any), `land_id`/`land_commit`/`landed_at` (present once it has
   landed), `post_verify`.
3. `[ -f $WT_ROOT/<app>/<job-name>/.wt.hold ]` and its contents - whether it is deliberately
   waiting on a person (not stalled - see `wt-hold.sh`); `tools/stall_watch.py`'s table
   reports these two cases differently and does not chase a "waiting on a person" copy.
4. `python3 v3/tools/receipts.py list <app's receipts folder>` (or `blocking <folder>` for
   only the unresolved ones) - the most recent deploy's outcome (success / unknown / reverted),
   its test count, and the deployed tree.
5. `git -C $WT_ROOT/<app>/<job-name> log --oneline <base>..HEAD` and `git status --short` -
   its actual commits and any uncommitted change.
6. `bash tools/scope-check.sh $WT_ROOT/<app>/<job-name>` - whether what it changed is still
   inside its own declared scope (exit 0 clean, 2 lists violations, 1 means it could not check
   - never read a failed check as "clean").
7. If this install runs `tools/daily_review.py`, its latest report shows the shape of recent
   trouble across every job, not one job's own detail.
8. `bash tools/wt-land.sh <app> <job-name> --dry` shows what landing would do right now,
   without changing anything, if that is specifically what's being asked.

## Report format
- App / job name
- Status: `wt_doctor.py`'s verdict + landed (with commit) / in progress (last commit time) /
  waiting on a person (hold reason) / done and cleaned up
- Deploy receipt: success / unknown / reverted, test count, last deploy time
- Changed files: from the `git log`/`git status` above, or "none yet"
- Scope check: clean / violations (listed) / could not check
- **Never fill in a field without having actually run the command above for it.** A field you
  could not check is "not checked: <what>", not a guess.

## Out of scope
This is read-only. Resuming, retrying a failed land, settling an "unknown" deploy outcome
(`wt-land.sh ... --resolve kept|reverted` / `--ack-unknown`), or releasing a hold
(`wt-hold.sh --release`) is a separate, deliberate action - not something this lookup does on
its own, and never guessed at from outside what production actually shows.
