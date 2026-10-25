"""T5 -- the speed contract for `gb2 change <app> "<request>" --small`
(engine.small_orbit): a real `gb2` subprocess, against the same real
Node/vitest template + bwrap sandbox T3 (test_t3_gb2_node_e2e.py)
already exercises, proving two things T3 alone cannot:

  1. `gb2 change --small` on a one-task, no-external-dependency request
     actually runs FEWER worker calls than the ordinary `gb2 change`
     full job for an equivalent one-task request (one combined
     facts+plan call instead of two separate ones) -- while still
     landing the same file, passing the same real `npx vitest run`,
     and writing the same .gongbang/<job>/report.md 대조표.
  2. the same release gating a full job gets: with `--with-release`
     configured, `gb2 change --small` stops needs_owner/
     "release_approval" exactly like a full job does (never deploys on
     its own), and a REAL `gb2 ship` (typed into a pty, same as T3's
     own ship test) deploys it exactly once.

Helpers below are deliberately duplicated from test_t3_gb2_node_e2e.py
in trimmed, one-task form rather than imported -- see this project's
own worker contract: the two test files own disjoint station-owners
(the full job's own facts/plan vs. small mode's combined "plan"
station) and are meant to evolve independently.

Skipped (not failed) under the same two conditions test_t3_gb2_node_
e2e.py already uses -- bwrap unavailable, or GB2_TEST_NODE_MODULES
unset/missing vitest -- turned into failures under GB2_REQUIRE_T3=1,
the CI job that exists to run this test alongside T3."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from engine import sandbox
from engine.ledger import Ledger

_GB2_PATH = Path(__file__).resolve().parents[2] / "tools" / "gb2"
_REQUIRE_T5 = os.environ.get("GB2_REQUIRE_T3") == "1"

_needs_bwrap = pytest.mark.skipif(
    not _REQUIRE_T5 and not sandbox.available(), reason="bwrap not available on this host",
)


def _node_modules_source() -> Path | None:
    value = os.environ.get("GB2_TEST_NODE_MODULES", "").strip()
    if not value:
        return None
    path = Path(value)
    if not (path / ".bin" / "vitest").exists() and not (path / "vitest").is_dir():
        return None
    return path


_node_modules_src = _node_modules_source()
_needs_node_modules = pytest.mark.skipif(
    not _REQUIRE_T5 and _node_modules_src is None,
    reason="GB2_TEST_NODE_MODULES unset (or has no vitest) -- see test_t3_gb2_node_e2e.py",
)


@pytest.fixture(autouse=True)
def _t5_prereqs() -> None:
    if not _REQUIRE_T5:
        return
    if not sandbox.available():
        pytest.fail("GB2_REQUIRE_T3=1 but bwrap is not available")
    if _node_modules_src is None:
        pytest.fail("GB2_REQUIRE_T3=1 but GB2_TEST_NODE_MODULES is unset or has no vitest")


_GREETER_JS = '''\
export function greet(name) {
  return `안녕, ${name}`;
}
'''

_GREETER_TEST_JS = '''\
import { describe, expect, it } from "vitest";
import { greet } from "../src/greeter.js";

describe("greeter", () => {
  it("greets by name", () => {
    expect(greet("세다")).toBe("안녕, 세다");
  });
});
'''

_SCENE_GREET_JS = '''\
import assert from "node:assert";
import { greet } from "../../src/greeter.js";

assert.strictEqual(greet("세다"), "안녕, 세다");
'''

_FACTS_MD = '''\
# 사실 확인

| 주장 | 판정 | 출처 |
|---|---|---|
| 이름을 받아 인사말을 돌려준다 | 통과 | 요구사항 원문 |

## 코드에 넣는 값
(없음 -- 하드코딩할 값 없음)

## 외부 대상
(없음)
'''

_REQUEST = "이름을 받아 인사말을 돌려주는 greet 함수를 만들어줘"

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "t3_node_app"
_PACKAGE_LOCK_JSON = (_FIXTURES_DIR / "memo-package-lock.json").read_text(encoding="utf-8")
_FAKE_AUDIT_CLEAN = _FIXTURES_DIR / "fake_audit_clean.py"


def _plan_json() -> str:
    return json.dumps({
        "goal": "이름을 받아 인사하는 함수", "ui": False,
        "requirements": [{"id": "R1", "text": "이름을 받아 인사말을 돌려준다", "kind": "function"}],
        "scenes": [{"id": "s1", "actor_does": "이름을 넣어 인사를 받는다",
                   "app_shows": "이름이 들어간 인사말이 보인다", "where": "cli",
                   "cmd": ["node", "tests/scenes/s1_greet.js"], "covers": ["R1"]}],
        "tasks": [{"id": "t1", "title": "greet 구현", "detail": "greeter.js에 greet를 만든다",
                  "owns": ["src/greeter.js", "tests/greeter.test.js"], "depends_on": []}],
        "acceptance": ["이름을 받아 인사말을 돌려준다"],
    }, ensure_ascii=False)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_script(path: Path, actions: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(actions, ensure_ascii=False), encoding="utf-8")


_UNUSED_ROLE_SCRIPT = [{"final": {"status": "done", "summary": "unused", "requests": []}}]


def _write_full_job_scripts(scripts_dir: Path, job_name: str) -> None:
    """One script per role -- the ordinary `gb2 change` path: a
    separate facts call, then a separate plan call."""
    _write_script(scripts_dir / "facts.json", [
        {"tool": "Write", "file_path": f".gongbang/{job_name}/facts.md", "content": _FACTS_MD},
        {"final": {"status": "done", "summary": "facts", "requests": []}},
    ])
    _write_script(scripts_dir / "prober.json", _UNUSED_ROLE_SCRIPT)
    _write_script(scripts_dir / "planner.json", [
        {"tool": "Write", "file_path": f".gongbang/{job_name}/plan.json", "content": _plan_json()},
        {"tool": "Write", "file_path": "tests/scenes/s1_greet.js", "content": _SCENE_GREET_JS},
        {"final": {"status": "done", "summary": "plan", "requests": []}},
    ])
    _write_script(scripts_dir / "designer.json", _UNUSED_ROLE_SCRIPT)
    _write_script(scripts_dir / "builder.json", [
        {"tool": "Write", "file_path": "src/greeter.js", "content": _GREETER_JS},
        {"tool": "Write", "file_path": "tests/greeter.test.js", "content": _GREETER_TEST_JS},
        {"final": {"status": "done", "summary": "build", "requests": []}},
    ])
    _write_script(scripts_dir / "reviewer.json", [
        {"final": {"passed": True, "findings": [], "coverage": "full"}},
    ])


def _write_small_job_scripts(scripts_dir: Path, job_name: str) -> None:
    """Same roles, but the "planner" role writes BOTH facts.md AND
    plan.json in its ONE call -- "facts"/"prober"/"designer" are never
    dispatched at all by engine.small_orbit, but
    _apply_scripted_workers still requires a script file for every
    role config.workers defines, so these stay plain unused
    placeholders."""
    _write_script(scripts_dir / "facts.json", _UNUSED_ROLE_SCRIPT)
    _write_script(scripts_dir / "prober.json", _UNUSED_ROLE_SCRIPT)
    _write_script(scripts_dir / "planner.json", [
        {"tool": "Write", "file_path": f".gongbang/{job_name}/facts.md", "content": _FACTS_MD},
        {"tool": "Write", "file_path": f".gongbang/{job_name}/plan.json", "content": _plan_json()},
        {"tool": "Write", "file_path": "tests/scenes/s1_greet.js", "content": _SCENE_GREET_JS},
        {"final": {"status": "done", "summary": "small-plan", "requests": []}},
    ])
    _write_script(scripts_dir / "designer.json", _UNUSED_ROLE_SCRIPT)
    _write_script(scripts_dir / "builder.json", [
        {"tool": "Write", "file_path": "src/greeter.js", "content": _GREETER_JS},
        {"tool": "Write", "file_path": "tests/greeter.test.js", "content": _GREETER_TEST_JS},
        {"final": {"status": "done", "summary": "build", "requests": []}},
    ])
    _write_script(scripts_dir / "reviewer.json", [
        {"final": {"passed": True, "findings": [], "coverage": "full"}},
    ])


_DEPLOY_SCRIPT_SH = '''\
#!/bin/sh
echo "$(date +%s) $*" >> "{log_path}"
exit 0
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _run_gb2(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-P", str(_GB2_PATH), *args],
        env=env, capture_output=True, text=True, timeout=600,
    )


def _run_gb2_with_approval(
    args: list[str], env: dict[str, str], owner_text: str,
) -> subprocess.CompletedProcess:
    """Same pty technique as test_t3_gb2_node_e2e.py's own helper of
    this exact name -- `gb2 ship`'s TTY check needs a real pty stdin."""
    master_fd, slave_fd = os.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-P", str(_GB2_PATH), *args],
            env=env, stdin=slave_fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        os.close(slave_fd)
    try:
        os.write(master_fd, (owner_text + "\n").encode("utf-8"))
        stdout, stderr = proc.communicate(timeout=600)
    finally:
        os.close(master_fd)
    brace = stdout.find("{")
    if brace != -1:
        stdout = stdout[brace:]
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


