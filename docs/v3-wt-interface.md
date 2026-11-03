# v3 worktree tools — command contract (v3.1)

This is the public contract the `tools/*.sh` / `wt-env.py` scripts implement, generated from a
fixed commit of the private source (see `harness/export_v3_public.py`, `v3/MANIFEST.json`).
If this document and the code ever disagree, the code is read first and this document is
corrected — the private source went through exactly that correction once already (2026-11-01:
the active-copy cap, the `node_modules` link strategy, the `PRE_SWAP_CMD` guard shape, and
`wt-migrate.sh`'s required `<land-id>` argument had all drifted from an earlier draft of this
same document; the fixes are now the behavior described below).

## Required environment (no built-in defaults — see docs/v3-adapters.md for the full table)

| Variable | What it is |
|---|---|
| `WT_APPS_ROOT` | Folder containing every app's own git checkout. |
| `WT_DATA` | Folder for work-copy logs, locks and test-DB slot bookkeeping. |
| `WT_BACKUPS` | Folder for pre-migration `pg_dump` backups. |
| `WT_DEPLOY_STATE` | Folder deploy.sh/review-gate.sh/wt-land.sh use for deploy locks and review-gate state. |
| `WT_ADMIN_ENV` | Path to an env file holding a Postgres role that can create/drop test databases. |

`WT_ROOT` (work-copy root, default `$WT_APPS_ROOT/_wt`) and `WT_SHARED` (resolved automatically
to the directory `wt-lib.sh` itself lives in) have sane defaults; everything above does not,
and the tools refuse to run with a clear message when one is missing.

## Paths and names

- A work copy: `$WT_ROOT/<app>/<name>` (`<name>` = `[a-z0-9-]+`).
- Its branch: `wt/<name>`, branched from the app's own current default branch (read with
  `git rev-parse --abbrev-ref HEAD` in the app folder — apps are not assumed to share one
  default branch name).
- Its state file: `<copy>/.wt.json` — `{app, name, branch, base, path, port, db, units: [],
  created_at, task_id?, land_id?, land_tree?, land_commit?, post_verify?, post_evidence?}`.
- Its scope file: `<copy>/.wt.scope` — one allowed path glob per line, `#`-prefixed lines
  ignored. `*` matches one path segment, `**` any depth, a line ending in `/` means "everything
  under this folder". `package.json`, lock files and any `migrations/`/`drizzle/` path pass
  scope-check only when a line names them literally (`package.json`, `package-lock.json`,
  `lock`, `migrations`, `drizzle`) — a broad `**` does not cover them.
- Its done marker: `<copy>/.wt.done` (created by a successful land, or by whatever discards the
  job).
- Test-DB slot pool: `<app>_wt1_test` … `<app>_wt<WT_SLOTS>_test` (`WT_SLOTS` default 3).
  Per-slot lock: `$WT_DATA/<app>/slot.<N>.lock`. Global limit lock: `$WT_DATA/global.mutex`.
- Deploy lock: `$WT_DEPLOY_STATE/<app>.lock` (owned by `deploy.sh`, not reentrant). Land lock:
  `$WT_DATA/<app>/land.lock` (owned by `wt-land.sh`).

## Commands

### `wt-new.sh <app> <name> [--task <id>] [--scope-file <file>]`

- Limit check (app ≤ `WT_APP_MAX` [default 3], global ≤ `WT_GLOBAL_MAX` [default 8], at most one
  active copy per app with `migrations/`/`drizzle/` in its scope) under one lock — over a limit
  prints `대기 ...` and exits **3**.
- `git worktree add -b wt/<name> <copy> <base>`; `node_modules` is a **hard-link copy**
  (`cp -al`), not a symlink — a framework whose dev server refuses a `node_modules` symlink
  pointing outside the project root will not work with a symlink strategy. Any resulting
  relative symlink inside `node_modules` that points *out* of the copy's own tree (e.g. a
  `file:` dependency on a sibling package) is re-pointed at an absolute path so it still
  resolves one level deeper inside the copy.
