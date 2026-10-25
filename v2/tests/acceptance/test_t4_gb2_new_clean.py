"""T4 -- the one gap T3 (test_t3_gb2_node_e2e.py) deliberately leaves
open: T3 always pre-seeds node_modules (a symlink to GB2_TEST_NODE_
MODULES) and a hand-committed package-lock.json, specifically so it
never has to run a real `npm install` -- see that module's own
docstring. Nothing anywhere drives a real `gb2 new` against a TRULY
EMPTY app folder, where engine.bootstrap.ensure_installed's own FIRST
install (this project's own package.json devDependency on vitest) has
to actually run for real, and tools/gb2's own _commit_generated_
lockfile then has to commit whatever that real install just wrote.

This test does exactly that, as one single `gb2 new memo "<요구사항>"
--with-release` subprocess call against an apps_root that does not
contain `memo` at all yet -- no pre-seeded node_modules, no hand-
written package-lock.json. Only the AI-shaped workers are fake (the
GB2_SCRIPTED_WORKERS test seam, same as T3); the dependency install,
the security audit (the node-vitest template's own default `npm audit`
needs network, left untouched here), the sandbox, and the real `gb2
ship` through a pty are otherwise exactly what an owner would get.

Needs real network (a real `npm install` against the public npm
registry) and bwrap -- skipped (GB2_REQUIRE_T3 unset) or failed
(GB2_REQUIRE_T3=1) the same way T3 is when either is missing; see
_t4_prereqs. GB2_SCRIPTED_WORKERS's own scripts directory is kept
outside tmp_path/HOME for the exact reason T3's own module docstring
already documents (a sandboxed worker's --tmpfs /tmp hides anything
under the real /tmp, and bwrap also hides the real $HOME from a
worker's own copy) -- reusing that reasoning here, not importing it
from T3."""
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

_GB2_PATH = Path(__file__).resolve().parents[2] / "tools" / "gb2"

# Under GB2_REQUIRE_T3=1 (the same CI job T3 runs under) a missing
# prerequisite FAILS instead of skipping -- see _t4_prereqs below.
_REQUIRE_T3 = os.environ.get("GB2_REQUIRE_T3") == "1"

_needs_bwrap = pytest.mark.skipif(
    not _REQUIRE_T3 and not sandbox.available(), reason="bwrap not available on this host",
)


