"""Constants and helpers shared by every tools/gb2 command module:
workspace paths, app-name validation, the run log gb2 keeps for itself
(never part of the engine's own ledger), and the full-orbit
summarize/report plumbing that both `gb2new`'s `cmd_new` and
`runner`'s `cmd_change`/`cmd_fix`/`cmd_approve_design`/
`cmd_approve_install`/`cmd_continue` all need identically."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from engine.config import EngineConfig, WorkerConfig
from engine.ledger import Ledger
from engine.processor import Fakes
from engine.cli import _build_fakes

__all__ = [
    "GB2_WORKSPACE", "APPS_DIR", "TEMPLATES_DIR", "WORKTREE_BRANCH", "WORKTREE_BASE",
    "apps_root", "deploy_script_path",
    "validate_app_name", "is_within",
    "app_workspace", "read_run_log", "append_run_log",
    "git", "err_json",
    "approval_recorded_summary",
    "exit_code_for_full_outcome", "progress_printer",
    "summarize_full_outcome", "summarize_small_outcome",
    "scripted_workers_dir", "apply_scripted_workers", "fakes_builder",
    "run_full_job_and_report", "read_requirement",
]


def _default_workspace_root() -> Path:
    return Path.home() / ".gongbang" / "workspace"


GB2_WORKSPACE = Path(os.environ.get("GB2_WORKSPACE", str(_default_workspace_root())))
APPS_DIR = GB2_WORKSPACE / "apps"
# tools/gb2lib/shared.py -> parent is tools/gb2lib, parent.parent is tools -- the
# same tools/templates/ directory `tools/gb2` itself used to resolve relative to
# its own location.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
WORKTREE_BRANCH = "gb2/work"
WORKTREE_BASE = "master"


def apps_root() -> Path:
    """The directory holding each app's own source repo -- read lazily
    (only by the subcommands that actually need it: init, diff) rather
    than resolved at import time, so importing this module, or running
    a subcommand that only touches GB2_WORKSPACE, never requires
    GB2_APPS_ROOT to be set."""
    value = os.environ.get("GB2_APPS_ROOT")
    if not value:
        raise SystemExit(
            "GB2_APPS_ROOT is not set -- export it to the directory that "
            "holds each app's own source repo before running init/diff"
        )
    return Path(value)


def deploy_script_path(apps_root_dir: Path) -> Path:
    """GB2_DEPLOY_SCRIPT if set, else "<apps_root>/_shared/deploy.sh" --
    read lazily (cmd_init only), same reasoning as apps_root()."""
    value = os.environ.get("GB2_DEPLOY_SCRIPT")
    return Path(value) if value else (apps_root_dir / "_shared" / "deploy.sh")


# A single safe path component: no "/", no leading "-" (would otherwise
# be read as an option by something downstream), not "." or "..". An
# app name is joined onto both APPS_DIR and GB2_APPS_ROOT without any
# further escaping, so accepting anything else (a "/", an absolute-
# looking value, "..") would let `gb2 init` be pointed at an arbitrary
# path outside either root -- including, in the worst case, making the
# computed workspace and source repo paths the same directory.
_APP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_app_name(app: str) -> None:
    if app in (".", ".."):
        raise ValueError(f"invalid app name: {app!r}")
    if not _APP_NAME_RE.fullmatch(app) or app.startswith("-"):
        raise ValueError(
            f"invalid app name: {app!r} (must be a single path component "
            "matching [A-Za-z0-9._-]+, not '.', '..', or start with '-')"
        )


def is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def app_workspace(app: str) -> Path:
    """GB2_WORKSPACE/apps/<app> -- raises ValueError for an app name
    that is not a single safe path component, or that would somehow
    still resolve outside APPS_DIR (defense in depth alongside
    validate_app_name's own charset check). Reads the module-level
    APPS_DIR global by name (not a copy), so a test's
    `monkeypatch.setattr(shared, "APPS_DIR", ...)` takes effect here."""
    validate_app_name(app)
    workspace = (APPS_DIR / app).resolve()
    if not is_within(workspace, APPS_DIR.resolve()):
        raise ValueError(f"app workspace escapes GB2_WORKSPACE/apps: {app!r}")
    return workspace


def _runs_log_path(app: str) -> Path:
    return app_workspace(app) / "runs.json"


def read_run_log(app: str) -> list[dict[str, object]]:
    path = _runs_log_path(app)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def append_run_log(app: str, record: dict[str, object]) -> None:
    runs = read_run_log(app)
    runs.append(record)
    _runs_log_path(app).write_text(
        json.dumps(runs[-50:], ensure_ascii=False, indent=1), encoding="utf-8",
    )


def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def err_json(message: str, code: int) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    return code


def approval_recorded_summary(app: str, job_name: str, event_kind: str) -> dict[str, object]:
    """Printed by a TTY approval command (결함 64) once the approval
    ledger event itself is appended and nothing else -- the owner's
    own TTY process is never held open past that for a `continue_full_
    job` build loop that can run for minutes: `gb2 watch --loop
    --resume` already resumes every stopped job on its own, and an
    owner who wants THIS process to keep driving it passes
    `--continue` instead (unchanged: the original one-shot behavior,
    still covered by its own tests)."""
    return {
        "app": app, "job_name": job_name, "event": event_kind,
        "next": f"gb2 continue {app} (또는 watch --loop --resume가 자동으로 이어감); "
                f"바로 이어가려면 이 명령에 --continue",
    }


def exit_code_for_full_outcome(status: str) -> int:
    if status == "done":
        return 0
    if status == "needs_owner":
        return 2
    return 3


def progress_printer(kind: str) -> None:
    print(f"[{kind}] done", file=sys.stderr)


def summarize_full_outcome(config: EngineConfig, ledger: Ledger, app: str,
                           outcome: object, wall_sec: float) -> dict[str, object]:
    """Same shape of summary as runner's JobOutcome summarize, for a
    FullOutcome (engine.full_orbit): status/reason plus, only when the
    outcome carries an attempt_id (it may be None if the job never got
    as far as opening one), the commit it left and a changed-files
    diff against that attempt's base_head. report_path is passed
    through as-is -- engine.full_orbit/engine.handoff.write_handoff
    already decided where report.md lives."""
    attempt_id = getattr(outcome, "attempt_id", None)
    commit_hash = None
    changed_files: list[str] = []
    if attempt_id is not None:
        project = config.projects[app]
        try:
            commit_hash = git(["rev-parse", "HEAD"], cwd=project.repo)
        except subprocess.CalledProcessError:
            pass
        try:
            base_head = None
            for _seq, event in ledger.events(app, "job.opened"):
                if event.payload.get("attempt_id") == attempt_id:
                    base_head = event.payload.get("base_head")
                    break
            if base_head and commit_hash and base_head != commit_hash:
                out = git(["diff", "--name-only", f"{base_head}..{commit_hash}"],
                          cwd=project.repo)
                changed_files = [line for line in out.splitlines() if line]
        except subprocess.CalledProcessError:
            pass
    return {
        "app": app, "attempt_id": attempt_id, "status": getattr(outcome, "status"),
        "reason": getattr(outcome, "reason", None), "commit": commit_hash,
        "changed_files": changed_files, "minutes": round(wall_sec / 60.0, 2),
        "report_path": getattr(outcome, "report_path", None),
    }


def summarize_small_outcome(config: EngineConfig, ledger: Ledger, app: str,
                            outcome: "object", wall_sec: float) -> dict[str, object]:
    """Same shape as summarize_full_outcome, for engine.small_orbit's
    own SmallOutcome, plus "durations_ms" (that module's own per-
    station wall-clock timing -- see its SmallOutcome docstring)."""
    attempt_id = getattr(outcome, "attempt_id", None)
    commit_hash = None
    changed_files: list[str] = []
    if attempt_id is not None:
        project = config.projects[app]
        try:
            commit_hash = git(["rev-parse", "HEAD"], cwd=project.repo)
        except subprocess.CalledProcessError:
            pass
        try:
            base_head = None
            for _seq, event in ledger.events(app, "job.opened"):
                if event.payload.get("attempt_id") == attempt_id:
                    base_head = event.payload.get("base_head")
                    break
            if base_head and commit_hash and base_head != commit_hash:
                out = git(["diff", "--name-only", f"{base_head}..{commit_hash}"],
                          cwd=project.repo)
                changed_files = [line for line in out.splitlines() if line]
        except subprocess.CalledProcessError:
            pass
    durations = {k: round(v, 1) for k, v in dict(getattr(outcome, "durations_ms", {})).items()}
    return {
        "app": app, "attempt_id": attempt_id, "status": getattr(outcome, "status"),
        "reason": getattr(outcome, "reason", None), "commit": commit_hash,
        "changed_files": changed_files, "minutes": round(wall_sec / 60.0, 2),
        "report_path": getattr(outcome, "report_path", None), "durations_ms": durations,
    }


# --------------------------------------------------------------------
# GB2_SCRIPTED_WORKERS -- a test-only seam (not a documented
# subcommand/flag) letting an acceptance test drive the whole full
# orbit through the REAL `gb2 new`/`gb2 change`/`gb2 continue`/
# `gb2 approve-design` commands with deterministic ScriptedRunner
# workers instead of a live claude/codex CLI call. Unset (the default
# for every normal invocation), none of this runs: `fakes_builder`
# returns `config` untouched and the exact `fakes_for` closure that
# was inlined before this seam existed.
# --------------------------------------------------------------------

def scripted_workers_dir() -> Path | None:
    """The directory GB2_SCRIPTED_WORKERS points at, or None if that
    env var is unset/empty -- read lazily (once per full-orbit call,
    not at import time) so nothing here changes behavior for a normal
    `gb2` invocation that never sets it."""
    value = os.environ.get("GB2_SCRIPTED_WORKERS", "").strip()
    return Path(value) if value else None


def apply_scripted_workers(config: EngineConfig,
                          workers_dir: Path) -> tuple[EngineConfig, dict[str, object]]:
    """Return (`config` with every role in `config.workers` rewritten
    to runner "scripted_<role>", {"scripted_<role>": ScriptedRunner}
    for each of those roles) so a full-orbit run drives EVERY role
    (facts/prober/planner/designer/builder/reviewer, whichever this
    config defines) from `workers_dir`/<role>.json instead of a live
    claude/codex CLI. The reviewer role's ScriptedRunner is built
    read_only=True, same as every other scripted-reviewer fixture in
    this codebase (tests/acceptance/test_t1_new_app.py etc.) -- review
    never writes files, and engine.steps.review's own
    "reviewer_not_read_only" check would fail the job otherwise.

    Raises ValueError (nothing built, nothing started) if `workers_dir`
    is missing even one role's own script file -- a half-scripted job
    must never silently fall through to a real AI call for the role
    whose file is missing."""
    from engine.workers.scripted import ScriptedRunner

    missing = sorted(role for role in config.workers if not (workers_dir / f"{role}.json").exists())
    if missing:
        raise ValueError(
            f"GB2_SCRIPTED_WORKERS={workers_dir}: missing script file(s) for role(s) "
            f"{', '.join(missing)} -- expected {workers_dir}/<role>.json for every role "
            "in config.workers"
        )
    scripted_runners: dict[str, object] = {}
    new_workers: dict[str, WorkerConfig] = {}
    for role, worker in config.workers.items():
        runner_key = f"scripted_{role}"
        new_workers[role] = replace(worker, runner=runner_key)
        scripted_runners[runner_key] = ScriptedRunner(
            workers_dir / f"{role}.json", read_only=(role == "reviewer"),
        )
    return replace(config, workers=new_workers), scripted_runners


def fakes_builder(config: EngineConfig, app: str):
    """(config, fakes_for) for a full-orbit call against `app`: by
    default exactly `(config, lambda attempt_id: _build_fakes(config,
    app, attempt_id, None))` -- the real installer/publisher plus the
    real claude/codex runners, unchanged. When GB2_SCRIPTED_WORKERS is
    set (see scripted_workers_dir/apply_scripted_workers), `config`'s
    workers are rewritten to deterministic scripted runners and the
    returned `fakes_for` keeps `_build_fakes`'s own real installer/
    publisher but merges in those scripted runners -- so a test can
    drive the real `gb2` subcommands end to end with no live AI call
    anywhere.

    Raises ValueError (caller already catches this the same way it
    catches every other config-time ValueError) if the scripted
    workers dir is missing a role's script file."""
    workers_dir = scripted_workers_dir()
    if workers_dir is None:
        return config, (lambda attempt_id: _build_fakes(config, app, attempt_id, None))

    config, scripted_runners = apply_scripted_workers(config, workers_dir)

    def fakes_for(attempt_id: str):
        base = _build_fakes(config, app, attempt_id, None)
        return Fakes(publisher=base.publisher, installer=base.installer,
                     runners={**base.runners, **scripted_runners})

    return config, fakes_for


def run_full_job_and_report(args, config: EngineConfig,
                            job_name: str, request: str, *,
                            new_app_marker: Path | None = None) -> int:
    """Shared tail of `workspace.cmd_new`/`runner.cmd_change`:
    engine.full_orbit.run_full_job against this app's workspace, then
    the same summarize/log/print/exit-code handling `runner.cmd_run`
    gives an ordinary run_station+review+bench job.

    `new_app_marker`, when `cmd_new` passes it, is only ever unlinked
    AFTER `run_full_job` has actually returned an outcome -- i.e. the
    engine's first stage (facts) genuinely started, since `open_job` is
    the very first thing `run_full_job` does. If `fakes_builder` or
    `run_full_job` itself fails/raises before that point (a scripted-
    workers config error, an exception mid-ledger-open, ...), the
    marker is left in place so the NEXT `gb2 new <app>` call resumes
    this same init instead of refusing "app_exists" -- see
    `workspace._scaffold_new_app_repo`'s own docstring."""
    from engine import full_orbit
    from engine import state as state_module

    try:
        config, fakes_for = fakes_builder(config, args.app)
    except ValueError as exc:
        return err_json(str(exc), 1)

    from engine.application.driver_session import driver_session

    ledger = Ledger.open(config.ledger_path)
    start = time.monotonic()
    # driver_session: the whole command (open -> stations -> auto_settle)
    # reads as a live driver to `gb2 watch`, not only the moments a
    # processor lock is held (engine.application.driver_session).
    try:
      with driver_session(ledger, args.app):
        try:
            outcome = full_orbit.run_full_job(
                config, ledger, fakes_for, args.app, job_name, request,
                max_fix_rounds=config.projects[args.app].max_fix_rounds,
                on_progress=progress_printer,
            )
        except state_module.ProjectOpenError as exc:
            return err_json(str(exc), 1)
        # new_app_marker is unlinked HERE -- right after run_full_job
        # has actually returned an outcome, exactly as before this
        # module gained auto_settle (see this function's own
        # docstring) -- and, critically, BEFORE auto_settle's own
        # design/install/release auto-actions run. The marker
        # (`.gongbang-new`) lives directly under the project's own
        # source repo (release.master_repo, for a `--with-release`
        # job): auto_settle's own auto-approved `advance_master` step
        # requires that repo be completely clean (engine.application.
        # steps.publish's own "master_not_clean" check, which also
        # refuses an untracked-and-not-ignored file, not only a
        # tracked one) -- leaving the marker in place until AFTER
        # auto_settle ran (as a plain reordering bug first had it)
        # made every `--with-release` auto-pilot run fail that check
        # on its own leftover marker file.
        if new_app_marker is not None:
            new_app_marker.unlink(missing_ok=True)
        from engine.application import auto_pilot

        outcome = auto_pilot.auto_settle(config, ledger, fakes_for, args.app, job_name, outcome,
                                         on_progress=progress_printer)
        wall_sec = time.monotonic() - start
        summary = summarize_full_outcome(config, ledger, args.app, outcome, wall_sec)
    finally:
        ledger.close()

    append_run_log(args.app, {"job_name": job_name, **summary})
    print(json.dumps(summary, ensure_ascii=False))
    return exit_code_for_full_outcome(str(summary["status"]))


def read_requirement(args) -> str | None:
    if args.task_file:
        return Path(args.task_file).read_text(encoding="utf-8")
    return args.task
