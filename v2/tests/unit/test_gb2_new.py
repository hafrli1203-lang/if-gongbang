"""Unit tests for tools/gb2's full-orbit subcommands: `new`, `change`,
`approve-design`, `continue` (the full-orbit contract)
orbit-contract.md). engine.full_orbit (worker A's own file, landing in
parallel) is never imported for real here -- a stub module is injected
into sys.modules so these tests exercise exactly what tools/gb2 itself
does: refuse an existing app folder or an unsafe name, scaffold the new
repo + workspace, hand off to run_full_job/continue_full_job with this
app's config/ledger/job name, and report the resulting FullOutcome the
same way `gb2 run` reports an ordinary JobOutcome."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from dataclasses import dataclass
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

from engine.config import EngineConfig, ProjectConfig, write_json
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
    loader = SourceFileLoader("gb2_new_under_test", str(_GB2_PATH))
    spec = importlib.util.spec_from_loader("gb2_new_under_test", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture()
def gb2() -> ModuleType:
    # function-scoped (not module-scoped like test_gb2.py's fixture):
    # each test here installs its own fake engine.full_orbit via
    # sys.modules, and a fresh module instance keeps that from leaking
    # between tests that use different fakes.
    return _load_gb2()


@dataclass(frozen=True)
class _FakeFullOutcome:
    status: str
    reason: str | None = None
    attempt_id: str | None = None
    report_path: str | None = None


def _install_fake_full_orbit(monkeypatch: pytest.MonkeyPatch, *, run_result=None,
                             continue_result=None):
    """Inject a stub `engine.full_orbit` module into sys.modules (the
    real one does not exist yet -- worker A's own file, landing in
    parallel per the full-orbit contract) and return the two call-log
    lists `run_calls`/`continue_calls` tools/gb2's lazy
    `from engine import full_orbit` will resolve to."""
    run_calls: list[tuple] = []
    continue_calls: list[tuple] = []
    stub = types.ModuleType("engine.full_orbit")

    def run_full_job(config, ledger, fakes, project, job_name, request, *,
                     max_fix_rounds=2, on_progress=None):
        run_calls.append((config, ledger, fakes, project, job_name, request,
                          max_fix_rounds, on_progress))
        return run_result if run_result is not None else _FakeFullOutcome(status="done")

    def continue_full_job(config, ledger, fakes, project, job_name, *, on_progress=None):
        continue_calls.append((config, ledger, fakes, project, job_name, on_progress))
        return continue_result if continue_result is not None else _FakeFullOutcome(status="done")

    stub.run_full_job = run_full_job  # type: ignore[attr-defined]
    stub.continue_full_job = continue_full_job  # type: ignore[attr-defined]
    stub.FullOutcome = _FakeFullOutcome  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.full_orbit", stub)
    # `from engine import full_orbit` reads the package attribute first,
    # so a real module imported earlier in the run must be shadowed too.
    import engine
    monkeypatch.setattr(engine, "full_orbit", stub, raising=False)
    return run_calls, continue_calls


# ---------------------------------------------------------------------------
# gb2 new
# ---------------------------------------------------------------------------

def test_cmd_new_refuses_when_app_folder_already_exists(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    source_root = tmp_path / "source"
    monkeypatch.setenv("GB2_APPS_ROOT", str(source_root))
    (source_root / "demo").mkdir(parents=True)
    run_calls, _ = _install_fake_full_orbit(monkeypatch)

    code = gb2.main(["new", "demo", "요구사항"])

    assert code == 1
    assert run_calls == []
    assert not (apps_dir / "demo").exists()


@pytest.mark.parametrize("bad_name", [
    "../escape", "a/b", ".", "..", "-rf", "", "has space",
])
def test_cmd_new_refuses_unsafe_app_names(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_name: str,
) -> None:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    run_calls, _ = _install_fake_full_orbit(monkeypatch)

    # "-rf" is also rejected by argparse itself (looks like an option),
    # as a SystemExit rather than main()'s own return code -- either
    # way, nothing got created and run_full_job was never called.
    try:
        code = gb2.main(["new", bad_name, "요구사항"])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1

    assert code != 0
    assert run_calls == []


def test_cmd_new_requires_requirement_text(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    run_calls, _ = _install_fake_full_orbit(monkeypatch)

    code = gb2.main(["new", "demo"])

    assert code == 1
    assert run_calls == []
    assert not (tmp_path / "source" / "demo").exists()


def test_cmd_new_scaffolds_repo_workspace_and_calls_run_full_job(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    source_root = tmp_path / "source"
    monkeypatch.setenv("GB2_APPS_ROOT", str(source_root))
    run_calls, _ = _install_fake_full_orbit(
        monkeypatch, run_result=_FakeFullOutcome(status="done", report_path="r.md"),
    )

    code = gb2.main(["new", "demo", "할 일을 정리하는 앱을 만들어줘"])

    assert code == 0
    source_repo = source_root / "demo"
    # one initial commit, from the new-app skeleton
    assert (source_repo / "package.json").exists()
    assert (source_repo / "README.md").exists()
    assert (source_repo / ".gitignore").exists()
    assert (source_repo / "tests" / "scenes" / ".gitkeep").exists()
    assert _rgit(source_repo, "log", "--format=%s") == "chore: scaffold demo via gb2 new"
    package = json.loads((source_repo / "package.json").read_text(encoding="utf-8"))
    assert package["name"] == "demo"

    # the engine workspace (config.json, bench_cases.json, the gb2/work
    # worktree, the ledger's project row) was set up the same way
    # `gb2 init` does it.
    workspace = apps_dir / "demo"
    assert (workspace / "config.json").exists()
    assert (workspace / "repo").exists()
    config = EngineConfig.from_json(workspace / "config.json")
    assert "demo" in config.projects
    assert config.station_owners["facts"]["facts"] == (".gongbang/",)
    assert config.station_owners["plan"]["planner"] == (".gongbang/", "tests/scenes/")
    assert config.station_owners["design"]["designer"] == (".gongbang/",)

    # run_full_job got this app's config/project/job_name/request text
    assert len(run_calls) == 1
    (cfg, _ledger, fakes_for, project, job_name, request,
     _max_rounds, _on_progress) = run_calls[0]
    assert cfg.projects.keys() == {"demo"}
    assert project == "demo"
    assert request == "할 일을 정리하는 앱을 만들어줘"
    assert job_name.startswith("gb2-new-")
    # fakes_for is a callable (attempt_id) -> Fakes, same convention as
    # engine.fixup.fix's own fakes_for -- not a single pre-built Fakes,
    # since run_full_job opens its own (possibly more than one, across
    # fix rounds) attempt internally.
    built = fakes_for("A-demo-1#1")
    assert built.installer is not None


def test_cmd_new_reports_needs_owner_with_exit_code_2(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    _install_fake_full_orbit(
        monkeypatch,
        run_result=_FakeFullOutcome(status="needs_owner", reason="design_approval"),
    )

    code = gb2.main(["new", "demo", "요구사항"])

    assert code == 2


# ---------------------------------------------------------------------------
# gb2 change
# ---------------------------------------------------------------------------

def _init_demo_app(gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scaffold + `gb2 init` an existing app (no full-orbit call) so
    `gb2 change` has something initialized to work against."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    source_root = tmp_path / "source"
    monkeypatch.setenv("GB2_APPS_ROOT", str(source_root))
    source_repo = source_root / "demo"
    source_repo.mkdir(parents=True)
    _rgit(source_repo, "init", "-q", "-b", "master")
    (source_repo / "package.json").write_text("{}\n", encoding="utf-8")
    _rgit(source_repo, "add", "-A")
    _rgit(source_repo, "commit", "-q", "-m", "init")

    code = gb2.main(["init", "demo"])
    assert code == 0
    return apps_dir


def test_cmd_change_refuses_when_app_not_initialized(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    run_calls, _ = _install_fake_full_orbit(monkeypatch)

    code = gb2.main(["change", "demo", "뭔가 바꿔줘"])

    assert code == 1
    assert run_calls == []


def test_cmd_change_requires_change_request_text(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_demo_app(gb2, tmp_path, monkeypatch)
    run_calls, _ = _install_fake_full_orbit(monkeypatch)

    code = gb2.main(["change", "demo"])

    assert code == 1
    assert run_calls == []


def test_cmd_change_calls_run_full_job_against_the_initialized_app(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_demo_app(gb2, tmp_path, monkeypatch)
    run_calls, _ = _install_fake_full_orbit(monkeypatch)

    code = gb2.main(["change", "demo", "로그인 화면을 추가해줘"])

    assert code == 0
    assert len(run_calls) == 1
    (_cfg, _ledger, _fakes_for, project, job_name, request,
     _max_rounds, _on_progress) = run_calls[0]
    assert project == "demo"
    assert request == "로그인 화면을 추가해줘"
    assert job_name.startswith("gb2-change-")


# ---------------------------------------------------------------------------
# gb2 approve-design
# ---------------------------------------------------------------------------

def _force_tty_approval(monkeypatch: pytest.MonkeyPatch, owner_text: str) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": owner_text)


def _init_demo_app_with_open_attempt(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[EngineConfig, str]:
    apps_dir = _init_demo_app(gb2, tmp_path, monkeypatch)
    config = EngineConfig.from_json(apps_dir / "demo" / "config.json")
    ledger = Ledger.open(config.ledger_path)
    try:
        attempt_id = open_job(config, ledger, "demo", "gb2-new-1000", "full", roles=())
    finally:
        ledger.close()
    return config, attempt_id


def test_cmd_approve_design_refuses_without_a_tty(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_demo_app_with_open_attempt(gb2, tmp_path, monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _run_calls, continue_calls = _install_fake_full_orbit(monkeypatch)

    code = gb2.main(["approve-design", "demo"])

    assert code == 4
    assert continue_calls == []


def test_cmd_approve_design_refuses_wrong_approval_line(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_demo_app_with_open_attempt(gb2, tmp_path, monkeypatch)
    _force_tty_approval(monkeypatch, "yes")
    _run_calls, continue_calls = _install_fake_full_orbit(monkeypatch)

    code = gb2.main(["approve-design", "demo"])

    assert code == 1
    assert continue_calls == []


def test_cmd_approve_design_appends_event_and_calls_continue_full_job(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, attempt_id = _init_demo_app_with_open_attempt(gb2, tmp_path, monkeypatch)
    _force_tty_approval(monkeypatch, "승인 gb2-new-1000")
    _run_calls, continue_calls = _install_fake_full_orbit(monkeypatch)

    code = gb2.main(["approve-design", "demo"])

    assert code == 0
    assert len(continue_calls) == 1
    (_cfg, _ledger, fakes_for, project, job_name, _on_progress) = continue_calls[0]
    assert project == "demo"
    assert job_name == "gb2-new-1000"
    assert fakes_for(attempt_id).installer is not None

    ledger = Ledger.open(config.ledger_path)
    try:
        approved = [
            event for _seq, event in ledger.events("demo", "design.approved")
            if event.payload.get("job") == "gb2-new-1000"
        ]
    finally:
        ledger.close()
    assert len(approved) == 1


# ---------------------------------------------------------------------------
# gb2 continue
# ---------------------------------------------------------------------------

def test_cmd_continue_calls_continue_full_job(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, _attempt_id = _init_demo_app_with_open_attempt(gb2, tmp_path, monkeypatch)
    _run_calls, continue_calls = _install_fake_full_orbit(monkeypatch)

    code = gb2.main(["continue", "demo"])

    assert code == 0
    assert len(continue_calls) == 1
    (_cfg, _ledger, _fakes_for, project, job_name, _on_progress) = continue_calls[0]
    assert project == "demo"
    assert job_name == "gb2-new-1000"


def test_cmd_continue_refuses_when_no_active_job(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_demo_app(gb2, tmp_path, monkeypatch)
    _run_calls, continue_calls = _install_fake_full_orbit(monkeypatch)

    code = gb2.main(["continue", "demo"])

    assert code == 1
    assert continue_calls == []
