# fence — hook gates that enforce the harness's own workflow

These are Claude Code hooks (`settings.json` entries) that enforce the parts of the workflow a
plain instructions file cannot guarantee by itself: a report before a code change, staying
inside an approved set of app folders, reading a whole document instead of its first lines,
checking a done-report against the actual directives it claims to follow, and capping how long
the orchestrating session keeps acting by hand instead of handing work to a job track or a
sub-agent.

Each gate fails closed on a match (denies the tool call with an exit code the harness reads as
"deny") and fails open on its own internal errors, so a bug in a gate cannot itself block all
work.

## What each gate blocks

- **preflight_gate_hook.py** — `PreToolUse` on `Edit | Write | MultiEdit | NotebookEdit | Bash`.
  Denies a change to app code unless the current turn already contains a plain-text message with
  four headings: what was read, the expected risk, which files will change, how to undo it. A
  bare redirect (`>`, `>>`) only counts as an app-code write when its target is a literal path
  under the apps root; a shell-variable target or a path outside the apps root is not blocked.
- **directive_check_hook.py** — `Stop`. Denies ending the turn with a "done" claim unless the
  report quotes the actual directive(s) used this turn and checks the result against them.
- **read_whole_hook.py** — `PreToolUse` on `Read | Bash`. Denies reading only the first lines of
  a document under a line-count threshold when a cat/head-style partial read is detected, so a
  short document gets read in full before it is judged.
- **scope_fence_hook.py** — `PreToolUse` on `Edit | Write | MultiEdit | NotebookEdit | Bash |
  Agent`. When a scope file lists which app folders are currently in play, denies writing to, or
  launching a sub-agent against, any app folder outside that list. The scope file itself can only
  be changed by a separate script run directly by a human (never by a model tool call).
- **orbit_nudge_hook.py** — `Stop`. Does not block; appends a short notice to the turn when the
  same test has failed three times in a row, or when the turn is about to end while still waiting
  on a result, so the session is nudged toward handing the work to a job track or doing something
  else in parallel instead of looping by hand.
- **bansang_habit_hook.py** — `PreToolUse` on `Bash | Edit | Write`. Caps how many terminal/edit
  calls the orchestrating session makes by hand: denies once a rolling window of recent tool
  calls is mostly `Bash` with no skill or library lookup among them, and denies once the count of
  hand-run calls since the last human message passes a fixed cap. Calls made by a sub-agent, and
  calls to `Agent`, `Skill`, `Read`, `Grep`, `Glob`, or read-only lookups, are never counted.
- **worker_rules_hook.py** — `PreToolUse` on `Bash | Monitor | ScheduleWakeup`, scoped to
  sub-agent calls only (never the orchestrating session itself). Denies a sub-agent running
  `git stash`, `git reset`, `git restore`, a revert-style `git checkout`, `git add -A/--all/.`, or
  a commit inside the shared main repository (a sub-agent commits only inside its own work tree).
  Also denies any way of stopping the turn to wait for a result — background execution, a
  polling tool, or a sleep loop — since a sub-agent is expected to run with a foreground timeout
  and keep working.
- **set_scope.py** — not a hook. A script a human runs directly (`!python3 set_scope.py <apps...>
  --reason "..."`) to write the scope file `scope_fence_hook.py` reads. Writes the file
  atomically and keeps the previous value as a `.bak`.
- **ifapp-gongbang-watch.service** — a systemd user unit that runs a stall-watch loop on a fixed
  interval, so a stuck job is resumed even with no one watching. Installing and enabling the unit
  is an action only a human takes directly; the installer's self-test writes the unit's file
  content to a throwaway home directory and never calls `systemctl`.

## Installing

Each gate has its own installer (`apply_<name>.py`), run directly by a human (not by a model):
copies the hook file into place, backs up the current hook and `settings.json`, registers the
hook, then runs a self-test with a fake tool call; on failure it rolls back its own install only.

```
python3 apply_gongbang_all.py          # install/update preflight, directive-check,
                                        # read-whole, scope-fence, and the habit cap, in order
python3 apply_gongbang_all.py --check  # same, but report-only, no changes
python3 apply_gongbang_watch.py        # the stall-watch unit (separate: it restarts a service)
```

Each installer also supports `--check` (report only) and `--undo` (restore the previous hook and
settings).

## Turning a gate off

Every gate except the habit cap and the worker-rules gate reads a small marker file before it
runs and skips the check if the file exists — a deliberate escape hatch for a human to flip, not
something a model call can create on its own:

```
~/.claude/gate/preflight-off
~/.claude/gate/directive-check-off
~/.claude/gate/read-whole-off
~/.claude/gate/scope-fence-off      # does not exempt changes to the scope file itself
~/.claude/gate/orbit-nudge-off
```
