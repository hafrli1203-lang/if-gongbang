"""Read-only reporting (`status`/`backup`/`log`/`diff`/`board`) plus
`sync` (rebase the app worktree onto its default branch) and `doctor`
(the environment pre-flight every other command group's `new`/`change`
also calls before touching anything)."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from engine.cli import cmd_backup as _engine_cmd_backup
from engine.cli import cmd_status as _engine_cmd_status
from engine.config import EngineConfig

from . import shared

__all__ = [
    "cmd_status", "cmd_backup", "cmd_log", "cmd_board", "cmd_doctor",
    "cmd_diff", "cmd_sync", "require_doctor_ok",
]


def cmd_status(args: argparse.Namespace) -> int:
    config = EngineConfig.from_json(shared.app_workspace(args.app) / "config.json")
    return _engine_cmd_status(config, argparse.Namespace(project=args.app))


def cmd_backup(args: argparse.Namespace) -> int:
    """Copies this app's ledger with engine.cli backup (verified copy,
    newest --keep kept) into --dest or the workspace's backups folder."""
    config = EngineConfig.from_json(shared.app_workspace(args.app) / "config.json")
    return _engine_cmd_backup(config, argparse.Namespace(dest=args.dest, keep=args.keep))


def cmd_log(args: argparse.Namespace) -> int:
    runs = shared.read_run_log(args.app)[-5:]
    print(json.dumps({"runs": runs}, ensure_ascii=False, indent=1))
    return 0


def _discover_apps() -> list[str]:
    """Every app under GB2_WORKSPACE/apps/ that has a config.json --
    used by `gb2 board` with no <app> argument, and by the doctor
    checks below. A directory without a config.json (e.g. left over
    from a failed `init`) is skipped, not an error; `board` must still
    render the rest of the workspace. Reads shared.APPS_DIR as a
    module attribute (not a copied import), so a test's
    `monkeypatch.setattr(shared, "APPS_DIR", ...)` is seen here too."""
    if not shared.APPS_DIR.exists():
        return []
    return sorted(
        entry.name for entry in shared.APPS_DIR.iterdir()
        if entry.is_dir() and (entry / "config.json").exists()
    )


def cmd_board(args: argparse.Namespace) -> int:
    """`gb2 board [<app>] --out <file.html>`: a static, read-only HTML
    dashboard (engine.board.build_board) over one app's ledger, or
    every app's ledger when <app> is omitted. See engine/board.py's own
    module docstring for what the page shows and why it reads the
    ledger sqlite file directly instead of through engine.ledger.Ledger.

    `--lessons-export`/`--queue` are plain file paths (not engine
    config keys): engine/config.py is outside this file's owned scope
    for this task, so the optional lessons-export JSON and
    WORK_QUEUE-like markdown the plan describes are passed as CLI
    flags here instead of new EngineConfig fields -- same information,
    no edit to a file this change does not own."""
    from engine.board import build_board

    app_names = [args.app] if args.app else _discover_apps()
    app_ledgers: dict[str, tuple[Path, str, Path]] = {}
    skipped: list[str] = []
    for app in app_names:
        config_path = shared.app_workspace(app) / "config.json"
        if not config_path.exists():
            skipped.append(app)
            continue
        config = EngineConfig.from_json(config_path)
        if app not in config.projects:
            skipped.append(app)
            continue
        # The repo enables the board's evidence-graph completeness check.
        app_ledgers[app] = (config.ledger_path, app, config.projects[app].repo)

    lessons_path = Path(args.lessons_export) if args.lessons_export else None
    queue_path = Path(args.queue) if args.queue else None
    page = build_board(app_ledgers, lessons_path=lessons_path, queue_path=queue_path)

    out_path = Path(args.out) if args.out else (shared.GB2_WORKSPACE / "board.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    print(json.dumps(
        {"ok": True, "out": str(out_path), "apps": sorted(app_ledgers), "skipped": skipped},
        ensure_ascii=False,
    ))
    return 0


