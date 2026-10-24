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
from engine.bootstrap import BootstrapResult
from engine.config import EngineConfig, ProjectConfig, ReleaseConfig, write_json
from engine.evidence import fingerprint, record
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Event, Ledger
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


def _fake_install_cmd() -> list[str]:
    """A local, offline stand-in for a real `npm install`: writes
    node_modules/.keep and package-lock.json directly in the cwd
    instead of reaching the real npm registry. These unit tests
    exercise gb2's own bootstrap/worktree/commit wiring, not a real
    package manager's network behavior -- that is covered once, for
    real, by test_installers.py's own (network-independent) checks."""
    return [
        sys.executable, "-c",
        "import pathlib\n"
        "pathlib.Path('node_modules').mkdir(exist_ok=True)\n"
        "(pathlib.Path('node_modules') / '.keep').write_text('')\n"
        "pathlib.Path('package-lock.json').write_text('{}\\n')\n",
    ]


def _patch_fake_install_cmd(module: ModuleType) -> None:
    """Wrap `module.render_config` so every config a test in this file
    renders for a fresh app gets `_fake_install_cmd()` in place of the
    template's real `npm install` -- applied once, in the `gb2`
    fixture, so every test here that drives `cmd_init` end to end
    never reaches the network. The original is kept at
    `module._real_render_config` for the one named exception
    (test_cmd_init_bootstraps_the_source_repo_and_journals_it) that
    checks the real npm argv -- it restores this via monkeypatch and
    pairs it with a fake `npm` on PATH instead, so even that test never
    reaches the network."""
    real_render_config = module.render_config

    def _fake_render_config(template: dict[str, object],
                            app_name: str) -> dict[str, object]:
        rendered = real_render_config(template, app_name)
        rendered["projects"][app_name]["install_cmd"] = _fake_install_cmd()
        return rendered

    module.render_config = _fake_render_config
    module._real_render_config = real_render_config


@pytest.fixture(scope="module")
def gb2() -> ModuleType:
    module = _load_gb2()
    _patch_fake_install_cmd(module)
    return module


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
    # another change: engine.release.job_certificate_status now requires a
    # job-level certificate (acceptance AND app_security, both bound to
    # this exact fingerprint) before advance_master/call_publisher let a
    # release through -- this helper simulates a job that already has
    # one (what a real engine.full_orbit job would have recorded before
    # ever reaching engine.release.prepare), not the `gb2 run` -> `gb2
    # ship` bypass path that check exists to close.
    ledger.append(Event(
        project="demo", kind="acceptance.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": "job1", "passed": True, "scenes": [],
                 "full_suite_passed": True, "tree_changed": False,
                 "reason": None, "fingerprint": fp},
    ))
    ledger.append(Event(
        project="demo", kind="app_security.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"passed": True, "findings": [], "ran": ["secret_scan"],
                 "fingerprint": fp},
    ))

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