- A Postgres app (its env file has a `postgres://`/`postgresql://` `DATABASE_URL`) gets a test
  DB slot: every non-system schema dropped, `public` recreated for the app's role, the
  production database's own extensions recreated, then the app's migrate command run with the
  slot DB in the environment — never the app's real env file.
- Free port 4000–4999, `.wt.json` written, one JSON line on stdout.

### `wt-run.sh <copy> [--bg] [--mem 4G] [--dev | -- <command...>]`

Runs one command (or the app's dev server with `--dev`) inside `systemd-run --user --scope -p
MemoryMax=<mem>`, cwd = the copy, with an environment built by `wt-env.py app` (docs/v3-adapters.md
"환경"): the copy's own `DATABASE_URL`/`TEST_DATABASE_URL` (never the app's real one), fake
values of the same shape for everything else, `PORT`, `IF_BUILD_DIR=.next-wt`, and
`WT_SANDBOX=1` (so app code with a guarded production-default path can detect it is running
inside a copy). `PATH` is prefixed with `tools/wt-bin/` so install-type `npm`/`pnpm`/`yarn`
subcommands are refused with a message instead of silently mutating the shared `node_modules`.
`--bg` detaches and records the unit in `.wt.json`; foreground returns the command's real exit
code.

### `wt-stop.sh <copy>`

Stops only the systemd units recorded in `<copy>/.wt.json` (`systemctl --user stop`), then
clears the list. Never a process-name kill.

### `scope-check.sh <copy> [--range <a>..<b>]`

Compares what the copy changed (committed since branching, staged, unstaged-but-tracked,
untracked — work-copy artifacts like `node_modules`, `.wt.*`, build output and evidence folders
are excluded while untracked only) against `.wt.scope`. Exit 0 = everything in scope, 2 =
violations listed, 1 = could not check (never treated as "clean" — a failed git query must not
read as "no changes").

### `wt-land.sh <app> <name> [--retry <land-id>] [--dry]`

1. Land lock, then the app folder must be `git status --porcelain` clean (an unstaged
   `tsconfig.json`/`next-env.d.ts`/framework-regenerated-agent-file change alone is restored
   automatically; anything else stops here, exit **4**).
2. In the copy: no staged changes, rebase onto the app's current base (conflict → abort, exit
   **6**), `scope-check.sh` (violation → exit **2**).
3. An empty commit `land: <name>` with trailer `Landed-By: wt-land`, then
   `git merge --ff-only` in the app folder.
4. A `migrations/`/`drizzle/` path in the landed range sets `PRE_SWAP_CMD` to the migration
   adapter (docs/v3-adapters.md) before calling the deploy adapter.
5. Success: push, stop units, run the app's `verify-<app>` skill if it has one (never against
   production), record `land_id`/`land_tree` in `.wt.json`, mark done, release the test-DB
   slot. Failure: record the attempt, revert the landed range on the source, bring the running
   build/workers back to the pre-land state, exit **5** (retry with `--retry <land-id>`, which
   re-applies the same range after a fresh scope-check and goes through the same success/failure
   path).
6. `--dry` runs every check for real (nothing is skipped) and reports the first step a real run
   would stop at, without writing to either repository — though its own log and lock files are
   still written, so "dry" means "no repository or deploy change", not "no filesystem change at
   all".

An app whose own `deploy.env` sets `DEPLOY_CMD` (it deploys itself, outside this contract) goes
through the same land steps above, not a separate path: the deploy adapter call forwards to
that command, then (step 5a, step 7 below) an independent `deploy-check.sh` confirmation runs
since the exec hides the common receipt/fingerprint checks. A failure past that point runs the
app's own command again against the reverted source as recovery; if that second run also
cannot be confirmed, the land stops in the "unknown" state (exit **10**) rather than guessing.

### `wt-scope.sh <app> <name> --file <scope-file> | --add <glob>...`

Sets or extends a copy's `.wt.scope` after creation — the only sanctioned way besides
`wt-new.sh --scope-file`, since `protect_sensitive_files` refuses hand edits of `.wt.*` files.
Refuses a bare `**`/`/`/`.` line (that would mean "everything," not a scope).

### `wt-gc.sh [--dry] [--only <copy>]`

A copy is removed when: (a) `.wt.done` exists; (b) no live unit, no `task_id` at all (a
hand-made test copy) and untouched for `WT_GC_TASKLESS_SEC` (default 2h) or more; (b2) has a
`task_id`, no live unit, untouched 48h or more, and an external task-status adapter
(docs/v3-adapters.md) confirms its job ended failed/cancelled/discarded; or (c) its branch is
merged into the app's base and clean (ignoring the next-regenerated-file exception) even with
no `.wt.done` — a hand `--ff-only` merge outside `wt-land.sh`, gated by a freshness floor
(`WT_GC_FRESH_SEC`, default 30m) so a copy someone just resumed is not pulled out from under
them. A stray folder with neither `.wt.json` nor `.git` (died mid `wt-new.sh`) is archived
after `WT_GC_TASKLESS_SEC`'s longer sibling, never deleted outright. Every copy of an app
currently being landed (`land.lock` held) is skipped entirely. A dirty, untouched-48h+
hand-made copy is moved whole to `$WT_ARCHIVE_ROOT` (never deleted); everything else not
matched above is kept — including every copy when no task-status adapter is configured.

### `wt-adopt.sh <copy path>`

Registers a work copy made by hand (a `git worktree` under `$WT_ROOT/<app>/<name>` with no
`.wt.json` yet) so it counts toward the limits and the other commands can see it. No port or
test-DB slot is assigned.

### `new-app.sh <app> [--template <dir>]`

Scaffolds `$WT_APPS_ROOT/<app>` as a fresh git repo with one commit: either copies `--template
<dir>` (or `$WT_OPS_HARNESS_DIR/../templates/$WT_TEMPLATE`, when set and present) as-is, or
generates a minimal Next.js app whose dependency versions are copied from
`$WT_SCAFFOLD_VERSIONS_FROM/package.json` (required when no template applies — see
docs/v3-adapters.md). Installs nothing; exit 0 = created, 2 = usage/already exists/no version
source.

### `wt-sandbox.py` (used by `wt-run.sh`'s foreground commands via `WT_SANDBOX_BWRAP`)

Wraps a foreground command in `bubblewrap`: the copy, its own data folder and `/tmp` stay
writable; the rest of the filesystem is read-only or hidden (other apps, other copies, the
app's own untracked files, `/srv` backups/archives/logs, runtime sockets). Test-shaped commands
get no network except a loopback DB relay restricted to the environment's own slot database.
`WT_SANDBOX_BWRAP=0` disables it outright for a host without `bwrap`.

### `sec-gate.sh` / `suite-check.py` / `receipts.py` (used internally by `deploy.sh` and `wt-land.sh`)

Not meant to be called directly: `sec-gate.sh` audits a Node project's dependency changes from
the commits being landed (never a working folder); `suite-check.py` picks the test runner and
judges a run's output (0 tests, a count drop, a facts/probe file still unresolved) for
`deploy.sh`'s quality gate; `receipts.py` records and judges a deploy's outcome (survived vs.
reverted vs. unknown) from boot/start timestamps, read back by `wt-land.sh` and `wt-gc.sh`.

## Exit codes (land)

| Code | Meaning |
|---|---|
| 2 | Refused up front (bad argument, missing adapter) or scope violation |
| 3 | Nothing to land (0 commits ahead of base) |
| 4 | App folder or copy not clean |
| 5 | Deploy failed — reverted automatically |
| 6 | Rebase conflict, or another land already running |
| 7 | Deployed, but the push afterward failed |
| 10 | Self-deploy recovery could not be confirmed either — a person must look, then re-run with `--ack-unknown` |
