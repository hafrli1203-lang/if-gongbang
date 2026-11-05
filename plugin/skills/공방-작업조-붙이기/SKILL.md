---
name: 공방-작업조-붙이기
description: Attach several implementation sub-agents to one job at once, each with its own work copy and a fixed file-ownership split. Use on "attach a crew", "split this and run it in parallel", "work on these at the same time".
---

# Attach a crew (parallel work copies)

## Decide before splitting (do not change after)
1. **One contract**: fix the function names, arguments, file paths and config keys the crews
   hand to each other in one short doc before anyone starts. If it must change mid-job, tell
   every crew the same message and update the contract too.
2. **A file-ownership table**: one row per crew, the files it may change, and every other
   crew's files marked "do not touch". Never give the same file to two crews, and never
   re-split a job partway through — a late instruction landing twice on the same file is how
   this goes wrong.

## Work copies
```bash
bash tools/wt-new.sh <app> <crew-name-1> --scope-file /tmp/<crew-name-1>.scope
bash tools/wt-new.sh <app> <crew-name-2> --scope-file /tmp/<crew-name-2>.scope
```
Each crew gets its own copy under `$WT_ROOT/<app>/<crew-name>` - never a shared checkout.

## Required lines in every crew's briefing
- Its work-copy path, its scope file's contents, the contract's location, and its own
  owned/forbidden files.
- Git: `git add <path>` + `git commit -m "..." -- <path>` only. No stash, reset, checkout, or
  `add -A`.
- Run the full test suite once, in the foreground, at the end - not repeatedly, not in the
  background.
- No real deployment, no access to another app's repository, unless the coordinator explicitly
  said so for this job.
- Report format: commit hash, the test suite's final line, what was not finished.

## While it runs
- If a report is late, check the copy's own commit count and uncommitted-file count rather
  than waiting blind; if a backgrounded test run stalled, tell that crew to re-run it in the
  foreground.
- When every crew is done, land through `wt-land.sh` per copy, or merge the copies into one
  before landing - see the companion merge skill.
