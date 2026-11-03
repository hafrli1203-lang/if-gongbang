# v3 adapter boundaries

v3's tools call out to a handful of things that are specific to whoever is running them: a
deploy mechanism, a task-status source, a memory-capped build wrapper, and the set of internal
service addresses a work copy must never reach. Each one is an explicit adapter — a documented
contract plus an environment variable — not a hardcoded assumption. This page lists every one,
what happens when it is absent, and why it was drawn at that boundary rather than folded into
the tool itself (Codex review, 2026-11-01: the earlier draft of this bundle assumed its own
`deploy.sh`/`ship.sh`, an AX-OS-shaped SQLite task database, and a specific memory-capped build
script all existed — none of those are portable, so each became an adapter here instead).

## Required environment (no default — the tools refuse to run with a clear message)

| Variable | Used by | What it is |
|---|---|---|
| `WT_APPS_ROOT` | all | Folder containing every app's own git checkout, one subfolder per app. |
| `WT_DATA` | all | Folder for work-copy logs, locks, and test-DB slot bookkeeping. |
| `WT_BACKUPS` | `wt-migrate.sh` | Folder for pre-migration `pg_dump -Fc` backups. |
| `WT_DEPLOY_STATE` | `deploy.sh`, `review-gate.sh`, `wt-land.sh` | Folder for deploy locks and review-gate state (previous build kept here, outside the project tree, so a build tool that scans the whole project does not also scan the previous build). |
| `WT_ADMIN_ENV` | `wt-new.sh` | Path to an env file whose `DATABASE_URL` is a Postgres role allowed to create/drop test databases. |

## Optional adapters (absent = a documented, safe fallback — never a silent wrong answer)

### Deploy adapter — `deploy.sh` / `ship.sh`

`wt-land.sh` calls `tools/deploy.sh <app>` (or, when the app's `deploy.env` sets
`REVIEW_GATE=1`, `tools/ship.sh <app>` — **not included in this bundle**: it wraps a live-run +
automated-review workflow specific to the private source this was extracted from, and calling
back into that wiring was out of scope for the extraction. An app that sets `REVIEW_GATE=1`
without providing its own `ship.sh` gets a clear refusal — `wt-land.sh` exit 2 — rather than a
raw "No such file" from bash).

**Contract `deploy.sh <app> [--check | --rollback]` must satisfy** for `wt-land.sh` to use it:
- Build in a separate folder; swap into place only on a successful build. Never leave the
  running app in a half-built state.
- Honor `PRE_SWAP_CMD` (env var, may be empty): run it after a good build, before the swap —
  `if [ -n "${PRE_SWAP_CMD:-}" ]; then bash -c "$PRE_SWAP_CMD" || exit 8; fi`. **Written as an
  `if`, not `[ -n ] && cmd || exit 8`** — the latter exits 8 on every deploy that has no
  `PRE_SWAP_CMD` at all, not only on a real failure (reproduced once; the fix above is the
  contract now).
- `--rollback`: restore the previous build and restart, callable even with no prior failed
  deploy in this process (a separate `wt-land.sh` retry path calls it standalone).
- Exit codes `deploy.sh` without a mode uses that `wt-land.sh` interprets: build never started
  or build failed (1/2/3/4/6/8, in increasing specificity — "nothing was swapped, the old build
  is still serving") vs. already rolled back internally (5/7 — "swap happened, then this script
  already put the old build back"). A deploy adapter that does not distinguish these two
  families will cause `wt-land.sh` to call `--rollback` redundantly, which is harmless but noisy.

### Task-status adapter — `WT_TASK_STATUS_CMD`

`wt-gc.sh`'s staleness rule (case *b*: no live unit, 48h untouched, job confirmed abandoned)
needs to ask something "is job `<task-id>` really over, and did it fail/get cancelled?" The
private source answered that from an internal SQLite task database; here it is a command:

```
WT_TASK_STATUS_CMD="<program that takes a task id and prints a status word to stdout>"
```

**Unset: every copy with a `task_id` is always treated as "status unknown", which this script
already keeps rather than removes** — so with no adapter configured, `wt-gc.sh` only ever
removes copies by their own `.wt.done` marker (case *a*), never by inferred staleness. This is
a deliberately conservative default: the cost of not knowing is a kept copy, never a removed
one.

### Memory-capped build wrapper — `WT_HEAVY_CMD`

`deploy.sh`'s build step wants to cap memory and name the job for whatever process accounting
the host does. The private source used an internal script; here:

```
WT_HEAVY_CMD="<program taking --mem <cap> --name <name> -- <command...>>"
```

**Unset: the build command runs directly, with a warning printed to stderr, and no memory
cap.** A host with no spare memory headroom should set this; a host that already runs one
build at a time with room to spare can skip it.

### Internal-service dead-ends — `WT_INTERNAL_URL_KEYS`

`wt-env.py` always points a copy's database and token-file settings somewhere safe (never a
live value — see "환경" below). For *other* addresses — a company's own internal services a
copy's code might default to reaching (an orchestration API, an internal event log, a search
index) — the specific key *names* to redirect are environment-specific:

