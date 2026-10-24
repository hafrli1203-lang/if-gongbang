"""Unit tests for tools/gb2: config generation from the template,
argument parsing, and (for ship/skip-ship/sync) the release track end
to end against throwaway git repos under tmp_path -- never against a
real app's own repo. gb2 init's own worktree/branch/node_modules-copy
steps against a real app repo are exercised manually, not here."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

from dataclasses import replace

from engine.approvals import grant
from engine.config import EngineConfig, ProjectConfig, ReleaseConfig, write_json
from engine.evidence import fingerprint, record
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Ledger
from engine.processor import Fakes, Processor
from engine.steps.open_job import open_job

_GB2_PATH = Path(__file__).resolve().parents[2] / "tools" / "gb2"
_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "tools" / "templates" / "node-vitest.json"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
    "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
}


def _rgit(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=_GIT_ENV).stdout.strip()


def _make_source_repo(path: Path) -> Path:
    """A throwaway repo whose default branch is literally "master" --
    tools/gb2.WORKTREE_BASE -- unlike tests/conftest.make_repo (branch
    "main"), since gb2's own worktree/rebase/release code all hardcode
    "master" as the app's original branch name."""
    path.mkdir(parents=True)
    _rgit(path, "init", "-q", "-b", "master")
    (path / "app.py").write_text("v1\n", encoding="utf-8")
    _rgit(path, "add", "-A")
    _rgit(path, "commit", "-q", "-m", "init")
    return path


def _worktree_repo(source_repo: Path, dest: Path, branch: str = "gb2/work") -> Path:
    _rgit(source_repo, "branch", branch, "master")
    _rgit(source_repo, "worktree", "add", str(dest), branch)
    return dest


def _load_gb2() -> ModuleType:
    """tools/gb2 has no .py extension (it is an executable script), so a
    plain `import` can't find it -- load it explicitly via
    SourceFileLoader, the same way Python's own importlib docs show for
    a file with an arbitrary name."""
    loader = SourceFileLoader("gb2_under_test", str(_GB2_PATH))
    spec = importlib.util.spec_from_loader("gb2_under_test", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gb2() -> ModuleType:
    return _load_gb2()


def test_template_file_has_expected_shape(gb2: ModuleType) -> None:
    template = gb2.load_template(_TEMPLATE_PATH)
    assert list(template["projects"].keys()) == ["__APP__"]
    project = template["projects"]["__APP__"]
    assert project["repo"] == "repo"
    assert project["bench_cases"] == "../bench_cases.json"
    assert project["bench_scope"] == "related"
    assert "{files}" in project["related_cmd"]
    assert template["parallel_review_bench"] is True
    assert project["release"] is None  # off by default -- --with-release turns it on
    assert template["publisher"]["kind"] == "shell"
    assert template["publisher"]["dry_run"] is False
    assert "{target}" in template["publisher"]["argv"]
    assert "__DEPLOY_SCRIPT__" in template["publisher"]["argv"][0]


def test_render_config_renames_app_placeholder(gb2: ModuleType, tmp_path: Path) -> None:
    template = gb2.load_template(_TEMPLATE_PATH)
    rendered = gb2.render_config(template, "shortform")
    assert list(rendered["projects"].keys()) == ["shortform"]
    assert rendered["projects"]["shortform"]["repo"] == "repo"
    # the rest of the template is untouched
    assert rendered["station_owners"] == template["station_owners"]
    assert rendered["workers"]["builder"]["runner"] == "claude"


def test_render_config_rejects_wrong_shape(gb2: ModuleType) -> None:
    with pytest.raises(ValueError):
        gb2.render_config({"projects": {"not_app": {}}}, "myapp")
    with pytest.raises(ValueError):
        gb2.render_config({"projects": {}}, "myapp")


def test_render_config_is_loadable_as_engine_config(gb2: ModuleType, tmp_path: Path) -> None:
    """The rendered dict, written to a real config.json alongside a
    fake app repo directory, must load through EngineConfig.from_json
    without error -- proves the template's shape genuinely matches
    what engine.config expects, not just gb2's own assumptions."""
    import subprocess

    from engine.config import EngineConfig

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    (tmp_path / "bench_cases.json").write_text("[]", encoding="utf-8")

    template = gb2.load_template(_TEMPLATE_PATH)
    rendered = gb2.render_config(template, "myapp")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(rendered), encoding="utf-8")

    config = EngineConfig.from_json(config_path)
    assert "myapp" in config.projects
    assert config.projects["myapp"].repo == repo_dir.resolve()
    assert config.projects["myapp"].bench_scope == "related"
    assert config.parallel_review_bench is True


