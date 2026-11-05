---
name: 일
description: A user says "/일" or "build an app like this" or "change this app to do X", or a chat-style task-dispatch front door receives the same kind of request. Starts the v3 work-copy orbit (read facts → pass scenario → disposable work copy → implement/test → a second read-only opinion for risky work only → land/deploy → evidence report → lesson). Saying this is the instruction to actually run it.
---

# 일 — start the v3 work-copy orbit

Being asked this (by command or by an equivalent chat request) means: actually do this job on
this app now. Do not widen it to another app or another job. The entry point (a terminal, or a
chat-style dispatcher) never changes the procedure below — the same work copy, scope check,
land and deploy run either way (see `docs/v3.md`).

## 0. One line before starting
Write "doing this / not doing this / expected result" in one line, quoting the request as
given. If no app is named, ask only that.

## 1. Facts first
Read this app's own current documentation (its README, a decisions log such as
`docs/CURRENT_DECISIONS.md` if it has one, its `deploy.env`) before touching anything. If this
project keeps a lessons/issue log, check it for this app before starting.

## 2. Write the pass scenario first
One paragraph, before writing code: "what the person does → what the app shows → where on the
screen." A form with nothing wired behind it is not an implementation. For anything touching
auth, payments, deletion, or an external integration, verify the exact behavior against real
evidence before coding it in.

## 3. Work copy — scope decided up front
Never edit the app's own checkout directly. Before creating the copy, list every file you
expect to touch and put all of them in a scope file from the start (adding one path at a time
afterward is not supported the same way — if scope legitimately grows mid-job, extend it with
`wt-scope.sh --add` and say why in the report):

```bash
printf '%s\n' "<file to change 1>" "<file to change 2>" > /tmp/<job-name>.scope
bash tools/wt-new.sh <app> <job-name> --scope-file /tmp/<job-name>.scope
```

`package.json`, a lock file, or any `migrations/`/`drizzle/` path only counts as in-scope when
the scope file names it literally — a broad `**` does not cover those four. Hitting the
per-app or global copy limit prints "waiting ..." — wait, or flag it to whoever is coordinating.

## 4. Implement and test (inside the copy only)
- Change code only inside the copy (`$WT_ROOT/<app>/<job-name>`).
- Tests run against the copy's own test-database slot, never the production database.
- Run the dev server or a command with `bash tools/wt-run.sh <copy> --dev` or `-- <command>`.
- When something fails, fix the exact spot rather than re-running the same check unchanged. If
  the same finding repeats twice, change the approach.

## 5. A second read-only opinion — risky work only
Only for work touching auth, permissions, payments, deletion, an external integration,
deployment, or configuration:

```bash
codex exec -s read-only -c model_reasoning_effort="high" --skip-git-repo-check \
  "<one line: what to look at>" < /dev/null
```

Omitting `< /dev/null` hangs the call. Ordinary code changes skip this step — do not add a new
review stage beyond what is already here.

## 6. Land and deploy — `wt-land.sh`
```bash
bash tools/wt-land.sh <app> <job-name>
```
Runs the scope check, a secret-leak check on the added lines, a land commit, a fast-forward
merge, and the deploy adapter, in one call — success pushes and cleans up the copy; failure
reverts and restores the previous running state automatically. Do not stop to ask for
permission before this step. On failure, either retry with `--retry <land-id>` or fix the root
cause and run the whole step again.

## 7. Evidence report (lead with these four lines)
Before any technical detail (hashes, paths), write four plain lines:
1. What was fixed and what works now
2. The numbers that back it up (tests passed, commit, deployed or not)
3. What was not verified (or "none")
4. Any other problem noticed outside this job's scope (or "none")

Never write "done" or "passing" without the command output that proves it. Report a failure as
a failure.

## 8. Lesson
If this project keeps a lessons log, record anything a reviewer corrected, so the next job on
this app starts knowing it.

## Keep to
- No artificial cost or retry caps — if blocked, fix it directly rather than giving up at a
  limit.
- Never hardcode an industry or brand not present in the actual request.
- Do not wrap `wt-new.sh`/`wt-land.sh`/`wt-scope.sh` in a short timeout — let a long test or
  deploy run to completion in the foreground.
- Never hand-edit a `.wt.*` state file — extend scope only through `wt-scope.sh --add`.