def _npm_registry_reachable() -> bool:
    try:
        subprocess.run(
            ["npm", "ping"], capture_output=True, timeout=20,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


_npm_reachable = _npm_registry_reachable()
_needs_network = pytest.mark.skipif(
    not _REQUIRE_T3 and not _npm_reachable,
    reason="no network reach to the npm registry (GB2_REQUIRE_T3=1 would fail this instead)",
)


@pytest.fixture(autouse=True)
def _t4_prereqs() -> None:
    if not _REQUIRE_T3:
        return
    if not sandbox.available():
        pytest.fail("GB2_REQUIRE_T3=1 but bwrap is not available")
    if not _npm_reachable:
        pytest.fail("GB2_REQUIRE_T3=1 but the npm registry is not reachable")


_APP = "greeter"
_JOB_NAME = "greeter-job"
_REQUEST = "인사말 앱: 이름을 받아 인사말을 돌려주고, 그 인사말을 돌려받을 수 있게 해줘."

# ---------------------------------------------------------------------------
# One task, one scene -- deliberately smaller than T3's two-task memo
# app: this test's own job is proving the clean-folder/real-install/
# real-lockfile-commit path works, not feature coverage (that is T1/T2/
# T3's job already).
# ---------------------------------------------------------------------------

_GREET_JS = '''\
export function greet(name) {
  return `안녕, ${name}!`;
}
'''

_GREET_TEST_JS = '''\
import { describe, expect, it } from "vitest";
import { greet } from "../src/greet.js";

describe("greet", () => {
  it("returns a greeting for the given name", () => {
    expect(greet("세계")).toBe("안녕, 세계!");
  });
});
'''

_SCENE_GREET_JS = '''\
import assert from "node:assert";
import { greet } from "../../src/greet.js";

assert.strictEqual(greet("세계"), "안녕, 세계!");
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


def _plan_json() -> str:
    return json.dumps({
        "goal": "인사말 앱: 이름을 받아 인사말을 돌려준다",
        "ui": False,
        "scenes": [
            {"id": "s1", "actor_does": "이름을 넣고 인사말을 본다",
             "app_shows": "이름이 들어간 인사말이 보인다", "where": "cli",
             "cmd": ["node", "tests/scenes/s1_greet.js"], "covers": ["R1"]},
        ],
        "tasks": [
            {"id": "t1", "title": "인사말 구현",
             "detail": "greet.js에 greet(name)을 만든다",
             "owns": ["src/greet.js", "tests/greet.test.js"], "depends_on": [],
             "covers": ["R1"]},
        ],
        "acceptance": ["이름을 받아 인사말을 돌려준다"],
        "requirements": [
            {"id": "R1", "text": "이름을 받아 인사말을 돌려준다", "kind": "function"},
        ],
    }, ensure_ascii=False)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_script(path: Path, actions: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(actions, ensure_ascii=False), encoding="utf-8")


_UNUSED_ROLE_SCRIPT = [{"final": {"status": "done", "summary": "unused", "requests": []}}]


def _write_scripted_worker_scripts(scripts_dir: Path) -> None:
    """One script per role in tools/templates/node-vitest.json's own
    "workers" table (facts/prober/planner/designer/builder/reviewer) --
    _apply_scripted_workers refuses to run at all if even one is
    missing. prober/designer are never actually invoked by this job
    (facts.md's "## 외부 대상" is "(없음)" and plan.ui is False), so
    their own scripts are plain unused placeholders -- same convention
    test_t3_gb2_node_e2e.py's own _write_scripted_worker_scripts uses,
    written fresh here rather than imported from it."""
    _write_script(scripts_dir / "facts.json", [
        {"tool": "Write", "file_path": f".gongbang/{_JOB_NAME}/facts.md", "content": _FACTS_MD},
        {"final": {"status": "done", "summary": "facts", "requests": []}},
    ])
    _write_script(scripts_dir / "prober.json", _UNUSED_ROLE_SCRIPT)
    _write_script(scripts_dir / "planner.json", [
        {"tool": "Write", "file_path": f".gongbang/{_JOB_NAME}/plan.json", "content": _plan_json()},
        {"tool": "Write", "file_path": "tests/scenes/s1_greet.js", "content": _SCENE_GREET_JS},
        {"final": {"status": "done", "summary": "plan", "requests": []}},
    ])
    _write_script(scripts_dir / "designer.json", _UNUSED_ROLE_SCRIPT)
    _write_script(scripts_dir / "builder.json", [
        {"tool": "Write", "file_path": "src/greet.js", "content": _GREET_JS},
        {"tool": "Write", "file_path": "tests/greet.test.js", "content": _GREET_TEST_JS},
        {"final": {"status": "done", "summary": "build", "requests": []}},
    ])
    _write_script(scripts_dir / "reviewer.json", [
        {"final": {"passed": True, "findings": [], "coverage": "full"}},
    ])


_DEPLOY_SCRIPT_SH = '''\
#!/bin/sh
# Fake deploy script (test fixture, not engine code): records every
# call it gets (argv + a timestamp) to its own log file and exits 0 --
# never actually deploys anything.
echo "$(date +%s) $*" >> "{log_path}"
exit 0
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _run_gb2(args: list[str], env: dict[str, str], *,
            timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-P", str(_GB2_PATH), *args],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def _run_gb2_with_approval(
    args: list[str], env: dict[str, str], owner_text: str,
) -> subprocess.CompletedProcess:
    """Drives a real `gb2` subprocess (`ship`) whose own TTY check
    (tools/gb2's cmd_ship: `if not sys.stdin.isatty(): refuse`) must see
    a real terminal -- a plain pipe (subprocess.run's own
    capture_output) never satisfies that, so stdin is a pseudo-terminal
    (`os.openpty`) instead; stdout/stderr stay plain pipes. Python's
    own `input()` only engages the readline/echo path when BOTH stdin
    and stdout are ttys, so this still gets the plain "write prompt to
    stdout, read one line from stdin" behavior `isatty()`-gated code
    expects, with none of a pty's own line-echo duplicated into
    captured stdout. `owner_text` (the "승인 R-..." line) is written to
    the pty's master fd, with its own trailing newline, right after the
    child starts -- the pty's line discipline queues it in the slave's
    read buffer until the child's own `input()` call actually reads it,
    so no explicit synchronization is needed before `communicate()`.
    Same technique as test_t3_gb2_node_e2e.py's own
    _run_gb2_with_approval, written fresh here rather than imported
    from it. The returned `.stdout` has `input()`'s own prompt text
    (written to stdout, right before reading) stripped off: everything
    up to and including the FIRST "{" is dropped, so a caller can
    `json.loads(result.stdout)` directly."""
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
    """GB2_SCRIPTED_WORKERS's own directory -- deliberately NOT under
    tmp_path/`/tmp` (a sandboxed worker's `--tmpfs /tmp` hides anything
    there) and NOT under the real $HOME either (bwrap hides that too
    from a worker's own copy) -- same empirical reasoning
    test_t3_gb2_node_e2e.py's own `scripts_dir` fixture already
    documents, applied fresh here."""
    base = Path.home() / ".cache" / "gb2-t4-acceptance"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=str(base)))
    try:
        yield path
    finally:
        import shutil
        shutil.rmtree(path, ignore_errors=True)