def test_arg_parsing_init(gb2: ModuleType) -> None:
    args = gb2._build_parser().parse_args(["init", "shortform"])
    assert args.command == "init"
    assert args.app == "shortform"


def test_arg_parsing_run_with_inline_task(gb2: ModuleType) -> None:
    args = gb2._build_parser().parse_args(["run", "shortform", "fix the wording"])
    assert args.command == "run"
    assert args.app == "shortform"
    assert args.task == "fix the wording"
    assert args.task_file is None
    assert args.job_name is None


def test_arg_parsing_run_with_task_file_and_job_name(gb2: ModuleType) -> None:
    args = gb2._build_parser().parse_args([
        "run", "shortform", "--task-file", "task.md", "--job-name", "wording-fix",
    ])
    assert args.task is None
    assert args.task_file == "task.md"
    assert args.job_name == "wording-fix"


def test_arg_parsing_fix_with_and_without_extra(gb2: ModuleType) -> None:
    args = gb2._build_parser().parse_args(["fix", "shortform"])
    assert args.command == "fix"
    assert args.app == "shortform"
    assert args.extra is None

    args = gb2._build_parser().parse_args(["fix", "shortform", "also check the edge case"])
    assert args.extra == "also check the edge case"


def test_arg_parsing_status_log_diff(gb2: ModuleType) -> None:
    parser = gb2._build_parser()
    assert parser.parse_args(["status", "shortform"]).command == "status"
    assert parser.parse_args(["log", "shortform"]).command == "log"
    assert parser.parse_args(["diff", "shortform"]).command == "diff"


def test_arg_parsing_requires_a_command(gb2: ModuleType) -> None:
    with pytest.raises(SystemExit):
        gb2._build_parser().parse_args([])


def test_findings_by_severity_counts(gb2: ModuleType) -> None:
    findings = [
        {"severity": "P1", "file": "a.ts", "note": "x"},
        {"severity": "P1", "file": "b.ts", "note": "y"},
        {"severity": "P3", "file": "c.ts", "note": "z"},
        "not-a-dict",
    ]
    assert gb2._findings_by_severity(findings) == {"P1": 2, "P3": 1}


def test_run_log_roundtrip(gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gb2, "APPS_DIR", tmp_path)
    (tmp_path / "shortform").mkdir()
    assert gb2._read_run_log("shortform") == []
    gb2._append_run_log("shortform", {"job_name": "a", "status": "done"})
    gb2._append_run_log("shortform", {"job_name": "b", "status": "failed"})
    runs = gb2._read_run_log("shortform")
    assert [r["job_name"] for r in runs] == ["a", "b"]


# ---------------------------------------------------------------------------
# ship / skip-ship / sync (the release track: engine.release + the
# orbit stop after commit). Each test builds its own throwaway
# workspace (fresh tmp_path per test, pytest's own isolation) with a
# fake deploy script standing in for the real one -- a real deploy
# command is never invoked.
# ---------------------------------------------------------------------------

def _fake_deploy_script(tmp_path: Path, log_path: Path) -> Path:
    script = tmp_path / "fake_deploy.py"
    script.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(log_path)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    return script