@pytest.fixture()
def scripts_dir():
    """Not under tmp_path/`/tmp` -- see test_t3_gb2_node_e2e.py's own
    module docstring for why a sandboxed worker could never read a
    script living there."""
    base = Path.home() / ".cache" / "gb2-t5-acceptance"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=str(base)))
    try:
        yield path
    finally:
        import shutil

        shutil.rmtree(path, ignore_errors=True)


def _scaffold_app_source_repo(apps_root: Path, app: str, node_modules_src: Path) -> Path:
    """Trimmed, one-task-sized version of test_t3_gb2_node_e2e.py's own
    _scaffold_memo_source_repo -- see that function's own docstring for
    why each piece (lockfile, node_modules symlink, .git/info/exclude)
    is there; this one never configures release here (the caller edits
    config.json for that, same as T3's own _init_memo_workspace_with_
    audit_cmd)."""
    source_repo = apps_root / app
    source_repo.mkdir(parents=True)
    skeleton_dir = _GB2_PATH.parent / "templates" / "new-app"
    for rel in ("README.md", ".gitignore", "package.json", "tests/scenes/.gitkeep"):
        text = (skeleton_dir / rel).read_text(encoding="utf-8").replace("__APP__", app)
        _write(source_repo / rel, text)
    _write(source_repo / "package-lock.json", _PACKAGE_LOCK_JSON)
    _git(source_repo, "init", "-q", "-b", "master")
    _git(source_repo, "add", "README.md", ".gitignore", "package.json",
        "tests/scenes/.gitkeep", "package-lock.json")
    _git(source_repo, "-c", "user.name=gb2-t5-acceptance",
        "-c", "user.email=gb2-t5-acceptance@local",
        "commit", "-q", "-m", "chore: scaffold via test fixture (with lockfile)")
    (source_repo / "node_modules").symlink_to(node_modules_src, target_is_directory=True)
    (source_repo / ".git" / "info" / "exclude").write_text(
        "node_modules\n.gongbang-new\n", encoding="utf-8")
    return source_repo


