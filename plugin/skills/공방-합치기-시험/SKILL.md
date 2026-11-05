---
name: 공방-합치기-시험
description: Merge several crews' work copies for one job into one, run the full test suite once, and record the result before landing. Use on "merge these", "combine and test", "bring the crews together".
---

# Merge crews' work and test once

Only needed when several crews from `공방-작업조-붙이기` worked on separate copies of the same
job and must become one change before `wt-land.sh` runs. A single crew's single copy skips
this - go straight to landing.

## Before merging
- Look at each copy's real diff (`git -C <copy> log --oneline <base>..HEAD`, `git diff <base>
  <branch> --stat`) - know exactly what each one actually changed.
- If a copy has a commit that was meant to be thrown away (e.g. a revert that should not come
  back), `git cherry-pick` only the commits that should survive rather than a plain merge - a
  three-way merge can silently keep a stale version of a file only one side touched.

## Merging
```bash
git -C <one copy> merge --no-edit <other copy's branch>
```
On conflict:
- Two sides added different things to the same list (an exports list, a config-key list) -
  keep both.
- Two crews independently wrote the same function - keep whichever version's own tests cover
  more, delete the other.
- Resolve, then run only the directly affected test files first, and the full suite after.

## Testing
Run the project's own full test command once, in the foreground. Record the exact pass/fail
count from that run - do not round it or describe it from memory. A failure after a merge may
be the merge itself or a pre-existing defect; say which, with evidence, before reporting.

## After
Land the merged result the normal way:
```bash
bash tools/wt-land.sh <app> <job-name>
```
This one call does the scope check, a secrets check, the land commit, the fast-forward merge,
the deploy, and the deploy receipt check, in order - success pushes and cleans up the copy,
failure reverts and restores the previous running state automatically.

If the receipt comes back "unknown" (the deploy process died or never confirmed), `wt-land.sh`
stops without reverting or retrying - check production yourself first, then settle it
explicitly:
```bash
bash tools/wt-land.sh <app> <job-name> --resolve kept       # the new build is actually running
bash tools/wt-land.sh <app> <job-name> --resolve reverted    # it's actually back on the old build
bash tools/wt-land.sh <app> <job-name> --ack-unknown          # just clear the mark after checking
```
Never guess a `--resolve` value without having looked at production.

A finished copy whose branch is already merged and has no uncommitted changes is removed by
`wt-gc.sh` on its own - do not force-remove one that still has uncommitted work.

Report: which copies were merged, the exact test numbers, the deploy receipt's outcome, and
anything left unmerged.
