"""`gb2 approve-design|approve-install|approve-plan-edit --office-token
<path>`: the AI-office approval entry point (E1) -- a signed document
read from a FILE takes the place of the TTY "승인 <job>" line, so the
office web app's own [approve] button can drive these commands
without ever posing as a TTY. tests/unit/test_gb2_new.py's own TTY-
path tests for these same three commands are untouched by this
feature and must keep passing exactly as before.

A fresh `gb2` module instance per test (same reasoning as test_gb2_new
.py's own `gb2` fixture: each test here patches module state that must
not leak across tests)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

from engine.application import office_approval
from engine.application.job_state import JobPosition, record_position
from engine.config import EngineConfig, write_json
from engine.ledger import Ledger
from engine.steps.open_job import open_job

_GB2_PATH = Path(__file__).resolve().parents[2] / "tools" / "gb2"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
    "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
}


def _rgit(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=_GIT_ENV).stdout.strip()


def _load_gb2() -> ModuleType:
    loader = SourceFileLoader("gb2_office_under_test", str(_GB2_PATH))
    spec = importlib.util.spec_from_loader("gb2_office_under_test", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture()
def gb2() -> ModuleType:
    return _load_gb2()


def _init_demo_app(gb2_module: ModuleType, tmp_path: Path,
                   monkeypatch: pytest.MonkeyPatch) -> Path:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2_module.shared, "APPS_DIR", apps_dir)
    source_root = tmp_path / "source"
    monkeypatch.setenv("GB2_APPS_ROOT", str(source_root))
    source_repo = source_root / "demo"
    source_repo.mkdir(parents=True)
    _rgit(source_repo, "init", "-q", "-b", "master")
    (source_repo / "package.json").write_text("{}\n", encoding="utf-8")
    _rgit(source_repo, "add", "-A")
    _rgit(source_repo, "commit", "-q", "-m", "init")

    code = gb2_module.main(["init", "demo"])
    assert code == 0
    return apps_dir


def _set_office_key(apps_dir: Path) -> tuple[EngineConfig, Path]:
    """Writes the office-approval key at its one, fixed, non-
    configurable location
    (engine.application.office_approval.resolve_key_path): `secrets_dir
    / "office-approval.key"`. Nothing in config.json itself changes,
    so no write_json is needed -- the real `gb2.main([...])` call
    under test already reads the SAME config.secrets_dir this writes
    into."""
    config_path = apps_dir / "demo" / "config.json"
    config = EngineConfig.from_json(config_path)
    key_path = config.secrets_dir / office_approval.KEY_FILENAME
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(b"office-secret-for-tests")
    key_path.chmod(0o600)
    return config, config_path


def _open_attempt(config: EngineConfig, job_name: str) -> str:
    ledger = Ledger.open(config.ledger_path)
    try:
        return open_job(config, ledger, "demo", job_name, "full", ("builder",))
    finally:
        ledger.close()


def _write_token(tmp_path: Path, token: dict, *, name: str = "token.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(token), encoding="utf-8")
    return str(path)


def _stop_design_approval_pending(config: EngineConfig, job_name: str,
                                  attempt_id: str) -> None:
    """a design office token only verifies while this
    job is genuinely stopped at STAGE_DESIGN_APPROVAL with a reviewed
    plan -- see engine.application.office_approval._design_actually_
    pending's own docstring."""
    from engine.application.job_state import STAGE_DESIGN_APPROVAL
    from tests.conftest import seed_reviewed_manifest

    ledger = Ledger.open(config.ledger_path)
    try:
        seed_reviewed_manifest(ledger, "demo", attempt_id)
        record_position(ledger, "demo", JobPosition(
            job=job_name, stage=STAGE_DESIGN_APPROVAL, attempt_id=attempt_id,
            stop_reason="design_approval",
        ))
    finally:
        ledger.close()


def _install_fake_full_orbit(monkeypatch: pytest.MonkeyPatch) -> None:
    """approve-design/approve-install without --continue never reaches
    engine.full_orbit.continue_full_job at all (결함 64 default) -- no
    fake needed for the tests in this file, which never pass
    --continue. Kept as a hook point in case a later test here does."""
    del monkeypatch