```
WT_INTERNAL_URL_KEYS="SERVICE_A_URL,SERVICE_B_URL"   # comma-separated env-file key names
```

Each listed key is always set to a dead loopback address in a copy, overriding both the app's
own env file and any fake value supplied elsewhere. **Unset: no key gets this specific
treatment** — generic protections (DB-shaped keys always point at the test-DB slot,
`*_TOKEN_FILE`/`*_TOKEN_PATH` keys always point at a path that does not exist) still apply
regardless.

### Live-data-root relocation — `WT_LIVE_DATA_ROOT`

Some env-file values are a *file or folder path* under wherever an app keeps its persistent
state, not a URL or a database address (e.g. a JSON state file, an upload folder). If that
root is set:

```
WT_LIVE_DATA_ROOT="/path/apps use for persistent non-database state"
```

any non-database env value found to start with it is relocated to a folder inside the copy
(`<copy>/.verify/data/<KEY>`) instead of being faked outright, so an app whose code reads and
writes a real file at startup gets an empty one of its own rather than either a crash or a
shared file across copies. **Unset: this relocation never runs** — a value shaped like a path
under nothing in particular is just faked like any other string value.

### Unit-name prefix — `WT_UNIT_PREFIX`

Default `ifapp` — change only if the host's own convention for an app's systemd service names
differs (`wt-land.sh`'s worker-restart step and `deploy.sh`'s rollback step look for
`<prefix>-<app>-*.service`).

### Owner-alert adapter — `notify-owner.mjs` (not included in this bundle)

`wt-new.sh` (a limit/slot/port wait) and `wt-land.sh` (self-deploy recovery exhausted) both call
`node "$WT_SHARED/notify-owner.mjs" "<one line>" "<link>" "<channel>"` to page a person. Like
`ship.sh`, this is intentionally **not extracted**: the private source's version reaches into an
internal chat-bot/notification service that has no public equivalent. Both call sites already
treat it as optional and best-effort (`|| true` / a logged warning on failure, never a hard
stop) — unset or missing, a copy just waits/stops at the same point with the condition printed
to its own log instead of paged to a person. Drop in your own script at that path with the same
three-argument CLI to wire up a real alert channel.

### Garbage-collection archive root — `WT_ARCHIVE_ROOT`

`wt-gc.sh` moves a stale-but-uncommitted hand-made copy under here instead of deleting it
(`<root>/<date>/wt-stale/<app>-<name>`) — required, no default, so a host that has not set one
gets a clear refusal rather than silently writing nowhere or to a guessed path.

### Ops/harness read-only mount — `WT_OPS_HARNESS_DIR`

`wt-sandbox.py` exposes this one folder read-only inside the bwrap sandbox (nothing else from
the host's own ops workspace — plans, a task queue, etc.) so that an app's own test suite can
import a test-trust check module from it. **Unset (default): nothing is exposed** — an app
whose tests import from there will fail inside the sandbox exactly as if the module did not
exist, which is the safe default for a host with no such module to share.

### Scaffold version source — `WT_SCAFFOLD_VERSIONS_FROM`

`new-app.sh` (no `--template`) copies its `next`/`react`/`typescript` versions from an existing
app's `package.json` rather than inventing numbers — required, no default, since there is no
safe app to assume exists on every host. Point it at any existing Next.js app's folder.

## 사본 환경 (what a work copy's own environment looks like)

Built by `wt-env.py app` (never prints a value — everything stays inside the child process's
own environment). Starting from an allow-list of the caller's own environment (`PATH`, `HOME`,
locale, a short list of build/CI flags, and anything already prefixed `WT_`) — **not the whole
of it**, so a parent session's own API keys and tokens never reach a work copy by inheritance
(Codex review, 2026-11-01 — reproduced with a synthetic key once, fixed by switching from "drop
known-bad keys" to "start from an allow-list", which is what ships now):

- Any `*DATABASE*URL*`-shaped key pointing at a real Postgres URL → the copy's own test-DB
  slot, same host/user, different database name.
- A key listed in `WT_INTERNAL_URL_KEYS` → a dead loopback address, always.
- A `*_TOKEN_FILE`/`*_TOKEN_PATH`-shaped key → a path that does not exist.
- A value starting with `WT_LIVE_DATA_ROOT` (if set) → relocated into the copy's own folder.
- Everything else → a same-shaped fake value from `<copy>/.claude/verify-env.json` if that key
  is listed there, else a value of the same JSON/shell "shape" as the real one (a number stays
  a number so a schema validator does not reject it, a URL becomes a dead one, a boolean
  becomes `false`, anything else becomes the literal string `x-<KEY>`).

## Required to run at all

Linux, `systemd --user` (user-session scopes for memory-capped execution), `git`, `python3`,
`node`, and `psql`/`pg_dump`/`pg_restore` for any app with a Postgres database. Not tested on
macOS or inside a container without a user systemd instance.
