"""Operator command line for the gongbang v2 engine.

    python -m engine.cli <subcommand> --config <path/to/config.json>

Every subcommand loads EngineConfig.from_json(config) and then calls
existing public engine functions (engine.application.steps.open_job.open_job,
engine.application.processor.submit/retry_unknown, engine.application.task_pipeline.continue_job,
engine.application.owner_approval.approve_pending once Task 2 lands). Nothing here introduces
a new state transition or bypasses processor.submit -- this module is a
thin, scriptable front door for a human operator, not a new code path.

Errors are printed as one JSON line {"error": "..."} on stderr; the
process exits non-zero. No subcommand ever prints a secret (secrets_dir
contents, env values) -- only ids, statuses and reasons already meant to
be human-visible.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from engine.domain import ids
from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.infrastructure.fake_publisher import FakePublisher
from engine.infrastructure.db.ledger import Ledger, LedgerError
from engine.infrastructure.db.ledger import backup as ledger_backup
from engine.application.processor import Fakes, Processor, retry_unknown, submit
from engine.application.steps.open_job import open_job


def _err(message: str, code: int = 1) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    return code


def _job_name_for_attempt(ledger: Ledger, project: str, attempt_id: str) -> str:
    """job_name of the job.opened event that created this attempt_id --
    engine.application.steps.open_job keeps the equivalent lookup private (leading
    underscore); re-reading it here via the public Ledger.events() API
    is a few lines and avoids depending on another module's internals."""
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            return str(event.payload["job_name"])
    raise LedgerError(f"no job.opened event for attempt_id {attempt_id!r}")


def _other_project_repos(config: EngineConfig, project: str) -> tuple[Path, ...]:
    """Every OTHER project's repo path -- same helper engine.application.steps.
    bench_run/station/review each keep (StepContext-bound there; this
    is the same logic against a plain EngineConfig + project name,
    since _build_fakes has no StepContext)."""
    return tuple(p.repo for name, p in config.projects.items() if name != project)


def _installer_hidden_paths(config: EngineConfig, project: str) -> tuple[Path, ...]:
    """Absolute host paths install_cmd must never see, for THIS
    project's RealInstaller -- the same fixed set every worker/bench/
    reviewer sandbox already hides (engine.application.steps.suite_run/station/
    review's own `hidden` construction): the engine's own home,
    secrets_dir and ledger directory, every OTHER project's repo, and
    this project's own `hidden_paths` (e.g. gb2's real source repo,
    hidden from the worktree's own sandboxes -- see tools/gb2's
    `_setup_app_workspace`). Safe to include unconditionally here:
    RealInstaller's `repo` below is project_config.repo (the worktree),
    never the real source repo this project's own `hidden_paths` names
    -- the two are different paths, so there is no writable/hidden
    conflict (contrast engine.infrastructure.bootstrap.ensure_installed, which
    installs INTO the source repo itself and must NOT pass that
    project's own hidden_paths for exactly that reason)."""
    project_config = config.projects[project]
    return ((config.home, config.secrets_dir, config.ledger_path.parent)
            + _other_project_repos(config, project)
            + project_config.hidden_paths)