# ---------------------------------------------------------------------------
# approve-design --office-token
# ---------------------------------------------------------------------------

def test_approve_design_office_token_succeeds_without_a_tty(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apps_dir = _init_demo_app(gb2, tmp_path, monkeypatch)
    config, _config_path = _set_office_key(apps_dir)
    job_name = "gb2-office-design-1"
    attempt_id = _open_attempt(config, job_name)
    _stop_design_approval_pending(config, job_name, attempt_id)

    ledger = Ledger.open(config.ledger_path)
    try:
        token = office_approval.issue_office_approval_token(
            config, ledger, "demo", job_name, attempt_id, "design",
        )
    finally:
        ledger.close()
    token_path = _write_token(tmp_path, token)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    capsys.readouterr()

    code = gb2.main(["approve-design", "demo", "--office-token", token_path])

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["job_name"] == job_name

    ledger = Ledger.open(config.ledger_path)
    try:
        approved = [
            event for _seq, event in ledger.events("demo", "design.approved")
            if event.payload.get("job") == job_name
        ]
    finally:
        ledger.close()
    assert len(approved) == 1
    assert approved[0].payload.get("confirm_source") == "office"
    # the signed target_version is stored
    # verbatim on the approval event, for pipeline_resume's own
    # design_approval_still_matches to compare against later.
    assert approved[0].payload.get("target_version") == token["target_version"]


def test_approve_design_office_token_refuses_forged_signature(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = _init_demo_app(gb2, tmp_path, monkeypatch)
    config, _config_path = _set_office_key(apps_dir)
    job_name = "gb2-office-design-2"
    attempt_id = _open_attempt(config, job_name)
    _stop_design_approval_pending(config, job_name, attempt_id)

    ledger = Ledger.open(config.ledger_path)
    try:
        token = office_approval.issue_office_approval_token(
            config, ledger, "demo", job_name, attempt_id, "design",
        )
    finally:
        ledger.close()
    forged = {**token, "sig": "0" * len(token["sig"])}
    token_path = _write_token(tmp_path, forged)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    code = gb2.main(["approve-design", "demo", "--office-token", token_path])

    assert code == 1
    ledger = Ledger.open(config.ledger_path)
    try:
        approved = [
            event for _seq, event in ledger.events("demo", "design.approved")
            if event.payload.get("job") == job_name
        ]
    finally:
        ledger.close()
    assert approved == []


def test_approve_design_without_office_token_still_requires_a_tty(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing TTY requirement is unaffected by this feature when
    --office-token is simply not given."""
    _init_demo_app(gb2, tmp_path, monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    code = gb2.main(["approve-design", "demo"])

    assert code == 4


# ---------------------------------------------------------------------------
# approve-install --office-token
# ---------------------------------------------------------------------------

_INSTALL_REQUEST = {
    "kind": "install", "manifest": "package.json",
    "change": {"dependencies": {"left-pad": "^1.0.0"}},
}


def _make_install_harmless(config: EngineConfig, config_path: Path) -> EngineConfig:
    new_config = replace(config, projects={
        **config.projects,
        "demo": replace(config.projects["demo"], install_cmd=(sys.executable, "-c", "pass")),
    })
    write_json(new_config, config_path)
    return EngineConfig.from_json(config_path)


def test_approve_install_office_token_succeeds_without_a_tty(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apps_dir = _init_demo_app(gb2, tmp_path, monkeypatch)
    config, config_path = _set_office_key(apps_dir)
    config = _make_install_harmless(config, config_path)
    job_name = "gb2-office-install-1"
    attempt_id = _open_attempt(config, job_name)

    ledger = Ledger.open(config.ledger_path)
    try:
        record_position(ledger, "demo", JobPosition(
            job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
            pending_requests=(_INSTALL_REQUEST,), stop_reason="owner_request",
        ))
        token = office_approval.issue_office_approval_token(
            config, ledger, "demo", job_name, attempt_id, "install",
        )
    finally:
        ledger.close()
    token_path = _write_token(tmp_path, token)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    capsys.readouterr()

    code = gb2.main(["approve-install", "demo", "--office-token", token_path])

    assert code == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["job_name"] == job_name

    ledger = Ledger.open(config.ledger_path)
    try:
        approved = [
            event for _seq, event in ledger.events("demo", "owner.approved")
            if event.attempt_id == attempt_id
        ]
    finally:
        ledger.close()
    assert len(approved) == 1
    assert approved[0].payload.get("confirm_source") == "office"


def test_approve_install_office_token_rejects_nonce_replay(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = _init_demo_app(gb2, tmp_path, monkeypatch)
    config, config_path = _set_office_key(apps_dir)
    config = _make_install_harmless(config, config_path)
    job_name = "gb2-office-install-2"
    attempt_id = _open_attempt(config, job_name)

    ledger = Ledger.open(config.ledger_path)
    try:
        record_position(ledger, "demo", JobPosition(
            job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
            pending_requests=(_INSTALL_REQUEST,), stop_reason="owner_request",
        ))
        token = office_approval.issue_office_approval_token(
            config, ledger, "demo", job_name, attempt_id, "install",
        )
    finally:
        ledger.close()
    token_path = _write_token(tmp_path, token)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    first_code = gb2.main(["approve-install", "demo", "--office-token", token_path])
    assert first_code == 0

    second_code = gb2.main(["approve-install", "demo", "--office-token", token_path])
    assert second_code == 1


# ---------------------------------------------------------------------------
# approve-plan-edit --office-token (refusal path -- wiring check)
# ---------------------------------------------------------------------------

def test_approve_plan_edit_office_token_refused_without_a_key_file(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the --office-token branch is wired into approve-plan-edit
    (it calls verify_office_token, which refuses with no key file set
    up) WITHOUT needing a full valid plan.json fixture -- the success
    path for kind="plan_edit" is exercised directly against engine.
    application.office_approval in tests/unit/test_office_approval.py.
    approve-plan-edit never had a TTY gate (see its own docstring), so
    there is nothing to prove was "skipped" here; only that a bad/
    absent office token is refused the same way any other refusal
    reason already is."""
    apps_dir = _init_demo_app(gb2, tmp_path, monkeypatch)
    config_path = apps_dir / "demo" / "config.json"
    config = EngineConfig.from_json(config_path)
    job_name = "gb2-office-plan-edit-1"
    attempt_id = _open_attempt(config, job_name)

    fake_token_path = _write_token(tmp_path, {
        "app": "demo", "job_name": job_name, "attempt_id": attempt_id,
        "kind": "plan_edit", "target_version": "x", "issued_at": 0,
        "nonce": "n", "ttl_seconds": 600, "sig": "0" * 64,
    })

    code = gb2.main(["approve-plan-edit", "demo", "--reason", "owner edit",
                     "--office-token", fake_token_path])

    assert code == 1
    ledger = Ledger.open(config.ledger_path)
    try:
        amended = [
            event for _seq, event in ledger.events("demo", "plan.owner_amended")
            if event.payload.get("job") == job_name
        ]
    finally:
        ledger.close()
    assert amended == []


# ---------------------------------------------------------------------------
# approve-install --office-token never echoes the raw request
# ---------------------------------------------------------------------------

def test_approve_install_office_token_never_prints_the_raw_request(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The TTY path prints the full pending request to stderr so the
    human approving it can read it before typing "승인 <job>" -- that
    print must NEVER happen on the --office-token path (no human is
    reading this process's stderr; a web server collecting subprocess
    output could persist it to a log). Uses a synthetic "secret-like"
    value in the request content as the thing that must never appear
    anywhere in captured output, on EITHER a successful or a refused
    office-token call."""
    secret_marker = "SECRET-MARKER-9f3c21"
    install_request = {"kind": "install", "manifest": "package.json",
                       "change": {"dependencies": {"left-pad": f"1.0.0-{secret_marker}"}}}
    apps_dir = _init_demo_app(gb2, tmp_path, monkeypatch)
    config, config_path = _set_office_key(apps_dir)
    config = _make_install_harmless(config, config_path)
    job_name = "gb2-office-install-secret"
    attempt_id = _open_attempt(config, job_name)

    ledger = Ledger.open(config.ledger_path)
    try:
        record_position(ledger, "demo", JobPosition(
            job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
            pending_requests=(install_request,), stop_reason="owner_request",
        ))
        token = office_approval.issue_office_approval_token(
            config, ledger, "demo", job_name, attempt_id, "install",
        )
    finally:
        ledger.close()
    token_path = _write_token(tmp_path, token)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    capsys.readouterr()

    code = gb2.main(["approve-install", "demo", "--office-token", token_path])

    assert code == 0
    captured = capsys.readouterr()
    assert secret_marker not in captured.out
    assert secret_marker not in captured.err


def test_approve_install_office_token_forged_signature_never_prints_the_raw_request(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same secrecy guarantee for a call that ends up REFUSED -- the
    raw pending request must not be printed even on the way to a
    rejection."""
    secret_marker = "SECRET-MARKER-refused-7a1b"
    install_request = {"kind": "install", "manifest": "package.json",
                       "change": {"dependencies": {"left-pad": f"1.0.0-{secret_marker}"}}}
    apps_dir = _init_demo_app(gb2, tmp_path, monkeypatch)
    config, config_path = _set_office_key(apps_dir)
    config = _make_install_harmless(config, config_path)
    job_name = "gb2-office-install-secret-2"
    attempt_id = _open_attempt(config, job_name)

    ledger = Ledger.open(config.ledger_path)
    try:
        record_position(ledger, "demo", JobPosition(
            job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
            pending_requests=(install_request,), stop_reason="owner_request",
        ))
        token = office_approval.issue_office_approval_token(
            config, ledger, "demo", job_name, attempt_id, "install",
        )
    finally:
        ledger.close()
    forged = {**token, "sig": "0" * len(token["sig"])}
    token_path = _write_token(tmp_path, forged)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    capsys.readouterr()

    code = gb2.main(["approve-install", "demo", "--office-token", token_path])

    assert code == 1
    captured = capsys.readouterr()
    assert secret_marker not in captured.out
    assert secret_marker not in captured.err


# ---------------------------------------------------------------------------
# approve-install --office-token: snapshot pinning
# ---------------------------------------------------------------------------

def test_approve_install_office_token_refuses_when_batch_changed_after_issuance(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token signed against a ONE-request pending batch must be
    refused once a SECOND request joins the batch before the CLI call
    -- install_target_version is computed from the CURRENT pending
    list, pinned once and shared between verification and approval,
    so there is no window where the two could observe
    different batches; this proves the end-to-end CLI refusal."""
    apps_dir = _init_demo_app(gb2, tmp_path, monkeypatch)
    config, config_path = _set_office_key(apps_dir)
    config = _make_install_harmless(config, config_path)
    job_name = "gb2-office-install-batch"
    attempt_id = _open_attempt(config, job_name)

    ledger = Ledger.open(config.ledger_path)
    try:
        record_position(ledger, "demo", JobPosition(
            job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
            pending_requests=(_INSTALL_REQUEST,), stop_reason="owner_request",
        ))
        token = office_approval.issue_office_approval_token(
            config, ledger, "demo", job_name, attempt_id, "install",
        )
        other_request = {"kind": "install", "manifest": "package.json",
                         "change": {"dependencies": {"zod": "3.0.0"}}}
        record_position(ledger, "demo", JobPosition(
            job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
            pending_requests=(_INSTALL_REQUEST, other_request), stop_reason="owner_request",
        ))
    finally:
        ledger.close()
    token_path = _write_token(tmp_path, token)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    code = gb2.main(["approve-install", "demo", "--office-token", token_path])

    assert code == 1
    ledger = Ledger.open(config.ledger_path)
    try:
        approved = [e for _seq, e in ledger.events("demo", "owner.approved")
                   if e.attempt_id == attempt_id]
    finally:
        ledger.close()
    assert approved == []