def _init_app_workspace_with_audit_cmd(
    env: dict[str, str], workspace: Path, app: str, audit_script: Path, *, with_release: bool,
) -> None:
    init_result = _run_gb2(["init", app, *(["--with-release"] if with_release else [])], env)
    assert init_result.returncode == 0, (init_result.stdout, init_result.stderr)
    config_path = workspace / "apps" / app / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["security"] = {**config.get("security", {}), "audit_cmd": [sys.executable, str(audit_script)]}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _station_finished_count(workspace: Path, app: str, job_name: str) -> int:
    """How many real "station.finished" ledger events this job's own
    attempts recorded -- the actual number of worker calls (facts/
    plan/design/build, whichever ran), read straight off the ledger
    rather than inferred from file contents. Opened read-only, in its
    own `with` so it never holds the project lock a later `gb2`
    subprocess call would need."""
    from engine.config import EngineConfig

    config = EngineConfig.from_json(workspace / "apps" / app / "config.json")
    ledger = Ledger.open(config.ledger_path)
    try:
        attempt_ids = {a.attempt_id for a in ledger.attempts_for_job_name(app, job_name)}
        return sum(
            1 for _seq, event in ledger.events(app, "station.finished")
            if event.attempt_id in attempt_ids
        )
    finally:
        ledger.close()