def _build_fakes(config: EngineConfig, project: str, attempt_id: str,
                 script_path: str | None, script_read_only: bool = False) -> Fakes:
    """Fakes wired the same way the live acceptance tests build them
    (tests/acceptance/test_13_copy_change.py etc.): the real installer
    against the project's own repo/install_cmd, and the real claude/codex
    runners (constructing a runner never touches the CLI binary -- only
    spawning does, so this is safe even where those CLIs aren't
    installed). `script_path`, present only for `run --script` (used by
    tests/unit/test_cli.py and an operator who wants a dry run), also
    registers a ScriptedRunner under the "scripted" runner key;
    `script_read_only` matches `run --after-install` (the stage list from
    there on is reviewer-only, same as the live tests' own read_only=True
    reviewer ScriptedRunner -- a non-read-only one fails review's own
    "reviewer_not_read_only" check)."""
    from engine.infrastructure.installers import RealInstaller
    from engine.infrastructure.workers.claude_cli import ClaudeRunner
    from engine.infrastructure.workers.codex_cli import CodexRunner

    project_config = config.projects[project]
    installer = RealInstaller(
        repo=project_config.repo, install_cmd=project_config.install_cmd,
        marker_dir=config.home / "jobs" / ids.attempt_dirname(attempt_id) / "installs",
        sandbox_mode=config.sandbox,
        hidden_paths=_installer_hidden_paths(config, project),
    )
    runners: dict[str, object] = {"claude": ClaudeRunner(), "codex": CodexRunner()}
    if script_path is not None:
        from engine.infrastructure.workers.scripted import ScriptedRunner
        runners["scripted"] = ScriptedRunner(Path(script_path), read_only=script_read_only)
    return Fakes(publisher=_build_publisher(config), installer=installer, runners=runners)


def _build_publisher(config: EngineConfig) -> object:
    """config.publisher_kind picks FakePublisher (default, in-memory,
    used by every test) or engine.infrastructure.publishers.ShellPublisher, which
    drives a real deploy command. ShellPublisher is never run inside
    the OS sandbox and its receipts live under config.home/"publish"."""
    if config.publisher_kind == "shell":
        from engine.infrastructure.publishers import ShellPublisher
        return ShellPublisher(
            argv_template=config.publisher_argv,
            dry_run_argv_template=config.publisher_dry_run_argv,
            receipts_dir=config.home / "publish",
            timeout_sec=config.publisher_timeout_sec,
            dry_run=config.publisher_dry_run,
        )
    return FakePublisher()


def _outcome_dict(outcome: orbit.JobOutcome) -> dict[str, object]:
    return {
        "attempt_id": outcome.attempt_id,
        "status": outcome.status,
        "failed_request": outcome.failed_request,
        "reason": outcome.reason,
        "completed": list(outcome.completed),
        "pending_requests": list(outcome.pending_requests),
    }


def cmd_init(config: EngineConfig, args: argparse.Namespace) -> int:
    """Create the ledger if missing, then register every configured
    project (Ledger.register_project is itself idempotent) -- safe to run
    again on an already-initialized workspace."""
    ledger = Ledger.open(config.ledger_path) if config.ledger_path.exists() \
        else Ledger.create(config.ledger_path)
    try:
        for name in config.projects:
            ledger.register_project(name)
    finally:
        ledger.close()
    print(json.dumps({"ok": True, "projects": sorted(config.projects)},
                     ensure_ascii=False))
    return 0


def cmd_open(config: EngineConfig, args: argparse.Namespace) -> int:
    ledger = Ledger.open(config.ledger_path)
    try:
        roles = tuple(r for r in args.roles.split(",") if r)
        attempt_id = open_job(config, ledger, args.project, args.job_name,
                              "full", roles)
    finally:
        ledger.close()
    print(json.dumps({"attempt_id": attempt_id}, ensure_ascii=False))
    return 0