@_needs_bwrap
@_needs_network
def test_gb2_new_against_an_empty_folder_installs_for_real_and_ships(
    tmp_path: Path, scripts_dir: Path,
) -> None:
    apps_root = tmp_path / "source"
    workspace = tmp_path / "workspace"
    apps_root.mkdir()
    workspace.mkdir()
    _write_scripted_worker_scripts(scripts_dir)

    # `apps_root/greeter` does not exist at all yet -- `gb2 new` itself
    # must scaffold it (tools/gb2._scaffold_new_app_repo), with no
    # pre-seeded node_modules and no hand-written package-lock.json,
    # unlike every T3 scenario.
    assert not (apps_root / _APP).exists()

    deploy_log = tmp_path / "deploy-calls.log"
    deploy_script = tmp_path / "fake-deploy.sh"
    _write(deploy_script, _DEPLOY_SCRIPT_SH.format(log_path=deploy_log))
    deploy_script.chmod(deploy_script.stat().st_mode | stat.S_IEXEC)

    env = {
        **os.environ,
        "GB2_APPS_ROOT": str(apps_root),
        "GB2_WORKSPACE": str(workspace),
        "GB2_DEPLOY_SCRIPT": str(deploy_script),
        "GB2_SCRIPTED_WORKERS": str(scripts_dir),
    }

    # -- `gb2 new greeter "<요구사항>" --with-release --job-name
    # greeter-job`: scaffold -> workspace setup -> a REAL first
    # dependency install (engine.bootstrap.ensure_installed, no
    # node_modules/marker yet) -> the full orbit (facts -> plan ->
    # build -> review -> bench -> acceptance -> app_security -> report)
    # -> stops needs_owner/release_approval (a release track is
    # configured, nothing ships itself).
    new_result = _run_gb2(
        ["new", _APP, _REQUEST, "--with-release", "--job-name", _JOB_NAME], env,
    )
    assert new_result.returncode == 2, (new_result.stdout, new_result.stderr)
    new_summary = json.loads(new_result.stdout)
    assert new_summary["status"] == "needs_owner", new_summary
    assert new_summary["reason"] == "release_approval", new_summary

    source_repo = apps_root / _APP
    app_repo = workspace / "apps" / _APP / "repo"
    assert app_repo.is_dir()

    # The real install actually ran: node_modules now exists (with this
    # bootstrap's own completion marker) and a REAL package-lock.json
    # (npm's own output, never hand-written) was committed into the
    # SOURCE repo -- not just left dirty -- exactly the two things this
    # job's own regression fixes (engine.bootstrap's marker,
    # tools/gb2._commit_generated_lockfile's pathspec commit) are about.
    assert (source_repo / "node_modules" / ".gongbang-installed").exists()
    lockfile_log = _git(source_repo, "log", "--format=%s", "--", "package-lock.json")
    assert "bootstrap dependency lockfile" in lockfile_log
    assert (source_repo / "package-lock.json").exists()
    status = _git(source_repo, "status", "--porcelain", "--", "package-lock.json")
    assert status == ""  # committed, not left dirty

    # The app's own files are real, in the app's own repo (the gb2/work
    # worktree the build task actually ran against).
    assert (app_repo / "src" / "greet.js").read_text(encoding="utf-8") == _GREET_JS

    # `npx vitest run` passes in the app repo, the way an owner would
    # check it themselves -- against the REAL vitest this bootstrap
    # just installed, not a pre-seeded copy.
    vitest_result = subprocess.run(
        ["npx", "vitest", "run"], cwd=str(app_repo), capture_output=True, text=True, timeout=120,
    )
    assert vitest_result.returncode == 0, vitest_result.stdout + vitest_result.stderr

    report_text = (app_repo / ".gongbang" / _JOB_NAME / "report.md").read_text(encoding="utf-8")
    assert "통과" in report_text
    # plan.json's own requirement (R1, covered by scene s1) must show
    # up by id in the handoff's requirement<->scene table -- real
    # requirement tracking end to end, not just a pass/fail string.
    assert "R1" in report_text

    assert not deploy_log.exists()  # release still pending `gb2 ship`

    # -- `gb2 status greeter` -- exactly one queued publish request.
    status_result = _run_gb2(["status", _APP], env)
    assert status_result.returncode == 0, (status_result.stdout, status_result.stderr)
    status_json = json.loads(status_result.stdout)
    publish_requests = [r for r in status_json["queued_requests"] if r["kind"] == "publish"]
    assert len(publish_requests) == 1
    request_id = publish_requests[0]["request_id"]

    # -- a REAL `gb2 ship greeter`, typing "승인 R-..." into a real pty.
    ship_result = _run_gb2_with_approval(["ship", _APP], env, f"승인 {request_id}")
    assert ship_result.returncode == 0, (ship_result.stdout, ship_result.stderr)
    ship_summary = json.loads(ship_result.stdout)
    assert ship_summary["status"] == "done"
    assert ship_summary["publish"]["status"] == "done"

    # The fake deploy script ran exactly once.
    assert deploy_log.exists()
    deploy_lines = deploy_log.read_text(encoding="utf-8").splitlines()
    assert len(deploy_lines) == 1, deploy_lines