# --------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------

def _doctor_cmd_present(name: str) -> tuple[bool, str]:
    found = shutil.which(name)
    return (found is not None, found or "not found on PATH")


def _doctor_bwrap_runs() -> tuple[bool, str]:
    """shutil.which alone is not enough -- a bwrap binary that is present
    but cannot actually create its own namespaces (e.g. missing kernel
    support, missing setuid bit) fails exactly the way the real sandbox
    would at job time, so this runs the same read-only self-mount the
    docstring promises (`bwrap --ro-bind / / true`) rather than trusting
    PATH alone."""
    if shutil.which("bwrap") is None:
        return False, "not found on PATH"
    try:
        result = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "true"],
            capture_output=True, timeout=10,
        )
    except OSError as exc:
        return False, f"could not run: {exc}"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    if result.returncode != 0:
        tail = (result.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        return False, f"exited {result.returncode}: {tail}" if tail else f"exited {result.returncode}"
    return True, "ok"


def _doctor_release_apps() -> list[str]:
    """Every app (under GB2_WORKSPACE/apps/) whose config.json has at
    least one project with a "release" key configured -- `doctor` only
    requires a deploy script to exist for THESE apps, never for a
    workspace that has no release-configured app at all."""
    apps: list[str] = []
    for app in _discover_apps():
        try:
            raw = json.loads((shared.app_workspace(app) / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        projects = raw.get("projects")
        if isinstance(projects, dict) and any(
            isinstance(p, dict) and p.get("release") is not None for p in projects.values()
        ):
            apps.append(app)
    return apps


def _doctor_checks() -> list[tuple[str, bool, str]]:
    """Every external dependency `gb2` itself relies on (never a
    per-app concern, so this never touches an app's own repo/worktree):
    python3.12, git, a bwrap that actually runs, the claude/codex CLIs,
    node/npm (an app's own test_cmd/install_cmd need these, not gb2
    itself, but they are required by tools/install.sh and checked here
    for the same reason), GB2_APPS_ROOT being set, and -- only if some
    app has release configured -- its deploy script existing on disk.
    Shared by `cmd_doctor` (prints every check) and `require_doctor_ok`
    (gb2 new/change's own pre-flight, before touching anything).

    When GB2_SCRIPTED_WORKERS is set (shared.scripted_workers_dir), the
    claude/codex entries are left out entirely: a scripted-workers run
    never invokes either CLI (shared.apply_scripted_workers rewrites
    every role's own WorkerConfig.runner to a ScriptedRunner), so
    requiring them on a host that only has the scripted fakes installed
    would block a job that can never actually need them. Every other
    check still runs unconditionally."""
    checks: list[tuple[str, bool, str]] = []

    cli_names = ("python3.12", "git", "node", "npm")
    if shared.scripted_workers_dir() is None:
        cli_names = ("python3.12", "git", "claude", "codex", "node", "npm")
    for name in cli_names:
        ok, detail = _doctor_cmd_present(name)
        checks.append((name, ok, detail))

    bwrap_ok, bwrap_detail = _doctor_bwrap_runs()
    checks.append(("bwrap", bwrap_ok, bwrap_detail))

    apps_root_value = os.environ.get("GB2_APPS_ROOT", "")
    checks.append(("GB2_APPS_ROOT", bool(apps_root_value),
                   apps_root_value if apps_root_value else "not set"))

    release_apps = _doctor_release_apps()
    if release_apps:
        if apps_root_value:
            deploy_script = shared.deploy_script_path(Path(apps_root_value))
            exists = deploy_script.exists()
            detail = f"{deploy_script} (release apps: {', '.join(release_apps)})"
        else:
            exists, detail = False, "GB2_APPS_ROOT not set -- cannot resolve deploy script"
        checks.append(("deploy_script", exists, detail))

    return checks


def require_doctor_ok() -> str | None:
    """None if every `_doctor_checks` entry passes, else a one-line
    error message naming the ones that don't. `workspace.cmd_new`/
    `runner.cmd_change` call this FIRST, before `shared.apps_root()` or
    anything else that touches disk -- a missing CLI or unset
    GB2_APPS_ROOT must surface immediately, not as an opaque failure
    partway through scaffolding or a mid-job worker crash."""
    missing = [(name, detail) for name, ok, detail in _doctor_checks() if not ok]
    if not missing:
        return None
    detail = "; ".join(f"{name}: {detail}" for name, detail in missing)
    return f"doctor check failed -- fix the environment first ({detail})"


def _doctor_node_modules_status() -> list[tuple[str, bool]]:
    """(app, node_modules present) for every app `_discover_apps` finds
    -- only meaningful once GB2_APPS_ROOT is set, same as
    `_doctor_release_apps`; [] otherwise. Reported by `cmd_doctor` for
    visibility only (engine.bootstrap already installs this at
    `gb2 new`/`gb2 init` time, so it missing on an already-set-up app
    means a previous bootstrap failed or this app predates bootstrap
    existing) -- it is deliberately NOT part of `_doctor_checks`/
    `require_doctor_ok`, since one unrelated app's missing
    node_modules must never block `gb2 new`/`gb2 change` for a
    DIFFERENT app."""
    apps_root_value = os.environ.get("GB2_APPS_ROOT", "")
    if not apps_root_value:
        return []
    apps_root_dir = Path(apps_root_value)
    return [(app, (apps_root_dir / app / "node_modules").exists()) for app in _discover_apps()]


def cmd_doctor(args: argparse.Namespace) -> int:
    """Prints one line per `_doctor_checks` entry (exit 1 if anything
    required there is missing), plus one informational line per app
    reporting whether its own node_modules is present -- see
    `_doctor_node_modules_status`; those lines never affect the exit
    code."""
    checks = _doctor_checks()

    width = max(len(name) for name, _ok, _detail in checks)
    for name, ok, detail in checks:
        mark = "OK" if ok else "MISSING"
        print(f"{mark:7} {name.ljust(width)}  {detail}")

    for app, present in _doctor_node_modules_status():
        mark = "OK" if present else "MISSING"
        detail = "node_modules present" if present else "node_modules missing (bootstrap not run or failed)"
        print(f"{mark:7} {('node_modules:' + app).ljust(width)}  {detail}")

    missing = [name for name, ok, _detail in checks if not ok]
    if missing:
        print(f"missing: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    from . import workspace

    workspace.verify_app_repo_identity(args.app)
    source_repo = shared.apps_root() / args.app
    base = workspace.read_worktree_base(args.app)
    stat = shared.git(["diff", f"{base}...{shared.WORKTREE_BRANCH}", "--stat"], cwd=source_repo)
    full = shared.git(["diff", f"{base}...{shared.WORKTREE_BRANCH}"], cwd=source_repo)
    print(stat)
    print(full)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Rebase the app worktree's gb2/work branch onto the current
    default branch (the one `gb2 init`/`gb2 new` detected for this app --
    see workspace._detect_default_branch/workspace.read_worktree_base)
    -- refuses (no rebase attempted) if the worktree has any
    uncommitted change, tracked or untracked."""
    from . import workspace

    repo_dir = shared.app_workspace(args.app) / "repo"
    if not repo_dir.exists():
        return shared.err_json(f"not initialized: run `gb2 init {args.app}` first", 1)
    workspace.verify_app_repo_identity(args.app)
    status = shared.git(["status", "--porcelain"], cwd=repo_dir)
    if status:
        return shared.err_json("worktree_dirty: commit or stash before sync", 1)
    shared.git(["rebase", workspace.read_worktree_base(args.app)], cwd=repo_dir)
    head = shared.git(["rev-parse", "HEAD"], cwd=repo_dir)
    print(json.dumps({"ok": True, "app": args.app, "head": head}, ensure_ascii=False))
    return 0