def cmd_run(config: EngineConfig, args: argparse.Namespace) -> int:
    """--after-install: continue_job(stages_after_install(...)) -- the
    attempt is already open (via a prior `open`) and an install request
    an earlier `run` surfaced has already been submitted/applied, so this
    resumes from review (both review and bench evidence are invalidated
    by an install, per orbit.stages_after_install's own docstring).
    Otherwise: continue_job(default_stages(...)) runs the full
    run_station -> merge -> review -> bench -> commit -> report -> close
    sequence on the given attempt_id. Neither path calls orbit.run_job --
    that function also calls open_job itself, which would try to open a
    second, fresh attempt instead of driving the one `open` already
    created; continue_job (which never calls open_job) is the correct
    function once an attempt_id already exists."""
    ledger = Ledger.open(config.ledger_path)
    try:
        job_name = _job_name_for_attempt(ledger, args.project, args.attempt_id)
        fakes = _build_fakes(config, args.project, args.attempt_id, args.script,
                             script_read_only=args.after_install)
        if args.after_install:
            from engine.application.repair import original_task
            stages = orbit.stages_after_install(
                config, args.attempt_id, args.builder_role, args.reviewer_role,
                job_name, original_task(ledger, args.project, args.attempt_id),
            )
        else:
            if not args.task:
                return _err("--task is required unless --after-install")
            stages = orbit.default_stages(
                config, args.attempt_id, args.builder_role, args.reviewer_role,
                args.task, job_name,
            )
        outcome = orbit.continue_job(config, ledger, fakes, args.project,
                                     args.attempt_id, stages)
    finally:
        ledger.close()
    print(json.dumps(_outcome_dict(outcome), ensure_ascii=False))
    if outcome.status == "done":
        return 0
    if outcome.status == "needs_owner":
        return 2
    return 3  # failed / pending / unknown