_FACTS_MD_WITH_TARGET = '''\
# 사실 확인

| 주장 | 판정 | 출처 |
|---|---|---|
| 이름을 받아 인사말을 돌려준다 | 통과 | 요구사항 원문 |

## 코드에 넣는 값
(없음 -- 하드코딩할 값 없음)

## 외부 대상
- 날씨 API: https://example.com/weather-api
'''


def _write_small_job_scripts_with_external_target(
    scripts_dir: Path, small_job_name: str, fallback_job_name: str,
) -> None:
    """Same roles `_write_small_job_scripts` wires up, except the
    "planner" role's one combined call writes a facts.md naming a real
    "## 외부 대상" -- engine.small_orbit must refuse outright (never
    probe it for real) and close+commit the attempt, letting
    tools/gb2's own fallback (engine.full_orbit.run_full_job, under
    `fallback_job_name` -- the literal `f"{job_name}-full"` tools/gb2
    computes) actually open and finish. The SAME static script plays
    back for every call of a given role -- see the ScriptedRunner's
    own docstring (engine/workers/scripted.py) -- so this one script
    writes BOTH the small job's own (target-naming) files AND the
    fallback full job's own (clean) files; which half of it matters
    depends only on which job_name's own merge/commit step actually
    reads afterward."""
    _write_script(scripts_dir / "facts.json", [
        {"tool": "Write", "file_path": f".gongbang/{fallback_job_name}/facts.md",
         "content": _FACTS_MD},
        {"final": {"status": "done", "summary": "facts", "requests": []}},
    ])
    _write_script(scripts_dir / "prober.json", _UNUSED_ROLE_SCRIPT)
    _write_script(scripts_dir / "planner.json", [
        {"tool": "Write", "file_path": f".gongbang/{small_job_name}/facts.md",
         "content": _FACTS_MD_WITH_TARGET},
        {"tool": "Write", "file_path": f".gongbang/{small_job_name}/plan.json",
         "content": _plan_json()},
        {"tool": "Write", "file_path": "tests/scenes/s1_greet.js", "content": _SCENE_GREET_JS},
        {"tool": "Write", "file_path": f".gongbang/{fallback_job_name}/plan.json",
         "content": _plan_json()},
        {"final": {"status": "done", "summary": "small-plan", "requests": []}},
    ])
    _write_script(scripts_dir / "designer.json", _UNUSED_ROLE_SCRIPT)
    _write_script(scripts_dir / "builder.json", [
        {"tool": "Write", "file_path": "src/greeter.js", "content": _GREETER_JS},
        {"tool": "Write", "file_path": "tests/greeter.test.js", "content": _GREETER_TEST_JS},
        {"final": {"status": "done", "summary": "build", "requests": []}},
    ])
    _write_script(scripts_dir / "reviewer.json", [
        {"final": {"passed": True, "findings": [], "coverage": "full"}},
    ])


