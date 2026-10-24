"""Unit tests for engine.cli: every subcommand exercised in-process via
engine.cli.main(argv) -> int (no subprocess -- faster, and keeps stdout
capturable with pytest's `capsys`).

The `run`/`close`/`approve` tests reuse the dependency-task sample app
and scripted builder/reviewer (tests/acceptance/test_15_dependency_
external.py, tests/acceptance/_owner_tasks.py) because that scenario is
the one that naturally splits into two phases at a "needs_owner" stop
(an install request) -- exactly where `run` (builder phase) and `run
--after-install` (reviewer phase) are meant to be invoked as two
separate operator commands. engine.owner.approve_pending now exists
(Task 2) and is used directly -- no stub module.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from engine import cli
from engine.config import WorkerConfig
from engine.ledger import Event, Ledger
from engine.processor import submit
from tests.acceptance._owner_tasks import (
    sample_workspace, station_config, status_stub, status_url, with_status_url,
    with_workers, write_script,
)
from tests.acceptance.test_15_dependency_external import (
    _DEP_BUILDER_SCRIPT, _INSTALL_REQUEST, _JOB_NAME, _REVIEWER_SCRIPT, _TASK,
)
from tests.conftest import Workspace, make_repo


def _dep_config(workspace: Workspace, status_server) -> Workspace:
    ws = sample_workspace(workspace, variant=3)
    base_config = with_status_url(station_config(ws), status_url(status_server))
    config = with_workers(base_config, {
        "builder": WorkerConfig("scripted"), "reviewer": WorkerConfig("scripted"),
    })
    return Workspace(ws.root, config, ws.ledger, ws.alpha, ws.beta)


def test_init_creates_ledger_and_is_idempotent(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps({
        "projects": {"alpha": {"repo": str(repo)}},
        "station_owners": {},
    }), encoding="utf-8")

    assert cli.main(["init", "--config", str(config_path)]) == 0
    from engine.config import EngineConfig
    config = EngineConfig.from_json(config_path)
    ledger = Ledger.open(config.ledger_path)
    try:
        assert ledger.project("alpha").state == "CLOSED"
    finally:
        ledger.close()

    # idempotent: a second run on an already-initialized workspace must
    # not raise (Ledger.create alone would refuse an existing file).
    assert cli.main(["init", "--config", str(config_path)]) == 0


def test_status_reports_queued_and_unknown_requests(
    workspace: Workspace, capsys,
) -> None:
    from engine.config import write_json
    from engine.steps.open_job import open_job

    config = write_json(workspace.config, workspace.root / "engine.json")
    attempt_id = open_job(config, workspace.ledger, "alpha", "status-check",
                          "full", ("builder",))
    rid_queued = submit(workspace.ledger, "alpha", attempt_id, "commit",
                        {"message": "x"}, "owner")
    rid_unknown = submit(workspace.ledger, "alpha", attempt_id, "report",
                         {"summary": "y"}, "owner")
    # Force one request to 'unknown' directly (the same event
    # request.status would append) -- exercising the processor's own
    # path to 'unknown' is covered elsewhere; this test is only about
    # cmd_status surfacing it.
    workspace.ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="test", attempt_id=attempt_id,
        payload={"request_id": rid_unknown, "status": "unknown",
                 "result": {"reason": "test"}},
    ))

    rc = cli.main(["status", "--config", str(config.config_path), "alpha"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "OPEN"
    assert out["current_attempt_id"] == attempt_id
    assert {r["request_id"] for r in out["queued_requests"]} == {rid_queued}
    assert {r["request_id"] for r in out["unknown_requests"]} == {rid_unknown}
    assert len(out["last_events"]) <= 10
    assert all({"seq", "kind"} == set(e) for e in out["last_events"])


def test_retry_unknown_retries_every_unknown_request(
    workspace: Workspace, capsys,
) -> None:
    from engine.config import write_json
    from engine.steps.open_job import open_job

    config = write_json(workspace.config, workspace.root / "engine.json")
    attempt_id = open_job(config, workspace.ledger, "alpha", "retry-check",
                          "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "report",
                {"summary": "y"}, "owner")
    workspace.ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="test", attempt_id=attempt_id,
        payload={"request_id": rid, "status": "unknown", "result": {"reason": "t"}},
    ))
    assert workspace.ledger.request(rid).status == "unknown"

    rc = cli.main(["retry-unknown", "--config", str(config.config_path), "alpha"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["retried"] == [rid]
    assert workspace.ledger.request(rid).status == "running"


def test_run_requires_task_unless_after_install(
    workspace: Workspace, capsys,
) -> None:
    from engine.config import write_json
    from engine.steps.open_job import open_job

    config = write_json(workspace.config, workspace.root / "engine.json")
    attempt_id = open_job(config, workspace.ledger, "alpha", "needs-task",
                          "full", ("builder",))
    rc = cli.main(["run", "--config", str(config.config_path), "alpha", attempt_id])
    assert rc == 1
    err = json.loads(capsys.readouterr().err)
    assert "task" in err["error"]


def test_run_then_approve_then_run_after_install_end_to_end(
    workspace: Workspace, tmp_path: Path, capsys, monkeypatch,
) -> None:
    """The install-task scenario (tests/acceptance/test_15_dependency_
    external.py) end to end, entirely through engine.cli.main(): `open`
    opens the attempt, `run` drives the builder through run_station+
    merge and stops needs_owner (exit 2) because the builder script asks
    for an install, `approve` (stubbed engine.owner) applies it, and
    `run --after-install` finishes review/bench/commit/report/close."""
    # real engine.owner (Task 2) is used; the stub is kept only for reference
    with status_stub() as server:
        ws = _dep_config(workspace, server)
        config_path = str(ws.config.config_path)

        rc_open = cli.main(["open", "--config", config_path, "alpha", _JOB_NAME])
        assert rc_open == 0
        attempt_id = json.loads(capsys.readouterr().out)["attempt_id"]

        builder_script = write_script(tmp_path, "cli-dep-builder.json", _DEP_BUILDER_SCRIPT)
        rc_run1 = cli.main([
            "run", "--config", config_path, "alpha", attempt_id,
            "--task", _TASK, "--script", str(builder_script),
        ])
        out1 = json.loads(capsys.readouterr().out)
        assert rc_run1 == 2, out1
        assert out1["status"] == "needs_owner"
        assert out1["pending_requests"] == [_INSTALL_REQUEST]
        assert out1["completed"] == ["run_station", "merge"]

        request_json = tmp_path / "install-request.json"
        request_json.write_text(json.dumps(_INSTALL_REQUEST), encoding="utf-8")
        rc_approve = cli.main([
            "approve", "--config", config_path, "alpha", attempt_id,
            "--request-json", str(request_json), "--yes",
        ])
        approve_captured = capsys.readouterr()
        assert rc_approve == 0, approve_captured
        approve_out = json.loads(approve_captured.out)
        assert ws.ledger.request(approve_out["request_id"]).status == "done"
        assert (ws.alpha / ".deps" / "vendored_pkg" / "__init__.py").exists()

        reviewer_script = write_script(tmp_path, "cli-dep-reviewer.json", _REVIEWER_SCRIPT)
        rc_run2 = cli.main([
            "run", "--config", config_path, "alpha", attempt_id,
            "--after-install", "--script", str(reviewer_script),
        ])
        out2 = json.loads(capsys.readouterr().out)
        assert rc_run2 == 0, out2
        assert out2["status"] == "done"
        assert out2["completed"] == ["review", "bench", "commit", "report", "close"]
        assert ws.ledger.project("alpha").state == "CLOSED"


def test_close_drains_close_request_to_done(
    workspace: Workspace, capsys,
) -> None:
    from engine.config import write_json
    from engine.steps.open_job import open_job

    config = write_json(workspace.config, workspace.root / "engine.json")
    attempt_id = open_job(config, workspace.ledger, "alpha", "close-check",
                          "full", ("builder",))
    rc = cli.main(["close", "--config", str(config.config_path), "alpha", attempt_id])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "done"
    assert workspace.ledger.project("alpha").state == "CLOSED"


def test_approve_refuses_without_yes_in_non_interactive_shell(
    workspace: Workspace, tmp_path: Path, capsys, monkeypatch,
) -> None:
    from engine.config import write_json
    from engine.steps.open_job import open_job

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    config = write_json(workspace.config, workspace.root / "engine.json")
    attempt_id = open_job(config, workspace.ledger, "alpha", "approve-refuse",
                          "full", ("builder",))
    request_json = tmp_path / "request.json"
    request_json.write_text(json.dumps({"kind": "report", "summary": "x"}),
                            encoding="utf-8")

    rc = cli.main([
        "approve", "--config", str(config.config_path), "alpha", attempt_id,
        "--request-json", str(request_json),
    ])
    assert rc == 4
    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    # Nothing about the request is printed on this path -- the refusal
    # is the ONLY line on stderr: a non-interactive shell with no --yes
    # must be refused before anything about the request is shown, not
    # after printing the request/validate_pending preview first.
    assert len(err_lines) == 1
    last_error = json.loads(err_lines[-1])
    assert "non-interactive" in last_error["error"]


def test_approve_interactive_prints_request_before_asking(
    workspace: Workspace, tmp_path: Path, capsys, monkeypatch,
) -> None:
    """Interactive path (a tty, no --yes): the human must see the full
    request + validate_pending verdict before the y/N prompt, and
    confirm_source="tty" is what reaches owner.approve_pending (exercised
    indirectly here via the refusal -- "report" is not a whitelisted
    kind, so approve_pending still raises after confirming, but only
    after the interactive prompt path ran)."""
    from engine.config import write_json
    from engine.steps.open_job import open_job

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    config = write_json(workspace.config, workspace.root / "engine.json")
    attempt_id = open_job(config, workspace.ledger, "alpha", "approve-interactive",
                          "full", ("builder",))
    request = {"kind": "report", "summary": "distinctive-marker-x"}
    request_json = tmp_path / "request.json"
    request_json.write_text(json.dumps(request), encoding="utf-8")

    rc = cli.main([
        "approve", "--config", str(config.config_path), "alpha", attempt_id,
        "--request-json", str(request_json),
    ])
    captured_err = capsys.readouterr().err
    assert "distinctive-marker-x" in captured_err
    assert "validate_pending" in captured_err
    assert "kind_not_whitelisted" in captured_err
    assert rc == 1  # "report" is not in PENDING_KIND_WHITELIST


def test_approve_redacts_credential_looking_substrings_in_preview(
    workspace: Workspace, tmp_path: Path, capsys, monkeypatch,
) -> None:
    """A URL embedding a credential, and a sensitive-named field's
    value, must never reach stderr verbatim -- even though the request
    is otherwise rejected (kind not whitelisted), the preview is still
    printed before that rejection."""
    from engine.config import write_json
    from engine.steps.open_job import open_job

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    config = write_json(workspace.config, workspace.root / "engine.json")
    attempt_id = open_job(config, workspace.ledger, "alpha", "approve-redact",
                          "full", ("builder",))
    request = {
        "kind": "report",
        "summary": "fetch https://alice:CANARY-PASSWORD@example.test/pkg",
        "authToken": "CANARY-TOKEN-VALUE",
    }
    request_json = tmp_path / "request.json"
    request_json.write_text(json.dumps(request), encoding="utf-8")

    cli.main([
        "approve", "--config", str(config.config_path), "alpha", attempt_id,
        "--request-json", str(request_json),
    ])
    captured_err = capsys.readouterr().err
    assert "CANARY-PASSWORD" not in captured_err
    assert "CANARY-TOKEN-VALUE" not in captured_err
    assert "https://***@example.test/pkg" in captured_err
    assert '"authToken": "***"' in captured_err


def test_approve_reads_request_json_bounded_before_parsing(
    workspace: Workspace, tmp_path: Path, capsys,
) -> None:
    from engine.config import write_json
    from engine.steps.open_job import open_job

    config = write_json(workspace.config, workspace.root / "engine.json")
    attempt_id = open_job(config, workspace.ledger, "alpha", "approve-oversized",
                          "full", ("builder",))
    request_json = tmp_path / "request.json"
    # Not valid JSON at all -- oversized-refusal must win before any
    # attempt to parse it, so this never surfaces "invalid_request_json".
    request_json.write_text("{" + ("x" * (64 * 1024 + 10)), encoding="utf-8")

    rc = cli.main([
        "approve", "--config", str(config.config_path), "alpha", attempt_id,
        "--request-json", str(request_json), "--yes",
    ])
    assert rc == 1
    err = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert err["error"] == "request_json_too_large"


def test_build_publisher_defaults_to_fake(workspace: Workspace) -> None:
    from engine.cli import _build_publisher
    from engine.fakes.publisher import FakePublisher
    publisher = _build_publisher(workspace.config)
    assert isinstance(publisher, FakePublisher)


def test_build_publisher_shell_from_config(workspace: Workspace) -> None:
    from dataclasses import replace

    from engine.cli import _build_publisher
    from engine.publishers import ShellPublisher

    config = replace(
        workspace.config, publisher_kind="shell",
        publisher_argv=("/opt/apps/_shared/deploy.sh", "{target}"),
        publisher_dry_run_argv=("/opt/apps/_shared/deploy.sh", "{target}", "--check"),
        publisher_timeout_sec=120, publisher_dry_run=False,
    )
    publisher = _build_publisher(config)
    assert isinstance(publisher, ShellPublisher)
    assert publisher.argv_template == ("/opt/apps/_shared/deploy.sh", "{target}")
    assert publisher.dry_run_argv_template == (
        "/opt/apps/_shared/deploy.sh", "{target}", "--check")
    assert publisher.timeout_sec == 120
    assert publisher.dry_run is False
    assert publisher.receipts_dir == workspace.config.home / "publish"