def test_ci_engine_node_e2e_job_runs_t3_t4_and_t5() -> None:
    """ci/engine-node-e2e.yml (the public GitHub Actions job snippet for
    this whole file's CLI-level suite) must actually invoke this test
    module too, not just T3 -- an earlier round left the checked-in job
    running only test_t3_gb2_node_e2e.py, so T4's/T5's own CLI-level
    coverage never ran in CI at all. Plain text containment, no YAML
    parser dependency (pyyaml is not a project dependency here)."""
    ci_path = Path(__file__).resolve().parents[2] / "ci" / "engine-node-e2e.yml"
    text = ci_path.read_text(encoding="utf-8")
    assert "test_t3_gb2_node_e2e.py" in text
    assert "test_t4_gb2_new_clean.py" in text
    assert "test_t5_small_change.py" in text
    assert "GB2_REQUIRE_T3: \"1\"" in text
    assert "GB2_TEST_NODE_MODULES" in text


_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "tools" / "templates"


def test_new_app_template_provides_a_runnable_app_skeleton() -> None:
    """Review finding: "Template cannot scaffold common web apps --
    root application/configuration files remain outside builder
    ownership." tools/templates/new-app/ must ship a real, bootable
    Vite skeleton (index.html + src/main.js + vite.config.js + dev/
    build/preview scripts), and node-vitest.json's own
    station_owners.build.builder must list those exact root files so a
    plan that owns them is accepted by engine.plan's own
    "owns outside build prefixes" check -- see engine/plan.py's
    _prefix_covers and its own "problems" list. This checks the
    template files directly; it does NOT check that `gb2 new` actually
    copies index.html/vite.config.js/src/main.js into a scaffolded repo
    yet -- that copy list (tools/gb2's own _NEW_APP_SKELETON_FILES) is
    outside this file's ownership and still needs updating separately
    for the new files to reach a real scaffolded app."""
    skeleton = _TEMPLATES_DIR / "new-app"
    assert (skeleton / "index.html").is_file()
    assert (skeleton / "vite.config.js").is_file()
    assert (skeleton / "src" / "main.js").is_file()

    package = json.loads((skeleton / "package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]
    assert scripts["dev"] == "vite"
    assert scripts["build"] == "vite build"
    assert scripts["preview"] == "vite preview"
    assert "vite" in package["devDependencies"]

    config = json.loads((_TEMPLATES_DIR / "node-vitest.json").read_text(encoding="utf-8"))
    builder_owns = config["station_owners"]["build"]["builder"]
    for root_file in ("index.html", "vite.config.js", "vite.config.ts", "vite.config.mjs"):
        assert root_file in builder_owns