def _setup_release_job_with_failing_full_bench(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Builds on _setup_release_job, then invalidates its pre-recorded
    PASSING "full" bench evidence and reconfigures the project's
    test_cmd to genuinely fail -- so a later `gb2 ship`'s own
    ensure_full_bench call cannot trust stale evidence and actually
    re-runs (and fails) the full suite, the exact precondition the
    ordering-bug regression tests below need."""
    from engine.evidence import invalidate_after

    job = _setup_release_job(gb2, tmp_path, monkeypatch)
    workspace = job["config"].ledger_path.parent
    failing_project = replace(
        job["config"].projects["demo"],
        test_cmd=(sys.executable, "-c", "import sys; sys.exit(1)"),
    )
    failing_config = replace(
        job["config"], projects={**job["config"].projects, "demo": failing_project},
    )
    failing_config = write_json(failing_config, workspace / "config.json")

    ledger = Ledger.open(failing_config.ledger_path)
    invalidate_after(ledger, "demo", job["attempt_id"], ("bench",))
    ledger.close()

    job["config"] = failing_config
    return job


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
    # cancel_pending_publish fails it outright before the queue ever
    # drains -- "no_approval" would also have been the outcome here
    # (nothing ever granted an approval), but skip-ship no longer
    # relies on that happening naturally.
    assert req.result == {"reason": "skip_ship"}
    assert ledger.project("demo").state == "CLOSED"
    ledger.close()
    assert not job["deploy_log"].exists()
    assert release.head_sha(job["source_repo"]) != job["sha"]


# ---------------------------------------------------------------------------
# P0 regression: a publish request left 'running' by an interrupt BEFORE
# the deploy was ever dispatched (e.g. a KeyboardInterrupt during
# record_source, strictly before call_publisher's own journal entry
# exists) must not be left for report -> close's own queue processing to
# silently RESUME and deploy -- `gb2 skip-ship` must either cancel it
# outright (safe: never dispatched) or refuse and point at `gb2 resolve`
# (unsafe: may already be mid-dispatch).
# ---------------------------------------------------------------------------

def test_skip_ship_cancels_a_publish_interrupted_before_dispatch_and_closes(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approval granted, then a KeyboardInterrupt lands inside
    record_source (strictly before call_publisher's own journal entry
    is ever written) -- the request is left 'running' with no
    call_publisher entry at all and no receipt on disk. `gb2 skip-ship`
    must recognize this as safe to cancel outright and still finish
    report -> close, WITHOUT ever calling the real deploy script."""
    import engine.steps.publish as publish_steps

    job = _setup_release_job(gb2, tmp_path, monkeypatch)
    ledger = Ledger.open(job["config"].ledger_path)
    grant(ledger, "demo", job["request_id"], ttl_sec=3600)

    original_record = publish_steps.record
    state = {"raised": False}

    def fake_record(*args, **kwargs):
        if not state["raised"]:
            state["raised"] = True
            raise KeyboardInterrupt()
        return original_record(*args, **kwargs)

    monkeypatch.setattr(publish_steps, "record", fake_record)

    fakes = gb2._build_fakes(job["config"], "demo", job["attempt_id"], None)
    proc = Processor(job["config"], ledger, fakes)
    proc.register_defaults()
    with pytest.raises(KeyboardInterrupt):
        proc.run_once("demo")

    req = ledger.request(job["request_id"])
    assert req.status == "running"  # interrupt escaped uncaught
    steps = ledger.steps(job["request_id"])
    assert steps["record_source"].status == "started"  # never reached 'done'
    assert "call_publisher" not in steps  # dispatch step never even started
    ledger.close()

    code = gb2.main(["skip-ship", "demo"])

    assert code == 0
    assert not job["deploy_log"].exists()  # the real deploy script never ran
    ledger = Ledger.open(job["config"].ledger_path)
    req = ledger.request(job["request_id"])
    assert req.status == "failed"
    assert req.result == {"reason": "cancelled_before_dispatch"}
    assert ledger.project("demo").state == "CLOSED"
    ledger.close()


def test_skip_ship_refuses_a_publish_interrupted_after_dispatch_started(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same setup, but the KeyboardInterrupt instead lands while
    ShellPublisher.publish is actually waiting on the real deploy
    command -- call_publisher's own journal entry exists ('started')
    and a receipt is already on disk (status "unknown", engine.
    publishers.ShellPublisher's own interrupt handling). `gb2
    skip-ship` must refuse outright (never force-cancel this) and
    point the owner at `gb2 resolve` instead -- and must never call
    the real deploy script a second time."""
    import subprocess as subprocess_module

    job = _setup_release_job(gb2, tmp_path, monkeypatch)
    ledger = Ledger.open(job["config"].ledger_path)
    grant(ledger, "demo", job["request_id"], ttl_sec=3600)

    original_communicate = subprocess_module.Popen.communicate
    deploy_marker = str(job["config"].publisher_argv[1])
    state = {"interrupted": False}

    def fake_communicate(self, *args, **kwargs):
        argv = [str(a) for a in self.args] if isinstance(self.args, (list, tuple)) else []
        if not state["interrupted"] and deploy_marker in argv:
            state["interrupted"] = True
            self.wait(timeout=1)  # let the near-instant fake script actually finish
            raise KeyboardInterrupt()
        return original_communicate(self, *args, **kwargs)

    monkeypatch.setattr(subprocess_module.Popen, "communicate", fake_communicate)

    fakes = gb2._build_fakes(job["config"], "demo", job["attempt_id"], None)
    proc = Processor(job["config"], ledger, fakes)
    proc.register_defaults()
    with pytest.raises(KeyboardInterrupt):
        proc.run_once("demo")

    req = ledger.request(job["request_id"])
    assert req.status == "running"
    assert ledger.steps(job["request_id"])["call_publisher"].status == "started"
    receipt_path = job["config"].home / "publish" / f"{job['request_id']}.json"
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "unknown"
    ledger.close()
    deploy_log_text = job["deploy_log"].read_text(encoding="utf-8")  # ran exactly once

    code = gb2.main(["skip-ship", "demo"])

    assert code == 3
    ledger = Ledger.open(job["config"].ledger_path)
    req = ledger.request(job["request_id"])
    assert req.status == "running"  # untouched -- never force-cancelled
    assert ledger.project("demo").state == "OPEN"  # never closed
    ledger.close()
    # never redeployed -- the deploy log is byte-for-byte what the one
    # real dispatch already wrote.
    assert job["deploy_log"].read_text(encoding="utf-8") == deploy_log_text


# ---------------------------------------------------------------------------
# P0 regression: a ship whose full test suite fails must leave NOTHING a
# later `gb2 skip-ship`/`gb2 fix` queue drain could ever dispatch and
# deploy -- the exact ordering bug engine.release.approve_and_run used to
# have (grant() before ensure_full_bench).
# ---------------------------------------------------------------------------

def test_ship_full_bench_failure_then_skip_ship_never_deploys_and_closes(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine import release

    job = _setup_release_job_with_failing_full_bench(gb2, tmp_path, monkeypatch)
    master_before = release.head_sha(job["source_repo"])
    _force_tty_approval(monkeypatch, f"승인 {job['request_id']}")

    code_ship = gb2.main(["ship", "demo"])

    assert code_ship == 3
    assert not job["deploy_log"].exists()
    ledger = Ledger.open(job["config"].ledger_path)
    req = ledger.request(job["request_id"])
    assert req.status == "failed"
    assert req.result == {"reason": "full_bench_failed"}
    assert ledger.approvals_for(job["request_id"]) == []  # never granted
    ledger.close()

    code_skip = gb2.main(["skip-ship", "demo"])

    assert code_skip == 0
    assert not job["deploy_log"].exists()  # publisher called 0 times, start to finish
    ledger = Ledger.open(job["config"].ledger_path)
    assert ledger.request(job["request_id"]).status == "failed"
    assert ledger.project("demo").state == "CLOSED"
    ledger.close()
    assert release.head_sha(job["source_repo"]) == master_before  # master unchanged


def test_ship_full_bench_failure_then_simulated_fix_drain_never_deploys(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same scenario, but instead of `gb2 skip-ship`, simulates the
    queue-drain `gb2 fix` (engine.fixup.fix) ends up performing: a
    fresh Processor's run_once over the whole project queue."""
    from engine import release

    job = _setup_release_job_with_failing_full_bench(gb2, tmp_path, monkeypatch)
    master_before = release.head_sha(job["source_repo"])
    _force_tty_approval(monkeypatch, f"승인 {job['request_id']}")

    code_ship = gb2.main(["ship", "demo"])
    assert code_ship == 3
    assert not job["deploy_log"].exists()

    ledger = Ledger.open(job["config"].ledger_path)
    fakes = gb2._build_fakes(job["config"], "demo", job["attempt_id"], None)
    proc = Processor(job["config"], ledger, fakes)
    proc.register_defaults()
    n = proc.run_once("demo")

    assert n == 0  # nothing left queued -- `ship` already failed it outright
    req = ledger.request(job["request_id"])
    assert req.status == "failed"
    assert req.result == {"reason": "full_bench_failed"}
    ledger.close()
    assert not job["deploy_log"].exists()  # publisher called 0 times
    assert release.head_sha(job["source_repo"]) == master_before  # master unchanged


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
        gb2._ensure_worktree(source_repo, unrelated_dir, "master")


def test_ensure_worktree_accepts_its_own_previously_created_worktree(
    tmp_path: Path, gb2: ModuleType,
) -> None:
    source_repo = _make_source_repo(tmp_path / "source" / "demo")
    repo_dir = tmp_path / "apps" / "demo" / "repo"
    gb2._ensure_worktree(source_repo, repo_dir, "master")  # creates it

    gb2._ensure_worktree(source_repo, repo_dir, "master")  # re-entrant: must not raise


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
# Bootstrap: `_setup_app_workspace` installs the SOURCE repo's own
# dependencies via engine.bootstrap once the ledger is open, and a
# failed install stops cold with exit 2 (never continuing on to run a
# job against a repo whose npm install never actually succeeded).
# ---------------------------------------------------------------------------

def test_cmd_init_bootstraps_the_source_repo_and_journals_it(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ONE test in this file that checks the config's real npm
    install_cmd (every other test gets `_fake_install_cmd()` via the
    `gb2` fixture's render_config patch) -- restores the real
    render_config here, and pairs it with a fake `npm` on PATH (same
    technique as test_installers.py's own argv-logging tests) so this
    still never reaches the real network."""
    apps_root = tmp_path / "source"
    source_repo = _make_source_repo(apps_root / "demo")
    (source_repo / "package.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(gb2, "APPS_DIR", tmp_path / "ws" / "apps")
    monkeypatch.setenv("GB2_APPS_ROOT", str(apps_root))
    monkeypatch.setattr(gb2, "_copy_node_modules", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb2, "render_config", gb2._real_render_config)

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_npm = fake_bin / "npm"
    fake_npm.write_text(
        "#!/bin/sh\nmkdir -p node_modules\necho '{}' > package-lock.json\n",
        encoding="utf-8",
    )
    fake_npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    calls: list[tuple] = []
    real_ensure_installed = gb2.ensure_installed

    def _spy_ensure_installed(repo, install_cmd, **kwargs):
        calls.append((repo, install_cmd))
        return real_ensure_installed(repo, install_cmd, **kwargs)

    monkeypatch.setattr(gb2, "ensure_installed", _spy_ensure_installed)
    args = gb2._build_parser().parse_args(["init", "demo"])

    code = gb2.cmd_init(args)

    assert code == 0
    assert len(calls) == 1
    called_repo, called_install_cmd = calls[0]
    assert called_repo == source_repo.resolve()
    assert called_install_cmd == ("npm", "install", "--no-audit", "--no-fund")

    config = EngineConfig.from_json(tmp_path / "ws" / "apps" / "demo" / "config.json")
    ledger = Ledger.open(config.ledger_path)
    try:
        events = ledger.events("demo", "dependency.installed")
    finally:
        ledger.close()
    assert len(events) == 1
    assert events[0][1].payload["ok"] is True


def test_cmd_init_exits_2_when_bootstrap_install_fails(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed dependency install must stop with exit code 2 (never the
    generic exit 1 a config mistake gets) -- and still journal the
    attempt via record_bootstrap before raising, so the ledger shows
    WHY, not just that nothing ran afterward."""
    apps_root = tmp_path / "source"
    source_repo = _make_source_repo(apps_root / "demo")
    (source_repo / "package.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(gb2, "APPS_DIR", tmp_path / "ws" / "apps")
    monkeypatch.setenv("GB2_APPS_ROOT", str(apps_root))
    monkeypatch.setattr(gb2, "_copy_node_modules", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb2, "ensure_installed", lambda repo, install_cmd, **kwargs:
                        BootstrapResult(ran=True, ok=False, detail="install_failed:boom"))
    args = gb2._build_parser().parse_args(["init", "demo"])

    code = gb2.cmd_init(args)

    assert code == 2
    # the workspace config was still written (the failure is reported,
    # not silently swallowed), but the job itself never ran -- there is
    # no later caller here at all, by construction (cmd_init never calls
    # run_full_job); the real "nothing continues on top of a failed
    # bootstrap" guarantee is exercised by `gb2 new`'s own version of
    # this test, where a real orbit call would otherwise follow.
    config = EngineConfig.from_json(tmp_path / "ws" / "apps" / "demo" / "config.json")
    ledger = Ledger.open(config.ledger_path)
    try:
        events = ledger.events("demo", "dependency.installed")
    finally:
        ledger.close()
    assert len(events) == 1
    assert events[0][1].payload["ok"] is False
    assert "boom" in events[0][1].payload["detail"]


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


def test_detect_default_branch_prefers_origin_head(
    tmp_path: Path, gb2: ModuleType,
) -> None:
    source = _make_source_repo(tmp_path / "source")
    remote = _make_source_repo(tmp_path / "remote")
    _rgit(remote, "branch", "-m", "trunk")
    _rgit(source, "remote", "add", "origin", str(remote))
    _rgit(source, "fetch", "-q", "origin")
    _rgit(source, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")

    assert gb2._detect_default_branch(source) == "trunk"


def test_detect_default_branch_falls_back_to_current_head_branch(
    tmp_path: Path, gb2: ModuleType,
) -> None:
    source = tmp_path / "source" / "demo"
    source.mkdir(parents=True)
    _rgit(source, "init", "-q", "-b", "main")
    (source / "app.py").write_text("v1\n", encoding="utf-8")
    _rgit(source, "add", "-A")
    _rgit(source, "commit", "-q", "-m", "init")

    assert gb2._detect_default_branch(source) == "main"


def test_detect_default_branch_falls_back_to_main_or_master_when_detached(
    tmp_path: Path, gb2: ModuleType,
) -> None:
    source = tmp_path / "source" / "demo"
    source.mkdir(parents=True)
    _rgit(source, "init", "-q", "-b", "main")
    (source / "app.py").write_text("v1\n", encoding="utf-8")
    _rgit(source, "add", "-A")
    _rgit(source, "commit", "-q", "-m", "init")
    head = _rgit(source, "rev-parse", "HEAD")
    _rgit(source, "checkout", "-q", head)  # detach HEAD

    assert gb2._detect_default_branch(source) == "main"


def test_cmd_init_against_a_main_only_repo_uses_main_as_worktree_base(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source repo whose only branch is "main" (never "master") must
    have its gb2/work worktree branched off "main", with that detected
    base recorded for `gb2 sync`/`gb2 diff` to reuse -- the hardcoded
    WORKTREE_BASE ("master") must never be assumed."""
    apps_root = tmp_path / "source"
    source_repo = apps_root / "demo"
    source_repo.mkdir(parents=True)
    _rgit(source_repo, "init", "-q", "-b", "main")
    (source_repo / "package.json").write_text("{}\n", encoding="utf-8")
    _rgit(source_repo, "add", "-A")
    _rgit(source_repo, "commit", "-q", "-m", "init")
    monkeypatch.setattr(gb2, "APPS_DIR", tmp_path / "ws" / "apps")
    monkeypatch.setenv("GB2_APPS_ROOT", str(apps_root))
    args = gb2._build_parser().parse_args(["init", "demo"])

    code = gb2.cmd_init(args)

    assert code == 0
    assert gb2._read_worktree_base("demo") == "main"
    repo_dir = tmp_path / "ws" / "apps" / "demo" / "repo"
    branch = _rgit(repo_dir, "branch", "--show-current")
    assert branch == "gb2/work"
    merge_base = _rgit(source_repo, "merge-base", "main", "gb2/work")
    main_head = _rgit(source_repo, "rev-parse", "main")
    assert merge_base == main_head  # gb2/work branched off main, not a nonexistent master


# ---------------------------------------------------------------------------
# gb2 doctor
# ---------------------------------------------------------------------------

def test_doctor_cmd_present_reports_found_path(
    gb2: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gb2.shutil, "which", lambda name: "/usr/bin/" + name)
    ok, detail = gb2._doctor_cmd_present("git")
    assert ok is True
    assert detail == "/usr/bin/git"


def test_doctor_cmd_present_reports_missing(
    gb2: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gb2.shutil, "which", lambda name: None)
    ok, detail = gb2._doctor_cmd_present("codex")
    assert ok is False
    assert "not found" in detail


def test_doctor_bwrap_runs_reports_missing_binary(
    gb2: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gb2.shutil, "which", lambda name: None)
    ok, detail = gb2._doctor_bwrap_runs()
    assert ok is False
    assert "not found" in detail


def test_doctor_bwrap_runs_actually_executes_it(gb2: ModuleType) -> None:
    """Real environment check -- xfail-free: bwrap not being on this
    machine's PATH is itself a legitimate "MISSING" result, not a test
    failure, since doctor's whole job is to report that accurately."""
    ok, detail = gb2._doctor_bwrap_runs()
    assert isinstance(ok, bool)
    assert isinstance(detail, str)


def test_doctor_release_apps_lists_only_apps_with_release_configured(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = tmp_path / "ws" / "apps"
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    (apps_dir / "with-release").mkdir(parents=True)
    (apps_dir / "with-release" / "config.json").write_text(json.dumps({
        "projects": {"with-release": {"repo": "repo", "release": {
            "master_repo": "/x", "target": "with-release"}}},
    }), encoding="utf-8")
    (apps_dir / "no-release").mkdir(parents=True)
    (apps_dir / "no-release" / "config.json").write_text(json.dumps({
        "projects": {"no-release": {"repo": "repo"}},
    }), encoding="utf-8")

    assert gb2._doctor_release_apps() == ["with-release"]


def test_doctor_release_apps_excludes_an_explicit_null_release(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The node-vitest template always writes "release": null for an app
    scaffolded WITHOUT --with-release (tools/templates/node-vitest.json)
    -- a plain `"release" in p` key-presence check would wrongly count
    EVERY such app as release-configured and demand a deploy script for
    it. Only a non-null "release" value counts."""
    apps_dir = tmp_path / "ws" / "apps"
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    (apps_dir / "no-release").mkdir(parents=True)
    (apps_dir / "no-release" / "config.json").write_text(json.dumps({
        "projects": {"no-release": {"repo": "repo", "release": None}},
    }), encoding="utf-8")

    assert gb2._doctor_release_apps() == []


def test_cmd_doctor_exit_1_when_something_required_is_missing(
    gb2: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(gb2.shutil, "which", lambda name: None)
    monkeypatch.delenv("GB2_APPS_ROOT", raising=False)
    args = gb2._build_parser().parse_args(["doctor"])

    code = gb2.cmd_doctor(args)

    assert code == 1
    out = capsys.readouterr().out
    assert "MISSING" in out
    assert "git" in out


def test_cmd_doctor_exit_0_when_everything_present(
    gb2: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gb2.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(gb2, "_doctor_bwrap_runs", lambda: (True, "ok"))
    monkeypatch.setattr(gb2, "_doctor_release_apps", lambda: [])
    monkeypatch.setenv("GB2_APPS_ROOT", "/tmp/apps-root")
    args = gb2._build_parser().parse_args(["doctor"])

    code = gb2.cmd_doctor(args)

    assert code == 0


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


def test_resolve_handles_a_publish_left_running_by_a_ctrl_c_interrupt(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KeyboardInterrupt hitting `gb2 ship` while it waits on the
    deploy (e.g. the owner hitting ctrl-C) leaves the LEDGER's own
    request status 'running' -- the interrupt escapes run_once/
    approve_and_run untouched, never caught as a terminal/unknown
    status -- even though ShellPublisher's own receipt already says
    "unknown" (engine.publishers.ShellPublisher's own KeyboardInterrupt
    handling). `gb2 resolve` must still find and resolve this, not
    just a request the ledger itself already marked 'unknown', and
    must never re-run the real deploy command."""
    import subprocess as subprocess_module

    job = _setup_release_job(gb2, tmp_path, monkeypatch)
    check_log = tmp_path / "check.log"
    check_script = tmp_path / "logging_check.py"
    check_script.write_text(
        f"import sys\nopen({str(check_log)!r}, 'w').write('ran')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    config = replace(job["config"], publisher_dry_run_argv=(sys.executable, str(check_script)))
    config = write_json(config, job["config"].ledger_path.parent / "config.json")
    job["config"] = config

    ledger = Ledger.open(config.ledger_path)
    grant(ledger, "demo", job["request_id"], ttl_sec=3600)

    # Interrupt only the ONE Popen that is the real deploy dispatch
    # itself (ShellPublisher.publish's own Popen(argv).communicate())
    # -- every earlier git subprocess call this run_once also makes
    # (check_artifact, advance_master, ...) must run normally.
    original_communicate = subprocess_module.Popen.communicate
    deploy_marker = str(job["config"].publisher_argv[1])  # the fake deploy script's own path
    state = {"interrupted": False}

    def fake_communicate(self, *args, **kwargs):
        argv = [str(a) for a in self.args] if isinstance(self.args, (list, tuple)) else []
        if not state["interrupted"] and deploy_marker in argv:
            state["interrupted"] = True
            # Let the (near-instant) fake deploy script actually finish
            # writing before the simulated ctrl-C lands -- communicate()
            # itself is what we're interrupting, not the real subprocess.
            self.wait(timeout=1)
            raise KeyboardInterrupt()
        return original_communicate(self, *args, **kwargs)

    monkeypatch.setattr(subprocess_module.Popen, "communicate", fake_communicate)

    fakes = gb2._build_fakes(config, "demo", job["attempt_id"], None)
    proc = Processor(config, ledger, fakes)
    proc.register_defaults()
    with pytest.raises(KeyboardInterrupt):
        proc.run_once("demo")

    req = ledger.request(job["request_id"])
    assert req.status == "running"  # never flipped to a terminal/unknown status
    receipt_path = config.home / "publish" / f"{job['request_id']}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "unknown"  # ShellPublisher's own interrupt handling
    ledger.close()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "not deployed")

    code = gb2.main(["resolve", "demo"])

    assert code == 0
    assert check_log.exists()  # the check command DID run (just to inform the owner)
    ledger = Ledger.open(config.ledger_path)
    assert ledger.request(job["request_id"]).status == "failed"
    assert ledger.project("demo").state == "CLOSED"
    ledger.close()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    # the deploy script ran exactly once in total -- the dispatch
    # ShellPublisher.publish started before the interrupt -- resolve
    # never re-ran it.
    assert job["deploy_log"].read_text(encoding="utf-8") == "demo"


def test_resolve_refuses_while_a_live_processor_holds_the_project_lock(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publish request left 'running' must NOT be treated as
    resolvable while some OTHER live processor could still be the one
    mid-dispatching it -- _project_lock_is_free must see a live holder
    and refuse before ever looking at the receipt's pgid."""
    job = _setup_release_job(gb2, tmp_path, monkeypatch)
    ledger = Ledger.open(job["config"].ledger_path)
    grant(ledger, "demo", job["request_id"], ttl_sec=3600)
    ledger.append(gb2.Event(
        project="demo", kind="request.status", actor_kind="processor",
        actor_id="p", attempt_id=job["attempt_id"],
        payload={"request_id": job["request_id"], "status": "running", "result": {}},
    ))
    assert ledger.acquire_lock("demo", "some-other-live-processor") is True
    ledger.close()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    code = gb2.main(["resolve", "demo"])

    assert code == 1
    ledger = Ledger.open(job["config"].ledger_path)
    assert ledger.request(job["request_id"]).status == "running"  # untouched
    ledger.close()
    assert not job["deploy_log"].exists()


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


# ---------------------------------------------------------------------------
# _commit_generated_lockfile: only commits package-lock.json, only when
# THIS bootstrap call actually changed it, and never sweeps up something
# else the owner had already staged.
# ---------------------------------------------------------------------------

def test_commit_generated_lockfile_leaves_a_pre_staged_other_file_staged(
    gb2: ModuleType, tmp_path: Path,
) -> None:
    """Regression: `git commit` with no pathspec commits the WHOLE
    index, not just package-lock.json -- an owner who had already run
    `git add some_other_file` before `gb2 init` would find that file
    silently swept into gb2's own bootstrap commit. Here the lockfile
    genuinely changed (before != after, as a real `npm install` would
    leave it) but some_other_file must stay staged and uncommitted."""
    repo = _make_source_repo(tmp_path / "repo")
    before = gb2._lockfile_snapshot(repo)
    assert before is None

    (repo / "some_other_file.txt").write_text("owner's own work\n", encoding="utf-8")
    _rgit(repo, "add", "some_other_file.txt")
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")

    gb2._commit_generated_lockfile(repo, before)

    head_files = _rgit(repo, "show", "--name-only", "--pretty=format:", "HEAD").strip().splitlines()
    assert head_files == ["package-lock.json"]
    status = _rgit(repo, "status", "--porcelain")
    assert "A  some_other_file.txt" in status  # still staged, never committed


def test_commit_generated_lockfile_is_a_noop_when_bootstrap_did_not_touch_it(
    gb2: ModuleType, tmp_path: Path,
) -> None:
    """ensure_installed found node_modules already installed (nothing
    ran) -- the lockfile is unchanged from the snapshot taken right
    before that call, so this must not stage or commit anything, even
    if the file happens to be sitting there dirty for some unrelated
    reason."""
    repo = _make_source_repo(tmp_path / "repo")
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    _rgit(repo, "add", "package-lock.json")
    _rgit(repo, "commit", "-q", "-m", "lockfile already committed")
    before = gb2._lockfile_snapshot(repo)
    head_before = _rgit(repo, "rev-parse", "HEAD")

    # No ensure_installed call ran in between (nothing changed the
    # file from `before`) -- the exact state after a "node_modules
    # already installed" bootstrap result.
    gb2._commit_generated_lockfile(repo, before)

    assert _rgit(repo, "rev-parse", "HEAD") == head_before  # no new commit


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


# ---------------------------------------------------------------------------
# approve-install for an EARLY station (facts/probe/plan/design) stop --
# regression for engine.full_orbit.pending_owner_requests only ever
# checking STAGE_TASK_OWNER: an install request a facts/probe/plan/design
# station asked for (engine.full_orbit.STAGE_EARLY_STATION) used to be
# invisible to `gb2 approve-install` ("no_pending_install_request" even
# though a request was genuinely pending), so an owner had no way to grant
# it and `gb2 continue` waited forever. The design station is used here
# because resuming past its own install request lands on the next real
# stop (STAGE_DESIGN_APPROVAL) without needing a builder/reviewer script
# too -- the real `gb2 approve-install` command (TTY-forced) drives the
# approval/drain/resume itself; only the FIRST run_full_job call (before
# any owner request exists to approve) is stubbed at the Processor level,
# the same way tests/unit/test_full_orbit.py's own early-station tests
# are.
# ---------------------------------------------------------------------------

_EARLY_DESIGN_INSTALL_REQUEST = {
    "kind": "install", "manifest": "package.json",
    "change": {"dependencies": {"left-pad": "^1.0.0"}},
}

_EARLY_DESIGN_PLAN_JSON = json.dumps({
    "goal": "화면 하나", "ui": True, "scenes": [],
    "tasks": [{"id": "t1", "title": "첫 파일", "detail": "",
              "owns": ["src/extra1.py"], "depends_on": []}],
    "acceptance": ["요구사항대로 동작한다"],
})

_EARLY_DESIGN_STATION_OWNERS = {
    "facts": {"facts": (".gongbang/",)},
    "probe": {"prober": (".gongbang/",)},
    "plan": {"planner": (".gongbang/",)},
    "design": {"designer": (".gongbang/",)},
    "build": {"builder": ("src/",)},
    "review": {"reviewer": ("review.md",)},
}


def _setup_early_design_install_job(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """A gb2 "demo" workspace whose only attempt is already stopped at
    engine.full_orbit.STAGE_EARLY_STATION (station "design") with one
    pending install request -- the FIRST run_full_job call drives
    facts/plan/design through a Processor-level stub (no real worker
    process, same technique as test_full_orbit.py's
    _station_stub_with_one_owner_request), stopping right after the
    design station's own "requests" entry is recorded. The resumed
    design station call (after approval) is left to a REAL run_station
    via GB2_SCRIPTED_WORKERS (see test_approve_install_... below) --
    this helper only gets the job to the owner-request stop."""
    from engine import full_orbit
    from engine.config import EngineConfig, ProjectConfig, WorkerConfig, write_json
    from engine.fakes.installer import FakeInstaller
    from engine.fakes.publisher import FakePublisher
    from engine.ledger import Event, Ledger
    from engine.processor import Fakes, Processor, StepContext, StepDef
    from engine.steps.open_job import copy_root, dirty_paths

    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    monkeypatch.setattr(gb2, "APPS_DIR", apps_dir)
    monkeypatch.setenv("GB2_APPS_ROOT", str(tmp_path / "source"))
    workspace = apps_dir / "demo"
    workspace.mkdir()
    (workspace / "home").mkdir()
    (workspace / "secrets").mkdir()

    source_repo = _make_source_repo(tmp_path / "source" / "demo")
    (source_repo / "package.json").write_text("{}\n", encoding="utf-8")
    repo_dir = _worktree_repo(source_repo, workspace / "repo")

    config = EngineConfig(
        ledger_path=workspace / "ledger.sqlite",
        home=workspace / "home",
        secrets_dir=workspace / "secrets",
        projects={"demo": ProjectConfig(
            "demo", repo_dir, install_cmd=tuple(_fake_install_cmd()),
            manifest="package.json",
        )},
        station_owners=_EARLY_DESIGN_STATION_OWNERS,
        approval_ttl_sec=3600,
        workers={"designer": WorkerConfig(runner="claude")},
    )
    config = write_json(config, workspace / "config.json")

    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("demo")
    job_name = "early-design-job"

    queues = {
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n"}],
        "plan": [{f".gongbang/{job_name}/plan.json": _EARLY_DESIGN_PLAN_JSON}],
    }

    def steps(ctx: StepContext) -> list[StepDef]:
        station = str(ctx.request.payload.get("station"))
        role = str(ctx.request.payload.get("role"))

        if station == "design":
            def _run_design(ctx: StepContext) -> dict[str, object]:
                ctx.ledger.append(Event(
                    project=ctx.project, kind="station.finished", actor_kind="processor",
                    actor_id="processor", attempt_id=ctx.attempt_id,
                    payload={
                        "request_id": ctx.request.request_id, "station": "design",
                        "role": role, "ok": True, "summary": "stub", "cost_usd": 0.0,
                        "turns": 1, "changed_paths": [],
                        "requests": [_EARLY_DESIGN_INSTALL_REQUEST],
                    },
                ))
                return {}

            def _reconcile_design(ctx: StepContext) -> bool:
                for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
                    if event.payload.get("request_id") == ctx.request.request_id:
                        return True
                return False

            return [StepDef("record_station", _run_design, _reconcile_design)]

        def _run(ctx: StepContext) -> dict[str, object]:
            copy = copy_root(ctx.config, ctx.attempt_id, role)
            files = queues[station].pop(0)
            for rel, content in files.items():
                target = copy / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            changed = dirty_paths(copy, ())
            ctx.ledger.append(Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": station, "role": role,
                    "ok": True, "summary": "stub", "cost_usd": 0.0, "turns": 1,
                    "changed_paths": list(changed), "requests": [],
                },
            ))
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
                if event.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_station", _run, _reconcile)]

    def _review_always_passes(ctx: StepContext) -> list[StepDef]:
        from engine import evidence

        def _run_review(ctx: StepContext) -> dict[str, object]:
            project_config = ctx.config.projects[ctx.project]
            fp = evidence.fingerprint(project_config.repo, project_config.excludes)
            evidence.record(
                ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
                payload={"passed": True, "request_id": ctx.request.request_id, "findings": []},
            )
            return {}

        def _reconcile_review(ctx: StepContext) -> bool:
            for row in ctx.ledger.evidence(ctx.attempt_id, "review"):
                if row.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_review", _run_review, _reconcile_review)]

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps)
        proc.register("review", _review_always_passes)
        return proc

    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller())
    outcome = full_orbit.run_full_job(
        config, ledger, fakes, "demo", job_name, "화면 하나 만들어줘",
        processor_factory=factory,
    )
    assert outcome.status == "needs_owner"
    assert outcome.reason == "owner_request"
    ledger.close()

    return {"config": config, "source_repo": source_repo, "repo_dir": repo_dir,
            "job_name": job_name, "attempt_id": outcome.attempt_id}


def test_approve_install_finds_and_resumes_an_early_station_request(
    gb2: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _setup_early_design_install_job(gb2, tmp_path, monkeypatch)
    _force_tty_approval(monkeypatch, f"승인 {job['job_name']}")

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    _write_script = lambda path, actions: path.write_text(  # noqa: E731
        json.dumps(actions, ensure_ascii=False), encoding="utf-8")
    _write_script(scripts_dir / "designer.json", [
        {"tool": "Write", "file_path": f".gongbang/{job['job_name']}/design/design.md",
         "content": "디자인\n"},
        {"final": {"status": "done", "summary": "design", "requests": []}},
    ])
    monkeypatch.setenv("GB2_SCRIPTED_WORKERS", str(scripts_dir))

    code = gb2.main(["approve-install", "demo"])

    # Before the fix, pending_owner_requests never saw a STAGE_EARLY_STATION
    # stop, so this always failed with "no_pending_install_request" (exit
    # 1) and the install was never approved. Now it finds the request,
    # approves it, drains the install, and resumes the design station --
    # landing on the NEXT real stop (design approval), never "done"
    # outright (nobody approved the design itself) and never stuck.
    assert code == 2, code

    ledger = Ledger.open(job["config"].ledger_path)
    try:
        install_events = ledger.events("demo", "install.recorded")
        progress = None
        for _seq, event in ledger.events("demo", "full_orbit.progress"):
            if event.payload.get("job") == job["job_name"]:
                progress = event.payload
    finally:
        ledger.close()
    assert len(install_events) == 1
    assert install_events[0][1].payload["change"] == _EARLY_DESIGN_INSTALL_REQUEST["change"]
    assert progress is not None
    assert progress["stage"] == "awaiting_design_approval"