def cmd_status(config: EngineConfig, args: argparse.Namespace) -> int:
    ledger = Ledger.open(config.ledger_path)
    try:
        project_row = ledger.project(args.project)
        queued = ledger.queued_requests(args.project)
        unknown = ledger.unknown_requests(args.project)
        # Ledger.events() does not expose the `ts` column through its
        # public return type (tuple[int, Event] -- Event itself carries
        # no timestamp), so "last 10 events" are identified by seq
        # (monotonic, same ordering a ts would give) instead of ts.
        last_events = ledger.events(args.project)[-10:]
    finally:
        ledger.close()
    out = {
        "project": project_row.project,
        "state": project_row.state,
        "current_attempt_id": project_row.current_attempt_id,
        "generation": project_row.generation,
        "queued_requests": [
            {"request_id": r.request_id, "kind": r.kind, "status": r.status}
            for r in queued
        ],
        "unknown_requests": [
            {"request_id": r.request_id, "kind": r.kind} for r in unknown
        ],
        "last_events": [{"seq": seq, "kind": event.kind} for seq, event in last_events],
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_retry_unknown(config: EngineConfig, args: argparse.Namespace) -> int:
    ledger = Ledger.open(config.ledger_path)
    try:
        retried = []
        for row in ledger.unknown_requests(args.project):
            retry_unknown(ledger, row.request_id, "owner")
            retried.append(row.request_id)
    finally:
        ledger.close()
    print(json.dumps({"retried": retried}, ensure_ascii=False))
    return 0


_MAX_REQUEST_JSON_BYTES = 64 * 1024

# "scheme://user:pass@host" -> "scheme://***@host" wherever it appears
# in a printed string value (e.g. a dependency URL embedding a
# credential).
_CREDENTIAL_URL_RE = re.compile(r"(://)[^/\s@]+:[^/\s@]+@")
# A dict KEY matching this (case-insensitive) has its value redacted
# wholesale when printed, regardless of the value's own shape.
_SENSITIVE_KEY_RE = re.compile(r"token|secret|password|key", re.IGNORECASE)


def _redact_for_display(value: object) -> object:
    """A best-effort, display-only redaction of credential-looking
    content before it is printed for a human to read -- never used for
    anything validate_pending/approve_pending themselves decide on.
    Recurses into dicts/lists; a dict key matching _SENSITIVE_KEY_RE
    redacts its whole value (regardless of type); every string value
    (including dict keys that did NOT match) still gets
    _CREDENTIAL_URL_RE applied."""
    if isinstance(value, dict):
        out: dict[object, object] = {}
        for key, sub in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                out[key] = "***"
            else:
                out[key] = _redact_for_display(sub)
        return out
    if isinstance(value, list):
        return [_redact_for_display(v) for v in value]
    if isinstance(value, str):
        return _CREDENTIAL_URL_RE.sub(r"\1***@", value)
    return value


def cmd_approve(config: EngineConfig, args: argparse.Namespace) -> int:
    # Imported lazily: engine.application.owner_approval is Task 2's module, written in
    # parallel with this one -- importing it only inside this handler
    # means engine.interface.cli still imports cleanly (and every other subcommand
    # still works) even before that module exists.
    import engine.application.owner_approval as owner_module

    # Read at most _MAX_REQUEST_JSON_BYTES + 1 bytes -- the file is
    # never fully loaded into memory just to then discover it is over
    # the limit (a prior version read the whole file before checking
    # its length).
    try:
        with open(args.request_json, "rb") as handle:
            raw = handle.read(_MAX_REQUEST_JSON_BYTES + 1)
    except OSError as exc:
        return _err(f"request_json_unreadable: {exc}")
    if len(raw) > _MAX_REQUEST_JSON_BYTES:
        return _err("request_json_too_large")

    # Decide confirm_source BEFORE parsing or printing anything: a
    # non-interactive shell with no --yes is refused outright here,
    # printing NOTHING of the request -- not even a redacted preview,
    # and not the "invalid_request_json" detail if the file happens to
    # be malformed JSON too (refusal takes priority on this path).
    confirm = bool(args.yes)
    confirm_source = "--yes"
    if not confirm:
        if not sys.stdin.isatty():
            return _err("refused: non-interactive shell and no --yes", code=4)
        confirm_source = "tty"

    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _err(f"invalid_request_json: {exc}")

    # Shown to the human BEFORE asking (and even with --yes, so a
    # non-interactive approval still leaves a visible record of exactly
    # what was approved) -- never approve-by-trust. validate_pending's
    # own size cap (16 KB) keeps this print small; _redact_for_display
    # masks credential-looking substrings and sensitive-named fields
    # before anything reaches the terminal.
    ok, reason = owner_module.validate_pending(config, args.project, request)
    print(json.dumps({
        "request": _redact_for_display(request),
        "validate_pending": {"ok": ok, "reason": reason},
    }, ensure_ascii=False, sort_keys=True, indent=1), file=sys.stderr)

    if confirm_source == "tty":
        answer = input(f"approve {args.project} {args.attempt_id}? [y/N] ")
        confirm = answer.strip().lower() == "y"

    ledger = Ledger.open(config.ledger_path)
    try:
        try:
            request_id = owner_module.approve_pending(
                config, ledger, args.project, args.attempt_id, request,
                confirm=confirm, confirm_source=confirm_source,
            )
        except ValueError as exc:
            return _err(str(exc))
        # An approved request is only queued by approve_pending; drain the
        # processor here so the approval actually takes effect (bounded,
        # same shape as cmd_close).
        status = _drain(config, ledger, args.project, args.attempt_id, request_id)
    finally:
        ledger.close()
    print(json.dumps({"request_id": request_id, "status": status}, ensure_ascii=False))
    return 0 if status == "done" else 3


def _drain(config: EngineConfig, ledger: Ledger, project: str, attempt_id: str,
           rid: str, max_rounds: int = 50) -> str:
    """Call Processor.run_once(project) repeatedly, which each time
    processes EVERY queued request of this project (not only `rid`) one
    at a time until none are left queued/running -- `rid` is only the
    request this function waits on to decide when to stop, not the sole
    thing run_once touches. Gives up after max_rounds if `rid` is still
    queued/running (another processor holding the project lock, etc.)."""
    fakes = _build_fakes(config, project, attempt_id, None)
    proc = Processor(config, ledger, fakes)
    proc.register_defaults()
    status = ledger.request(rid).status
    while status not in ("done", "failed", "unknown") and max_rounds > 0:
        proc.run_once(project)
        status = ledger.request(rid).status
        max_rounds -= 1
    return status


def cmd_close(config: EngineConfig, args: argparse.Namespace) -> int:
    """Submit a close request and run_once() until it leaves
    queued/running (bounded: a close's own step list is finite, so a
    request still queued/running after that many drains means something
    else is wrong -- e.g. another processor instance holding the lock --
    and this should stop rather than loop forever)."""
    ledger = Ledger.open(config.ledger_path)
    try:
        rid = submit(ledger, args.project, args.attempt_id, "close", {}, "owner")
        status = _drain(config, ledger, args.project, args.attempt_id, rid)
    finally:
        ledger.close()
    print(json.dumps({"request_id": rid, "status": status}, ensure_ascii=False))
    return 0 if status == "done" else 3


def cmd_backup(config: EngineConfig, args: argparse.Namespace) -> int:
    """Back up the ledger (engine.infrastructure.db.ledger.backup -- SQLite's online
    backup API, verified by PRAGMA integrity_check on the copy) into
    --dest (default: config.home / "backups"), then prune that
    directory to the newest --keep files. Does not touch the live
    ledger beyond opening a read connection to it."""
    dest_dir = Path(args.dest) if args.dest else (config.home / "backups")
    path = ledger_backup(config.ledger_path, dest_dir, keep=args.keep)
    print(json.dumps({"backup_path": str(path)}, ensure_ascii=False))
    return 0


_HANDLERS = {
    "init": cmd_init, "open": cmd_open, "run": cmd_run, "status": cmd_status,
    "retry-unknown": cmd_retry_unknown, "approve": cmd_approve, "close": cmd_close,
    "backup": cmd_backup,
}


def _build_parser() -> argparse.ArgumentParser:
    # --config is required on every subcommand, but the usage this module
    # documents is "<subcommand> --config <path>" -- --config AFTER the
    # subcommand name, not before it. A plain top-level `add_argument`
    # only accepts it BEFORE the subcommand token, so --config is instead
    # added to a shared `parents=[common]` parser every subparser below
    # includes, which is what lets argparse accept it in either position
    # after the subcommand word.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", required=True, help="path to engine config JSON")

    parser = argparse.ArgumentParser(prog="python -m engine.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", parents=[common])

    p_open = sub.add_parser("open", parents=[common])
    p_open.add_argument("project")
    p_open.add_argument("job_name")
    p_open.add_argument("--roles", default="builder,reviewer",
                        help="comma-separated worker roles (default: builder,reviewer)")

    p_run = sub.add_parser("run", parents=[common])
    p_run.add_argument("project")
    p_run.add_argument("attempt_id")
    p_run.add_argument("--builder-role", default="builder")
    p_run.add_argument("--reviewer-role", default="reviewer")
    p_run.add_argument("--after-install", action="store_true")
    p_run.add_argument("--task", default=None,
                       help="required unless --after-install")
    p_run.add_argument("--script", default=None,
                       help="test-only: path to a ScriptedRunner script "
                            "registered under the 'scripted' runner key")

    p_status = sub.add_parser("status", parents=[common])
    p_status.add_argument("project")

    p_retry = sub.add_parser("retry-unknown", parents=[common])
    p_retry.add_argument("project")

    p_approve = sub.add_parser("approve", parents=[common])
    p_approve.add_argument("project")
    p_approve.add_argument("attempt_id")
    p_approve.add_argument("--request-json", required=True)
    p_approve.add_argument("--yes", action="store_true")

    p_close = sub.add_parser("close", parents=[common])
    p_close.add_argument("project")
    p_close.add_argument("attempt_id")

    p_backup = sub.add_parser("backup", parents=[common])
    p_backup.add_argument("--dest", default=None,
                          help="backup directory (default: <home>/backups)")
    p_backup.add_argument("--keep", type=int, default=10,
                          help="newest backups to keep in --dest (default: 10)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = EngineConfig.from_json(Path(args.config))
    except (ValueError, OSError) as exc:
        return _err(f"config_error: {exc}")
    try:
        return _HANDLERS[args.command](config, args)
    except LedgerError as exc:
        return _err(f"ledger_error: {exc}")
    except ValueError as exc:
        return _err(f"value_error: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface as JSON, not a traceback
        return _err(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