def _setup_release_job(gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Build a "demo" gb2 workspace with `release` configured (master_repo
    = the real original repo, sharing git history with the gb2/work
    worktree the same way `gb2 init --with-release` would set up), open
    an attempt, make a change, record matching review+bench evidence,
    then run the commit stage via orbit directly so the project is
    already stopped at needs_owner/release_approval -- exactly the state
    `gb2 run` would leave it in for a release-configured app. Returns a
    dict with everything a test needs: config, workspace paths, the
    pending request_id/sha, and the deploy log path the fake script
    writes its argv to."""
    from engine import orbit

    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    workspace = apps_dir / "demo"
    workspace.mkdir()
    (workspace / "home").mkdir()
    (workspace / "secrets").mkdir()

    source_repo = _make_source_repo(tmp_path / "source" / "demo")
    repo_dir = _worktree_repo(source_repo, workspace / "repo")

    deploy_log = tmp_path / "deploy.log"
    deploy_script = _fake_deploy_script(tmp_path, deploy_log)

    config = EngineConfig(
        ledger_path=workspace / "ledger.sqlite",
        home=workspace / "home",
        secrets_dir=workspace / "secrets",
        projects={"demo": ProjectConfig(
            "demo", repo_dir,
            release=ReleaseConfig(master_repo=source_repo, target="demo"),
        )},
        station_owners={},
        approval_ttl_sec=3600,
        publisher_kind="shell",
        publisher_argv=(sys.executable, str(deploy_script), "{target}"),
        publisher_dry_run_argv=(sys.executable, str(deploy_script), "{target}", "--check"),
        publisher_dry_run=False,
    )
    config = write_json(config, workspace / "config.json")

    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("demo")
    attempt_id = open_job(config, ledger, "demo", "job1", "full", roles=())
    (repo_dir / "app.py").write_text("v2\n", encoding="utf-8")
    fp = fingerprint(repo_dir, config.projects["demo"].excludes)
    record(ledger, "demo", attempt_id, "review", fp, payload={"passed": True})
    record(ledger, "demo", attempt_id, "bench", fp, payload={"passed": True, "scope": "full"})

    fakes = Fakes(FakePublisher(), FakeInstaller())
    outcome = orbit.continue_job(
        config, ledger, fakes, "demo", attempt_id,
        [orbit.Stage("commit", {"message": "gongbang: job1"})],
    )
    assert outcome.status == "needs_owner"
    request_id = outcome.pending_requests[0]["request_id"]
    sha = outcome.pending_requests[0]["artifact_hash"]
    ledger.close()

    return {
        "config": config, "source_repo": source_repo, "repo_dir": repo_dir,
        "attempt_id": attempt_id, "request_id": request_id, "sha": sha,
        "deploy_log": deploy_log,
    }


def _force_tty_approval(monkeypatch: pytest.MonkeyPatch, owner_text: str) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": owner_text)


def test_ship_refuses_without_a_tty(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _setup_release_job(gb2, tmp_path, monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    code = gb2.main(["ship", "demo"])

    assert code == 4
    ledger = Ledger.open(job["config"].ledger_path)
    assert ledger.request(job["request_id"]).status == "queued"
    assert ledger.project("demo").state == "OPEN"
    ledger.close()
    assert not job["deploy_log"].exists()


def test_ship_refuses_wrong_approval_line(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _setup_release_job(gb2, tmp_path, monkeypatch)
    _force_tty_approval(monkeypatch, "nope, not approving this")

    code = gb2.main(["ship", "demo"])

    assert code == 3
    ledger = Ledger.open(job["config"].ledger_path)
    assert ledger.request(job["request_id"]).status == "queued"  # never granted
    assert ledger.project("demo").state == "OPEN"
    ledger.close()
    assert not job["deploy_log"].exists()


def test_ship_with_correct_line_merges_and_deploys_and_closes(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine import release

    job = _setup_release_job(gb2, tmp_path, monkeypatch)
    _force_tty_approval(monkeypatch, f"승인 {job['request_id']}")

    code = gb2.main(["ship", "demo"])

    assert code == 0
    assert job["deploy_log"].read_text(encoding="utf-8") == "demo"
    ledger = Ledger.open(job["config"].ledger_path)
    assert ledger.request(job["request_id"]).status == "done"
    assert ledger.project("demo").state == "CLOSED"
    ledger.close()
    assert release.head_sha(job["source_repo"]) == job["sha"]


def test_skip_ship_abandons_publish_without_deploy_and_closes(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine import release

    job = _setup_release_job(gb2, tmp_path, monkeypatch)

    code = gb2.main(["skip-ship", "demo"])

    assert code == 0
    ledger = Ledger.open(job["config"].ledger_path)
    req = ledger.request(job["request_id"])
    assert req.status == "failed"
    assert req.result == {"reason": "no_approval"}
    assert ledger.project("demo").state == "CLOSED"
    ledger.close()
    assert not job["deploy_log"].exists()
    assert release.head_sha(job["source_repo"]) != job["sha"]


def test_sync_rebases_worktree_onto_current_master(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    source_repo = _make_source_repo(tmp_path / "source" / "demo")
    repo_dir = _worktree_repo(source_repo, apps_dir / "demo" / "repo")

    (repo_dir / "work.py").write_text("work\n", encoding="utf-8")
    _rgit(repo_dir, "add", "-A")
    _rgit(repo_dir, "commit", "-q", "-m", "work change")
    (source_repo / "master_change.py").write_text("m\n", encoding="utf-8")
    _rgit(source_repo, "add", "-A")
    _rgit(source_repo, "commit", "-q", "-m", "master advance")

    code = gb2.main(["sync", "demo"])

    assert code == 0
    log = _rgit(repo_dir, "log", "--format=%s")
    assert "master advance" in log
    assert "work change" in log


def test_sync_refuses_when_worktree_is_dirty(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    source_repo = _make_source_repo(tmp_path / "source" / "demo")
    repo_dir = _worktree_repo(source_repo, apps_dir / "demo" / "repo")
    (repo_dir / "dirty.py").write_text("d\n", encoding="utf-8")

    code = gb2.main(["sync", "demo"])

    assert code != 0
    # the branch never moved -- refused before any rebase was attempted.
    assert _rgit(repo_dir, "log", "--format=%s") == "init"


# ---------------------------------------------------------------------------
# P2-1: app name validation, workspace/source containment, worktree
# identity verification.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_name", [
    "../escape", "a/b", ".", "..", "-rf", "", "has space",
])
def test_app_workspace_rejects_unsafe_app_names(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_name: str,
) -> None:
    monkeypatch.setattr(gb2, "APPS_DIR", tmp_path / "apps")
    with pytest.raises(ValueError):
        gb2.app_workspace(bad_name)


def test_app_workspace_accepts_a_safe_name(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gb2, "APPS_DIR", tmp_path / "apps")
    workspace = gb2.app_workspace("my-app_1.2")
    assert workspace == (tmp_path / "apps" / "my-app_1.2").resolve()


def test_cmd_init_refuses_when_workspace_and_source_repo_coincide(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfiguration where GB2_WORKSPACE/apps and GB2_APPS_ROOT
    point at the same place must never let gb2 init make the app's own
    workspace double as its source repo."""
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", shared_root)
    monkeypatch.setenv("GB2_APPS_ROOT", str(shared_root))
    args = gb2._build_parser().parse_args(["init", "demo"])

    code = gb2.cmd_init(args)

    assert code == 1


def test_ensure_worktree_rejects_an_unrelated_existing_directory(
    tmp_path: Path, gb2: ModuleType,
) -> None:
    """An existing `repo/` that is NOT actually a registered worktree of
    the source repo (e.g. a stray directory left over from something
    else) must never be silently adopted and used as the app's repo."""
    source_repo = _make_source_repo(tmp_path / "source" / "demo")
    unrelated_dir = tmp_path / "apps" / "demo" / "repo"
    unrelated_dir.mkdir(parents=True)
    (unrelated_dir / "stray.txt").write_text("not a worktree\n", encoding="utf-8")

    with pytest.raises(ValueError):
        gb2._ensure_worktree(source_repo, unrelated_dir)


def test_ensure_worktree_accepts_its_own_previously_created_worktree(
    tmp_path: Path, gb2: ModuleType,
) -> None:
    source_repo = _make_source_repo(tmp_path / "source" / "demo")
    repo_dir = tmp_path / "apps" / "demo" / "repo"
    gb2._ensure_worktree(source_repo, repo_dir)  # creates it

    gb2._ensure_worktree(source_repo, repo_dir)  # re-entrant: must not raise


# ---------------------------------------------------------------------------
# P2-2: gb2 run must drive the full stage list in ONE continue_job call
# (so parallel review/bench can actually overlap), not one growing
# prefix at a time.
# ---------------------------------------------------------------------------

def test_cmd_run_drives_continue_job_exactly_once_with_the_full_stage_list(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import dataclass, field

    from engine import orbit as orbit_module
    from engine.config import EngineConfig, ProjectConfig
    from engine.ledger import Ledger
    from engine.steps.open_job import open_job

    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    workspace = apps_dir / "demo"
    workspace.mkdir()
    (workspace / "home").mkdir()
    (workspace / "secrets").mkdir()
    source_repo = _make_source_repo(tmp_path / "source" / "demo")
    repo_dir = _worktree_repo(source_repo, workspace / "repo")

    config = EngineConfig(
        ledger_path=workspace / "ledger.sqlite", home=workspace / "home",
        secrets_dir=workspace / "secrets",
        projects={"demo": ProjectConfig("demo", repo_dir)},
        station_owners={},
    )
    from engine.config import write_json
    config = write_json(config, workspace / "config.json")
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("demo")
    ledger.close()

    calls: list[list[str]] = []
    real_continue_job = orbit_module.continue_job

    def _spy(cfg, ledger_, fakes, project, attempt_id, stages, **kwargs):
        calls.append([s.kind for s in stages])
        return real_continue_job(cfg, ledger_, fakes, project, attempt_id, stages, **kwargs)

    monkeypatch.setattr(orbit_module, "continue_job", _spy)
    monkeypatch.setattr(gb2.orbit, "continue_job", _spy)

    gb2.main(["run", "demo", "do something"])

    assert len(calls) == 1  # exactly one continue_job call, not one per stage
    assert calls[0] == [
        "run_station", "merge", "review", "bench", "commit", "report", "close",
    ]


def test_cmd_fix_refuses_when_there_is_no_failed_attempt(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """A fresh (never-run) app workspace has no OPEN project at all --
    engine.fixup.FixupError must surface as a clean refusal (exit 1,
    one JSON error line on stderr), never an unhandled traceback."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    workspace = apps_dir / "demo"
    workspace.mkdir()
    (workspace / "home").mkdir()
    (workspace / "secrets").mkdir()
    source_repo = _make_source_repo(tmp_path / "source" / "demo")
    repo_dir = _worktree_repo(source_repo, workspace / "repo")

    config = EngineConfig(
        ledger_path=workspace / "ledger.sqlite", home=workspace / "home",
        secrets_dir=workspace / "secrets",
        projects={"demo": ProjectConfig("demo", repo_dir)},
        station_owners={},
    )
    config = write_json(config, workspace / "config.json")
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("demo")
    ledger.close()

    code = gb2.main(["fix", "demo"])

    assert code == 1
    captured = capsys.readouterr()
    assert "no_failed_attempt" in captured.err


def test_cmd_fix_calls_fixup_fix_with_the_app_and_extra_text(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cmd_fix must hand off to engine.fixup.fix with this app's project
    name and the owner's extra text, and report the resulting outcome
    the same way cmd_run does (status/exit code) -- spied rather than
    run for real (engine.fixup.fix itself, including the close+reopen+
    overlay it does, is covered directly by tests/unit/test_fixup.py)."""
    from engine import fixup as fixup_module
    from engine import orbit as orbit_module

    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    workspace = apps_dir / "demo"
    workspace.mkdir()
    (workspace / "home").mkdir()
    (workspace / "secrets").mkdir()
    source_repo = _make_source_repo(tmp_path / "source" / "demo")
    repo_dir = _worktree_repo(source_repo, workspace / "repo")

    config = EngineConfig(
        ledger_path=workspace / "ledger.sqlite", home=workspace / "home",
        secrets_dir=workspace / "secrets",
        projects={"demo": ProjectConfig("demo", repo_dir)},
        station_owners={},
    )
    config = write_json(config, workspace / "config.json")
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("demo")
    ledger.close()

    calls: list[tuple[str, str]] = []

    def _fake_fix(cfg, ledger_, project, extra, fakes_for, **kwargs):
        calls.append((project, extra))
        attempt_id = open_job(cfg, ledger_, project, "stub-fix-job", "full", ())
        outcome = orbit_module.JobOutcome(
            attempt_id=attempt_id, status="done", failed_request=None, reason=None,
        )
        return fixup_module.FixResult(
            old_attempt_id="J-demo-OLD#1", new_attempt_id=attempt_id,
            job_name="stub-fix-job", task="composed task text", outcome=outcome,
        )

    monkeypatch.setattr(gb2.fixup, "fix", _fake_fix)

    code = gb2.main(["fix", "demo", "also check the edge case"])

    assert code == 0
    assert calls == [("demo", "also check the edge case")]


def test_cmd_init_hides_the_source_repo_from_every_sandbox(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source repo can hold real environment files; the generated
    config must list it in hidden_paths so worker, reviewer and bench
    sandboxes only ever see the worktree."""
    apps_root = tmp_path / "source"
    source_repo = _make_source_repo(apps_root / "demo")
    (source_repo / "package.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(gb2, "APPS_DIR", tmp_path / "ws" / "apps")
    monkeypatch.setenv("GB2_APPS_ROOT", str(apps_root))
    monkeypatch.setattr(gb2, "_copy_node_modules", lambda *a, **k: None, raising=False)
    args = gb2._build_parser().parse_args(["init", "demo"])

    code = gb2.cmd_init(args)

    assert code == 0
    import json as _json
    written = _json.loads((tmp_path / "ws" / "apps" / "demo" / "config.json").read_text(encoding="utf-8"))
    assert written["projects"]["demo"]["hidden_paths"] == [str(source_repo.resolve())]


# ---------------------------------------------------------------------------
# The generated negative control's expected_signature
# must match what engine.steps.bench_run._case_signature actually
# returns for a real Vitest assertion failure ("assertion"), never the
# stale "exit:1" -- otherwise bench always fails with
# wrong_failure_signature and blocks every normal job.
# ---------------------------------------------------------------------------

_VITEST_ASSERTION_FAILURE_OUTPUT = """\
 FAIL  tests/gb2-negative-control.test.ts > gb2 negative control > fails on purpose when GB2_NEGATIVE is set
AssertionError: expected 1 to be 2 // Object.is equality

- Expected
+ Received

- 2
+ 1

 ❯ tests/gb2-negative-control.test.ts:7:16

 Test Files  1 failed (1)
      Tests  1 failed (1)
"""


def test_negative_control_case_signature_matches_a_real_vitest_assertion_failure(
    gb2: ModuleType,
) -> None:
    from engine.steps.bench_run import _case_signature

    case = gb2.NEGATIVE_CONTROL_CASES[0]
    signature = _case_signature(
        {"text": _VITEST_ASSERTION_FAILURE_OUTPUT, "returncode": 1})

    assert signature == "assertion"
    assert signature == case["expected_signature"]


def test_bench_cases_need_upgrade_detects_the_stale_exit1_signature(
    gb2: ModuleType, tmp_path: Path,
) -> None:
    stale = tmp_path / "bench_cases.json"
    stale.write_text(json.dumps([
        {"name": "gb2_negative_control", "selector": "x", "expect_fail": True,
         "expected_signature": "exit:1", "env": {}},
    ]), encoding="utf-8")
    assert gb2._bench_cases_need_upgrade(stale) is True

    current = tmp_path / "current.json"
    current.write_text(json.dumps(gb2.NEGATIVE_CONTROL_CASES), encoding="utf-8")
    assert gb2._bench_cases_need_upgrade(current) is False


def test_cmd_init_rewrites_stale_bench_cases_signature(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    source_repo = tmp_path / "source" / "demo"
    source_repo.mkdir(parents=True)
    (source_repo / "package.json").write_text("{}", encoding="utf-8")
    _make_source_repo_inplace = source_repo
    _rgit(_make_source_repo_inplace, "init", "-q", "-b", "master")
    (source_repo / "x.txt").write_text("x\n", encoding="utf-8")
    _rgit(source_repo, "add", "-A")
    _rgit(source_repo, "commit", "-q", "-m", "init")

    workspace = apps_dir / "demo"
    workspace.mkdir()
    bench_cases_path = workspace / "bench_cases.json"
    bench_cases_path.write_text(json.dumps([
        {"name": "gb2_negative_control", "selector": "tests/gb2-negative-control.test.ts",
         "expect_fail": True, "expected_signature": "exit:1", "env": {"GB2_NEGATIVE": "1"}},
    ]), encoding="utf-8")

    args = gb2._build_parser().parse_args(["init", "demo"])
    code = gb2.cmd_init(args)

    assert code == 0
    rewritten = json.loads(bench_cases_path.read_text(encoding="utf-8"))
    assert rewritten[0]["expected_signature"] == "assertion"


def test_cmd_init_refuses_unrelated_existing_repo_before_writing_config(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """identity must be verified BEFORE
    any write -- an existing repo/ that is not actually this source's
    worktree must refuse without ever creating config.json."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    source_repo = tmp_path / "source" / "demo"
    source_repo.mkdir(parents=True)
    (source_repo / "package.json").write_text("{}", encoding="utf-8")
    _rgit(source_repo, "init", "-q", "-b", "master")
    (source_repo / "x.txt").write_text("x\n", encoding="utf-8")
    _rgit(source_repo, "add", "-A")
    _rgit(source_repo, "commit", "-q", "-m", "init")

    unrelated_repo_dir = apps_dir / "demo" / "repo"
    unrelated_repo_dir.mkdir(parents=True)
    (unrelated_repo_dir / "stray.txt").write_text("not a worktree\n", encoding="utf-8")

    code = gb2.main(["init", "demo"])

    assert code == 1
    assert not (apps_dir / "demo" / "config.json").exists()


def test_cmd_run_refuses_when_repo_dir_identity_no_longer_matches(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run/ship/sync/diff must re-verify
    repo_dir's identity before doing anything, not just trust it was
    valid once at init time."""
    job = _setup_release_job(gb2, tmp_path, monkeypatch)
    repo_dir = job["repo_dir"]
    # Simulate the worktree having been replaced since init: detach it
    # from the source by removing it as a registered worktree (the
    # directory itself still exists on disk).
    _rgit(job["source_repo"], "worktree", "remove", "--force", str(repo_dir))
    repo_dir.mkdir()
    (repo_dir / "stray.txt").write_text("not a worktree anymore\n", encoding="utf-8")

    code = gb2.main(["run", "demo", "do something"])

    assert code == 1


# ---------------------------------------------------------------------------
# resolve: for a publish request stuck "unknown", show the receipt,
# refuse while its process group is still alive, run the check command,
# and let the owner say whether it actually deployed.
# ---------------------------------------------------------------------------

def _setup_unknown_publish_job(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Builds on _setup_release_job: grants the approval, then forces a
    REAL ShellPublisher dispatch to end up "unknown" (process-group
    termination could not be confirmed, per engine.publishers'
    documented semantics) by monkeypatching
    engine.publishers._terminate_process_group to return False while a
    real (short-lived) child process actually runs and times out. The
    child is explicitly reaped (os.waitpid) once the request is
    confirmed "unknown", so a resolve test's own process-group-aliveness
    check sees it as gone, not a lingering zombie this test created."""
    import engine.publishers as publishers_module

    job = _setup_release_job(gb2, tmp_path, monkeypatch)
    workspace = job["config"].ledger_path.parent
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(0.05)\n", encoding="utf-8")
    hang_config = replace(
        job["config"], publisher_argv=(sys.executable, str(sleeper)),
        publisher_timeout_sec=0.01,
    )
    hang_config = write_json(hang_config, workspace / "config.json")

    ledger = Ledger.open(hang_config.ledger_path)
    grant(ledger, "demo", job["request_id"], ttl_sec=3600)
    monkeypatch.setattr(publishers_module, "_terminate_process_group", lambda process: False)
    fakes = gb2._build_fakes(hang_config, "demo", job["attempt_id"], None)
    proc = Processor(hang_config, ledger, fakes)
    proc.register_defaults()
    proc.run_once("demo")
    assert ledger.request(job["request_id"]).status == "unknown"

    receipt_path = hang_config.home / "publish" / f"{job['request_id']}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "unknown"
    pid = receipt["pid"]
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    ledger.close()

    job["config"] = hang_config
    job["receipt_path"] = receipt_path
    return job


def test_resolve_refuses_without_a_tty(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _setup_unknown_publish_job(gb2, tmp_path, monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    code = gb2.main(["resolve", "demo"])

    assert code == 4
    ledger = Ledger.open(job["config"].ledger_path)
    assert ledger.request(job["request_id"]).status == "unknown"
    ledger.close()


def test_resolve_refuses_while_process_group_still_alive(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _setup_unknown_publish_job(gb2, tmp_path, monkeypatch)
    # Claim the receipt's process group is THIS test process's own pid --
    # guaranteed alive.
    receipt = json.loads(job["receipt_path"].read_text(encoding="utf-8"))
    receipt["pgid"] = os.getpid()
    job["receipt_path"].write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    code = gb2.main(["resolve", "demo"])

    assert code == 1
    ledger = Ledger.open(job["config"].ledger_path)
    assert ledger.request(job["request_id"]).status == "unknown"
    ledger.close()


def test_resolve_deployed_requires_check_command_to_pass(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answering "deployed" when the check command itself exits
    non-zero must be refused -- the receipt and request stay
    untouched."""
    job = _setup_unknown_publish_job(gb2, tmp_path, monkeypatch)
    failing_check = tmp_path / "failing_check.py"
    failing_check.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    config = replace(job["config"], publisher_dry_run_argv=(sys.executable, str(failing_check)))
    config = write_json(config, job["config"].ledger_path.parent / "config.json")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "deployed")

    code = gb2.main(["resolve", "demo"])

    assert code == 1
    ledger = Ledger.open(config.ledger_path)
    assert ledger.request(job["request_id"]).status == "unknown"
    ledger.close()


def test_resolve_deployed_with_passing_check_records_done_and_closes(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _setup_unknown_publish_job(gb2, tmp_path, monkeypatch)
    passing_check = tmp_path / "passing_check.py"
    passing_check.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    config = replace(job["config"], publisher_dry_run_argv=(sys.executable, str(passing_check)))
    config = write_json(config, job["config"].ledger_path.parent / "config.json")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "deployed")

    code = gb2.main(["resolve", "demo"])

    assert code == 0
    ledger = Ledger.open(config.ledger_path)
    assert ledger.request(job["request_id"]).status == "done"
    assert ledger.project("demo").state == "CLOSED"
    ledger.close()
    receipt = json.loads(job["receipt_path"].read_text(encoding="utf-8"))
    assert receipt["status"] == "done"


def test_resolve_not_deployed_records_failed_and_closes_without_redeploy(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _setup_unknown_publish_job(gb2, tmp_path, monkeypatch)
    check_log = tmp_path / "check.log"
    check_script = tmp_path / "logging_check.py"
    check_script.write_text(
        f"import sys\nopen({str(check_log)!r}, 'w').write('ran')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    config = replace(job["config"], publisher_dry_run_argv=(sys.executable, str(check_script)))
    config = write_json(config, job["config"].ledger_path.parent / "config.json")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "not deployed")

    code = gb2.main(["resolve", "demo"])

    # the job's own report -> close followup still completes ("done")
    # even though the publish request itself ends up "failed" below --
    # same two-outcomes-at-once shape cmd_ship/cmd_skip_ship already have.
    assert code == 0
    assert check_log.exists()  # the check command DID run (just to inform the owner)
    ledger = Ledger.open(config.ledger_path)
    assert ledger.request(job["request_id"]).status == "failed"
    assert ledger.project("demo").state == "CLOSED"  # report -> close still happens
    ledger.close()
    receipt = json.loads(job["receipt_path"].read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"


def test_summarize_includes_job_metrics_for_gb2_log(gb2: ModuleType, tmp_path: Path) -> None:
    """engine.orbit's job.metrics event (durations_ms, test_runs) must
    surface through gb2's own _summarize dict, since cmd_log just
    prints whatever _append_run_log stored verbatim."""
    from engine.config import EngineConfig, ProjectConfig
    from engine.ledger import Event, Ledger
    from engine.steps.open_job import open_job as _open_job

    repo = _make_source_repo(tmp_path / "repo")
    config = EngineConfig(
        ledger_path=tmp_path / "ledger.sqlite", home=tmp_path / "home",
        secrets_dir=tmp_path / "secrets",
        projects={"demo": ProjectConfig("demo", repo)},
        station_owners={},
    )
    (tmp_path / "home").mkdir()
    (tmp_path / "secrets").mkdir()
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("demo")
    attempt_id = _open_job(config, ledger, "demo", "job1", "full", roles=())
    ledger.append(Event(
        project="demo", kind="job.metrics", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"durations_ms": {"commit": 12.3}, "test_runs": {"related": 2}},
    ))

    class _StubOutcome:
        status = "done"
        reason = None

    summary = gb2._summarize(config, ledger, "demo", attempt_id, _StubOutcome(), 1.0)

    assert summary["durations_ms"] == {"commit": 12.3}
    assert summary["test_runs"] == {"related": 2}
