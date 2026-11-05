---
name: 공방-외부점검
description: Get a read-only second opinion from a different model family (Codex) on a change, and split its findings by root cause rather than by individual finding. Use on "get an outside review", "have Codex check this", "get a cross-check".
---

# A second, read-only opinion (Codex)

## When
At the end of a change that already passed its own tests and review - after `wt-land.sh`
would otherwise run, not instead of it. Re-checking the same scope more than twice is a sign
the fix, not the check, is wrong.

## Two passes (both matter - a diff-only check misses whether the feature actually works end
to end)
1. **Whole-goal check** (can run earlier, even mid-job): read the whole repository against the
   actual user-facing goal - does the real entry point the user actually calls complete the
   job safely end to end. Ask about: the real command path, when deployment actually happens
   (once, after full verification - never mid-job), every pause point's resume path, evidence
   that each requirement is actually met (not just "a test exists"), security of anything the
   job produces, and what the tests prove versus what they skip.
2. **Diff check**: the actual command below, against the specific commits just made.

## Review depth
- Security, permissions, deployment, configuration, or an external integration: the highest
  reasoning effort available.
- An ordinary feature or bug-fix change: a medium effort is usually enough and much faster.

## Running it
```bash
codex exec -s read-only -c model_reasoning_effort="medium" --skip-git-repo-check \
  "$(cat <instructions-file>)" < /dev/null > <log-file> 2>&1
```
`< /dev/null` is required - without it the call hangs waiting on stdin. The instructions file
should give: the exact commit range, what to read first, the specific questions to answer, a
fixed output format (severity | file:line | finding | how it fails | suggested fix), and "do
not change any code." If a previous review's findings exist, include them and ask for a
FIXED/PARTIAL/NOT FIXED table against each one.

## Handling the findings
- Verify every finding directly against the code before accepting it - fix only the ones that
  are actually true.
- **Group findings by root cause before fixing anything.** The same underlying cause often
  produces several findings across different files; fixing them one at a time, file by file,
  tends to miss a sibling occurrence of the same cause elsewhere and it resurfaces on the next
  review.
- One crew per root cause, not one crew per individual finding.
- Fix every finding at the highest severity levels; judge and either fix or record a reason for
  the next tier; record only for the lowest.

Report: time spent, counts by severity, the root-cause groups and each one's fixed/partial/not-
started status, and how many of the previous review's findings are now actually fixed - never
just repeat the tool's own output as the verdict.
