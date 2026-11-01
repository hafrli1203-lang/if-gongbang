# v3 — one unit, walked through

**This is one sample, not a benchmark.** A single unit of real work run end to end through the
v3 tools, with timings and token cost, reported exactly as it happened — including the one
point it needed a person. It is evidence that the pipeline works end to end at least once, not
a claim about typical speed, cost, or reliability. No conclusion about "v3 is N× faster" or
"v3 always needs 0 interventions" should be drawn from one run; a general claim needs a
sample, not an anecdote.

## What counts as start and end

- **Start**: the task is accepted by the agent session (first token of its own work, not the
  time a person spent writing the request).
- **End**: the change is pushed to the app's default branch and the deploy has returned
  success.
- Everything in between — including the one manual step — counts; nothing is excluded for
  looking bad.

## The run

| Stage | Result |
|---|---|
| Task accepted → session work | 653 seconds, 58 turns, $1.98 (model usage cost for this session) |
| Commit | made inside the work copy, scope intact |
| Scope check | passed — every changed path was inside `.wt.scope` |
| Automatic land attempt | stopped, exit 4 — an uncommitted change in the app folder (an automatically-regenerated project file) blocked the fast-forward merge |
| Manual intervention | 1 — a person cleared the blocking file and re-ran the land step |
| Deploy | 185 seconds |
| Push | completed |

Environment: the same host and commit history this bundle's tools were extracted from
(private; not published here — see `v3/MANIFEST.json` for the source commit this bundle's code
corresponds to). Failures and the wait before the manual step are included in the numbers
above, not subtracted out.

## What this does and doesn't show

- It shows the full pipeline — work copy, commit, scope check, land, deploy, push — completing
  for one real change, and shows exactly where it needed a human (an automatic-file collision
  the land step did not yet handle on its own at the time of this run).
- It does not show a distribution of run times, a failure rate, or a cost-per-unit average —
  those need many runs, not one.
- The specific blocking condition (an automatically-regenerated file left uncommitted in the
  app folder) is the kind of thing `wt-land.sh`'s restore-known-auto-files step
  (docs/v3-wt-interface.md, "App folder must be clean") exists to prevent; whether it would
  recur on a re-run of the *same* kind of change is not established by this one sample either.

## Reproducing a measurement like this

Run a real unit of work through the tools, record the same six rows above (session
cost/time/turns from your own session metadata, each `wt-*.sh` exit code and timing from its
own log under `$WT_DATA/<app>/land-<id>.log`), and report them with the same two caveats: one
sample, and failures included rather than discarded.