@_needs_bwrap
@_needs_node_modules
def test_gb2_change_small_closes_and_falls_back_to_full_job_on_an_external_target(
    tmp_path: Path, scripts_dir: Path,
) -> None:
    """A small job whose own combined planning call names a real
    external target must close its attempt before falling back, so
    tools/gb2's own fallback call (engine.full_orbit.run_full_job,
    under a fresh job_name) finds the project CLOSED rather than
    raising "required state ('CLOSED',), got 'OPEN'". This drives the
    real `gb2` CLI end to end (never engine.small_orbit directly) to
    prove the fallback actually completes."""
    apps_root = tmp_path / "source"
    workspace = tmp_path / "workspace"
    apps_root.mkdir()
    app = "greeter-target-app"
    job_name = "greeter-target-job"
    fallback_job_name = f"{job_name}-full"

    assert _node_modules_src is not None  # narrowed by @_needs_node_modules
    _scaffold_app_source_repo(apps_root, app, _node_modules_src)
    _write_small_job_scripts_with_external_target(scripts_dir, job_name, fallback_job_name)

    env = {
        **os.environ, "GB2_APPS_ROOT": str(apps_root), "GB2_WORKSPACE": str(workspace),
        "GB2_SCRIPTED_WORKERS": str(scripts_dir),
    }
    _init_app_workspace_with_audit_cmd(env, workspace, app, _FAKE_AUDIT_CLEAN, with_release=False)

    result = _run_gb2(["change", app, _REQUEST, "--job-name", job_name, "--small"], env)
    assert result.returncode == 0, (result.stdout, result.stderr)
    summary = json.loads(result.stdout)
    assert summary["status"] == "done"
    assert summary["small_fallback_reason"] == "small_mode_needs_probe"

    app_repo = workspace / "apps" / app / "repo"
    # The small attempt's own facts.md (naming the real target) is
    # committed -- never left dangling uncommitted for a reset to wipe
    # -- and the fallback full job's own facts.md/plan.json/report.md
    # exist alongside it under the SAME app repo (same app, different
    # job_name -- tools/gb2's own fallback never scaffolds a new app).
    small_facts = (app_repo / ".gongbang" / job_name / "facts.md").read_text(encoding="utf-8")
    assert "외부 대상" in small_facts and "날씨 API" in small_facts
    log = subprocess.run(
        ["git", "-C", str(app_repo), "log", "--oneline", "--", f".gongbang/{job_name}"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert log.strip()  # the small attempt's own trail landed as a real commit
    assert (app_repo / ".gongbang" / fallback_job_name / "report.md").exists()
    assert (app_repo / "src" / "greeter.js").read_text(encoding="utf-8") == _GREETER_JS


@_needs_bwrap
@_needs_node_modules
def test_gb2_change_small_runs_fewer_worker_calls_than_full_job(
    tmp_path: Path, scripts_dir: Path,
) -> None:
    apps_root = tmp_path / "source"
    workspace = tmp_path / "workspace"
    apps_root.mkdir()

    assert _node_modules_src is not None  # narrowed by @_needs_node_modules

    full_job_name = "greeter-full"
    small_job_name = "greeter-small"

    # -- the ordinary full job, as a baseline. --
    _scaffold_app_source_repo(apps_root, "greeter-full-app", _node_modules_src)
    _write_full_job_scripts(scripts_dir, full_job_name)
    env_full = {
        **os.environ, "GB2_APPS_ROOT": str(apps_root), "GB2_WORKSPACE": str(workspace),
        "GB2_SCRIPTED_WORKERS": str(scripts_dir),
    }
    _init_app_workspace_with_audit_cmd(
        env_full, workspace, "greeter-full-app", _FAKE_AUDIT_CLEAN, with_release=False)
    full_result = _run_gb2(
        ["change", "greeter-full-app", _REQUEST, "--job-name", full_job_name], env_full)
    assert full_result.returncode == 0, (full_result.stdout, full_result.stderr)
    full_summary = json.loads(full_result.stdout)
    assert full_summary["status"] == "done"
    full_calls = _station_finished_count(workspace, "greeter-full-app", full_job_name)

    # -- the small path, same request shape, a fresh app+scripts dir. --
    _scaffold_app_source_repo(apps_root, "greeter-small-app", _node_modules_src)
    _write_small_job_scripts(scripts_dir, small_job_name)
    env_small = {
        **os.environ, "GB2_APPS_ROOT": str(apps_root), "GB2_WORKSPACE": str(workspace),
        "GB2_SCRIPTED_WORKERS": str(scripts_dir),
    }
    _init_app_workspace_with_audit_cmd(
        env_small, workspace, "greeter-small-app", _FAKE_AUDIT_CLEAN, with_release=False)
    small_result = _run_gb2(
        ["change", "greeter-small-app", _REQUEST, "--job-name", small_job_name, "--small"],
        env_small)
    assert small_result.returncode == 0, (small_result.stdout, small_result.stderr)
    small_summary = json.loads(small_result.stdout)
    assert small_summary["status"] == "done"
    small_calls = _station_finished_count(workspace, "greeter-small-app", small_job_name)

    # Fewer worker calls: one combined planning call + one build call
    # (2), vs. the full job's facts + plan + build (3).
    assert small_calls < full_calls
    assert small_calls == 2
    assert full_calls == 3

    # Per-station timing was actually recorded.
    assert "planning" in small_summary["durations_ms"]
    assert small_summary["durations_ms"]["planning"] > 0

    # Same real files, same real `npx vitest run` passing, in both.
    for app in ("greeter-full-app", "greeter-small-app"):
        app_repo = workspace / "apps" / app / "repo"
        assert (app_repo / "src" / "greeter.js").read_text(encoding="utf-8") == _GREETER_JS
        vitest_result = subprocess.run(
            ["npx", "vitest", "run"], cwd=str(app_repo), capture_output=True, text=True,
            timeout=120,
        )
        assert vitest_result.returncode == 0, vitest_result.stdout + vitest_result.stderr

    small_report = (
        workspace / "apps" / "greeter-small-app" / "repo" / ".gongbang" / small_job_name
        / "report.md"
    ).read_text(encoding="utf-8")
    assert "R1" in small_report and "scene:s1" in small_report
    assert "통과" in small_report


@_needs_bwrap
@_needs_node_modules
def test_gb2_change_small_with_release_stops_for_approval_then_ships_once(
    tmp_path: Path, scripts_dir: Path,
) -> None:
    """Same release gating a full job gets (engine.full_orbit.
    _maybe_release_or_done/finalize_release -- engine.small_orbit never
    writes its own release logic, see that module's own docstring):
    `--small` stops needs_owner/"release_approval" instead of deploying
    on its own, and a real `gb2 ship` (typed into a pty) then deploys
    exactly once."""
    apps_root = tmp_path / "source"
    workspace = tmp_path / "workspace"
    apps_root.mkdir()
    app = "greeter-release-app"
    job_name = "greeter-release-job"

    assert _node_modules_src is not None  # narrowed by @_needs_node_modules
    _scaffold_app_source_repo(apps_root, app, _node_modules_src)
    _write_small_job_scripts(scripts_dir, job_name)

    deploy_log = tmp_path / "deploy-calls.log"
    deploy_script = tmp_path / "fake-deploy.sh"
    _write(deploy_script, _DEPLOY_SCRIPT_SH.format(log_path=deploy_log))
    deploy_script.chmod(deploy_script.stat().st_mode | stat.S_IEXEC)

    env = {
        **os.environ, "GB2_APPS_ROOT": str(apps_root), "GB2_WORKSPACE": str(workspace),
        "GB2_DEPLOY_SCRIPT": str(deploy_script), "GB2_SCRIPTED_WORKERS": str(scripts_dir),
    }
    _init_app_workspace_with_audit_cmd(env, workspace, app, _FAKE_AUDIT_CLEAN, with_release=True)

    change_result = _run_gb2(
        ["change", app, _REQUEST, "--job-name", job_name, "--small"], env)
    assert change_result.returncode == 2, (change_result.stdout, change_result.stderr)
    change_summary = json.loads(change_result.stdout)
    assert change_summary["status"] == "needs_owner"
    assert change_summary["reason"] == "release_approval"
    assert not deploy_log.exists()

    status_result = _run_gb2(["status", app], env)
    assert status_result.returncode == 0, (status_result.stdout, status_result.stderr)
    status = json.loads(status_result.stdout)
    publish_requests = [r for r in status["queued_requests"] if r["kind"] == "publish"]
    assert len(publish_requests) == 1
    request_id = publish_requests[0]["request_id"]

    ship_result = _run_gb2_with_approval(["ship", app], env, f"승인 {request_id}")
    assert ship_result.returncode == 0, (ship_result.stdout, ship_result.stderr)
    ship_summary = json.loads(ship_result.stdout)
    assert ship_summary["publish"]["status"] == "done"
    assert ship_summary["status"] == "done"

    assert deploy_log.exists()
    deploy_lines = deploy_log.read_text(encoding="utf-8").splitlines()
    assert len(deploy_lines) == 1, deploy_lines
