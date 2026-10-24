"""T3 -- the gap an external review named: T1/T2 (test_t1_new_app.py/
test_t2_change_app.py) call engine.full_orbit.run_full_job directly
against a Python app, and every CLI test (tests/unit/test_gb2_new.py)
replaces the engine with a stub. Nothing drives the REAL `gb2` CLI
against the REAL Node/Vitest template end to end.

This test does exactly that: every command is a real subprocess
(`python -P tools/gb2 ...`), against the real
tools/templates/new-app/ skeleton + tools/templates/node-vitest.json
config, with the whole engine (full_orbit, bwrap sandbox) -- only the
AI-shaped workers are fake, via the GB2_SCRIPTED_WORKERS test seam
tools/gb2's own _fakes_builder/_apply_scripted_workers add (see that
module). No live claude/codex call is possible here: GB2_SCRIPTED_WORKERS
rewrites EVERY role's own WorkerConfig.runner, so the real ClaudeRunner/
CodexRunner objects _build_fakes still constructs are simply never
selected by any role.

Scenario: a tiny Node app "메모장" (notes) with two requirements --
R1 (kind "function": add a note and list it back) and R2 (kind
"security": a note from user A is not visible to user B) -- split into
two build tasks on two disjoint files (src/notes.js / src/guard.js,
same "two independent files per task" shape test_t1_new_app.py and
test_t2_change_app.py already use, so task N's own commit never needs
task M's file to exist yet). Release is configured
(`gb2 new --with-release`) with a fake deploy script that only records
its own argv -- the whole point of the release track (ENGINE.md) is
that NOTHING calls that script until an owner approves `gb2 ship`,
which this test never runs.

Everything is asserted the way an owner actually would: `gb2`'s own
exit codes and stdout JSON, the app repo's own files/commits/`npx
vitest run`, `.gongbang/<job>/report.md`'s 대조표, `gb2 status`'s own
queued_requests, and `gb2 board`'s own HTML -- never engine-internal
state (the ledger is never opened directly anywhere below).

Skipped (not failed) when this host cannot run it at all:
  - bwrap is not available (engine.sandbox.available()) -- the real
    template hardcodes "sandbox": "bwrap", and this test does not try
    to talk tools/gb2 out of that.
  - GB2_TEST_NODE_MODULES is unset, or does not point at an existing
    node_modules with vitest already in it -- nothing here ever runs
    `npm install` (the scripted builder issues no install request, see
    below), so vitest must already be resolvable from a pre-existing
    node_modules or `npx vitest run`/the bench step have nothing to
    run. Locally: GB2_TEST_NODE_MODULES=<apps-root>/search/node_modules.
    The first `gb2 new` call below copies that whole node_modules tree
    into the app's own gb2/work worktree (tools/gb2's own
    `_ensure_worktree`, not touched by this test) -- real, and slow
    (tens of seconds) the one time it has to happen; this test is not
    fast.

Why GB2_SCRIPTED_WORKERS's own scripts directory is NOT under
tmp_path (verified empirically): engine.sandbox.wrap gives every
bwrap-sandboxed worker a fixed, unconditional "--tmpfs /tmp" (engine/
sandbox.py's own hardcoded mount list, independent of this project's
hidden/writable/readonly config) -- an EMPTY /tmp inside the sandbox,
regardless of what real content lives there on the host. pytest's own
tmp_path fixture always resolves under /tmp, so a ScriptedRunner script
path there is invisible to the sandboxed worker process that reads it
by absolute path (a bare FileNotFoundError, not a permission denial --
confirmed by reading the worker's own stdout file under this project's
stations/*.stdout file in the app workspace (engine home, jobs dir), which IS
visible -- it is one of the explicit writable binds engine.steps
.station adds back deeper than "--tmpfs /tmp"). GB2_WORKSPACE/
GB2_APPS_ROOT themselves stay under tmp_path with no such problem: a
worker only ever sees its own copy (engine.steps.open_job.copy_root)
or this project's explicit writable/readonly binds, never the repo or
workspace paths directly.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from engine import sandbox

_GB2_PATH = Path(__file__).resolve().parents[2] / "tools" / "gb2"

_needs_bwrap = pytest.mark.skipif(
    not sandbox.available(), reason="bwrap not available on this host",
)

# tools/gb2's own _NEW_APP_MARKER_NAME -- duplicated as a literal here
# (not imported) since this test deliberately never imports tools/gb2
# as a module, only ever runs it as a subprocess; see this module's own
# docstring. Used only to pre-seed node_modules into a not-yet-"new"ed
# app folder via the exact same crashed-partway-through resume path
# test_gb2_new.py's test_cmd_new_resumes_an_interrupted_init_via_its_marker
# already exercises -- `gb2 new` finishes scaffolding it for real.
_NEW_APP_MARKER_NAME = ".gongbang-new"

_APP = "memo"
_JOB_NAME = "memo-job"
_REQUEST = "메모장: 메모를 추가하고 목록으로 볼 수 있게 해줘. 다른 사람의 메모는 보이면 안 돼."


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
    _node_modules_src is None,
    reason="GB2_TEST_NODE_MODULES unset (or has no vitest) -- set it to an "
          "existing node_modules dir with vitest already installed, e.g. "
          "GB2_TEST_NODE_MODULES=<apps-root>/search/node_modules; nothing in "
          "this test ever runs `npm install`",
)


# ---------------------------------------------------------------------------
# App source files the scripted builder writes (two tasks, two disjoint
# files -- same shape as test_t1_new_app.py's greet()/farewell()).
# ---------------------------------------------------------------------------

_NOTES_JS = '''\
const notes = [];

export function addNote(owner, text) {
  notes.push({ owner, text });
}

export function listNotes(owner) {
  return notes.filter((n) => n.owner === owner).map((n) => n.text);
}
'''

_NOTES_TEST_JS = '''\
import { describe, expect, it } from "vitest";
import { addNote, listNotes } from "../src/notes.js";

describe("notes", () => {
  it("adds a note and lists it back for its own owner", () => {
    addNote("t1-alice", "hello");
    expect(listNotes("t1-alice")).toEqual(["hello"]);
  });
});
'''

_GUARD_JS = '''\
// canView(viewer, owner): true iff `viewer` is allowed to see notes
// owned by `owner` -- the one place "a note from user A is not
// visible to user B" is actually decided; callers must check this
// before ever calling notes.js's listNotes() with someone else's name.
export function canView(viewer, owner) {
  return viewer === owner;
}
'''

_GUARD_TEST_JS = '''\
import { describe, expect, it } from "vitest";
import { canView } from "../src/guard.js";

describe("guard", () => {
  it("only lets a user view their own notes", () => {
    expect(canView("t2-alice", "t2-alice")).toBe(true);
    expect(canView("t2-bob", "t2-alice")).toBe(false);
  });
});
'''

_SCENE_ADD_LIST_JS = '''\
import assert from "node:assert";
import { addNote, listNotes } from "../../src/notes.js";

addNote("s1-alice", "회의 메모");
assert.deepStrictEqual(listNotes("s1-alice"), ["회의 메모"]);
'''

_SCENE_ISOLATION_JS = '''\
import assert from "node:assert";
import { addNote, listNotes } from "../../src/notes.js";
import { canView } from "../../src/guard.js";

addNote("s2-alice", "비밀 메모");
addNote("s2-bob", "공개 메모");

assert.strictEqual(canView("s2-alice", "s2-alice"), true);
assert.strictEqual(canView("s2-bob", "s2-alice"), false);

const aliceOwnView = canView("s2-alice", "s2-alice") ? listNotes("s2-alice") : [];
const bobViewOfAlice = canView("s2-bob", "s2-alice") ? listNotes("s2-alice") : [];

assert.deepStrictEqual(aliceOwnView, ["비밀 메모"]);
assert.deepStrictEqual(bobViewOfAlice, []);
'''

_FACTS_MD = '''\
# 사실 확인

| 주장 | 판정 | 출처 |
|---|---|---|
| 메모는 사용자별로 저장되고 소유자만 조회할 수 있다 | 통과 | 요구사항 원문 |

## 코드에 넣는 값
(없음 -- 하드코딩할 값 없음)

## 외부 대상
(없음)
'''


def _plan_json() -> str:
    return json.dumps({
        "goal": "메모장: 메모를 추가/조회하고, 다른 사용자의 메모는 보이지 않게 한다",
        "ui": False,
        "requirements": [
            {"id": "R1", "text": "메모를 추가하고 목록으로 볼 수 있다", "kind": "function"},
            {"id": "R2", "text": "다른 사용자가 쓴 메모는 보이지 않는다", "kind": "security"},
        ],
        "scenes": [
            {"id": "s1", "actor_does": "메모를 추가하고 자기 목록을 본다",
             "app_shows": "방금 추가한 메모가 목록에 보인다", "where": "cli",
             "cmd": ["node", "tests/scenes/s1_add_list.js"], "covers": ["R1"]},
            {"id": "s2", "actor_does": "다른 사용자의 메모를 보려고 한다",
             "app_shows": "다른 사용자의 메모는 보이지 않는다", "where": "cli",
             "cmd": ["node", "tests/scenes/s2_isolation.js"], "covers": ["R2"]},
        ],
        "tasks": [
            {"id": "t1", "title": "메모 추가/조회 구현",
             "detail": "notes.js에 addNote/listNotes를 만든다",
             "owns": ["src/notes.js", "tests/notes.test.js"], "depends_on": []},
            {"id": "t2", "title": "사용자별 접근 제어 구현",
             "detail": "guard.js에 canView를 만든다",
             "owns": ["src/guard.js", "tests/guard.test.js"], "depends_on": ["t1"]},
        ],
        "acceptance": [
            "메모를 추가하고 목록으로 볼 수 있다",
            "다른 사용자가 쓴 메모는 보이지 않는다",
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
    their own scripts are plain unused placeholders."""
    _write_script(scripts_dir / "facts.json", [
        {"tool": "Write", "file_path": f".gongbang/{_JOB_NAME}/facts.md", "content": _FACTS_MD},
        {"final": {"status": "done", "summary": "facts", "requests": []}},
    ])
    _write_script(scripts_dir / "prober.json", _UNUSED_ROLE_SCRIPT)
    _write_script(scripts_dir / "planner.json", [
        {"tool": "Write", "file_path": f".gongbang/{_JOB_NAME}/plan.json", "content": _plan_json()},
        {"tool": "Write", "file_path": "tests/scenes/s1_add_list.js", "content": _SCENE_ADD_LIST_JS},
        {"tool": "Write", "file_path": "tests/scenes/s2_isolation.js", "content": _SCENE_ISOLATION_JS},
        {"final": {"status": "done", "summary": "plan", "requests": []}},
    ])
    _write_script(scripts_dir / "designer.json", _UNUSED_ROLE_SCRIPT)
    # One static script for every build-station call -- see
    # test_t1_new_app.py's own module docstring: each task's own
    # `owns` prefix is what actually narrows which of these Writes
    # lands on that task's own commit (engine.full_orbit/engine.steps
    # .merge), not this script.
    _write_script(scripts_dir / "builder.json", [
        {"tool": "Write", "file_path": "src/notes.js", "content": _NOTES_JS},
        {"tool": "Write", "file_path": "tests/notes.test.js", "content": _NOTES_TEST_JS},
        {"tool": "Write", "file_path": "src/guard.js", "content": _GUARD_JS},
        {"tool": "Write", "file_path": "tests/guard.test.js", "content": _GUARD_TEST_JS},
        {"final": {"status": "done", "summary": "build", "requests": []}},
    ])
    _write_script(scripts_dir / "reviewer.json", [
        {"final": {"passed": True, "findings": [], "coverage": "full"}},
    ])


_DEPLOY_SCRIPT_SH = '''\
#!/bin/sh
# Fake deploy script (test fixture, not engine code): records every
# call it gets (argv + a timestamp) to its own log file and exits 0 --
# never actually deploys anything. This test's whole point is proving
# this file is called zero times (the release stays needs_owner,
# pending `gb2 ship`, which this test never runs).
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


@pytest.fixture()
def scripts_dir():
    """GB2_SCRIPTED_WORKERS's own directory -- deliberately NOT under
    tmp_path/`/tmp` at all; see this module's own docstring for why a
    sandboxed worker could never read a script living there. Cleaned
    up unconditionally (pass or fail)."""
    base = Path.home() / ".cache" / "gb2-t3-acceptance"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=str(base)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@_needs_bwrap
@_needs_node_modules
def test_gb2_new_continue_status_board_node_app_with_pending_release(
    tmp_path: Path, scripts_dir: Path,
) -> None:
    apps_root = tmp_path / "source"
    workspace = tmp_path / "workspace"
    apps_root.mkdir()
    _write_scripted_worker_scripts(scripts_dir)

    deploy_log = tmp_path / "deploy-calls.log"
    deploy_script = tmp_path / "fake-deploy.sh"
    _write(deploy_script, _DEPLOY_SCRIPT_SH.format(log_path=deploy_log))
    deploy_script.chmod(deploy_script.stat().st_mode | stat.S_IEXEC)

    # Pre-seed this not-yet-existing app's node_modules via the exact
    # same "a previous `gb2 new` crashed partway through" resume path
    # test_gb2_new.py's own
    # test_cmd_new_resumes_an_interrupted_init_via_its_marker exercises
    # -- `gb2 new` below finishes scaffolding README/.gitignore/
    # package.json/tests/scenes/ for real and never touches
    # node_modules itself (it is not one of its named skeleton files).
    # See this module's own docstring for why nothing here ever runs
    # `npm install` instead.
    source_repo = apps_root / _APP
    source_repo.mkdir(parents=True)
    (source_repo / _NEW_APP_MARKER_NAME).write_text(_APP, encoding="utf-8")
    assert _node_modules_src is not None  # narrowed by @_needs_node_modules
    (source_repo / "node_modules").symlink_to(_node_modules_src, target_is_directory=True)

    env = {
        **os.environ,
        "GB2_APPS_ROOT": str(apps_root),
        "GB2_WORKSPACE": str(workspace),
        "GB2_DEPLOY_SCRIPT": str(deploy_script),
        "GB2_SCRIPTED_WORKERS": str(scripts_dir),
    }

    # -- `gb2 new memo "<요구사항>" --with-release --job-name memo-job` --
    new_result = _run_gb2(
        ["new", _APP, _REQUEST, "--with-release", "--job-name", _JOB_NAME], env,
    )
    assert new_result.returncode == 2, (new_result.stdout, new_result.stderr)
    new_summary = json.loads(new_result.stdout)
    assert new_summary["status"] == "needs_owner"
    assert new_summary["reason"] == "release_approval"

    app_repo = workspace / "apps" / _APP / "repo"
    assert app_repo.is_dir()

    # The app's own files/commits are real, in the app's own repo (the
    # gb2/work worktree gb2 new drives every build task against) -- not
    # engine-internal state.
    assert (app_repo / "src" / "notes.js").read_text(encoding="utf-8") == _NOTES_JS
    assert (app_repo / "src" / "guard.js").read_text(encoding="utf-8") == _GUARD_JS
    log = _git(app_repo, "log", "--format=%s")
    assert log.count("\n") >= 1  # more than the single initial commit

    # the final commit the release's own publish request was prepared
    # for (new_summary["commit"]) IS the app repo's current HEAD -- the
    # publish request was built from the finished job's own final
    # state, never from some earlier, mid-job commit.
    assert new_summary["commit"] == _git(app_repo, "rev-parse", "HEAD")

    # `npx vitest run` passes in the app repo, the way an owner would
    # check it themselves.
    vitest_result = subprocess.run(
        ["npx", "vitest", "run"], cwd=str(app_repo), capture_output=True, text=True, timeout=120,
    )
    assert vitest_result.returncode == 0, vitest_result.stdout + vitest_result.stderr

    # .gongbang/<job>/report.md exists; its 대조표 shows R1/R2 each
    # against a passing scene of their own.
    report_text = (app_repo / ".gongbang" / _JOB_NAME / "report.md").read_text(encoding="utf-8")
    assert "R1" in report_text and "scene:s1" in report_text
    assert "R2" in report_text and "scene:s2" in report_text
    assert "통과" in report_text

    # NO deploy happened -- the release stays pending an owner's own
    # `gb2 ship`, which this test never runs.
    assert not deploy_log.exists()

    # -- `gb2 continue memo` -- idempotent while the publish request is
    # still queued: still needs_owner/release_approval, still no deploy.
    continue_result = _run_gb2(["continue", _APP], env)
    assert continue_result.returncode == 2, (continue_result.stdout, continue_result.stderr)
    continue_summary = json.loads(continue_result.stdout)
    assert continue_summary["status"] == "needs_owner"
    assert continue_summary["reason"] == "release_approval"
    assert not deploy_log.exists()

    # -- `gb2 status memo` -- exactly one queued publish request, still
    # queued (never dispatched).
    status_result = _run_gb2(["status", _APP], env)
    assert status_result.returncode == 0, (status_result.stdout, status_result.stderr)
    status = json.loads(status_result.stdout)
    publish_requests = [r for r in status["queued_requests"] if r["kind"] == "publish"]
    assert len(publish_requests) == 1
    assert publish_requests[0]["status"] == "queued"

    # -- `gb2 board memo --out <file>` -- the job itself reads 완료
    # (write_handoff's own job.handoff_written event -- engine.board
    # keys off that, not off the still-pending release) even though
    # the whole job's own FullOutcome is needs_owner.
    board_out = tmp_path / "board.html"
    board_result = _run_gb2(["board", _APP, "--out", str(board_out)], env)
    assert board_result.returncode == 0, (board_result.stdout, board_result.stderr)
    board_html = board_out.read_text(encoding="utf-8")
    assert _JOB_NAME in board_html
    assert "완료" in board_html

    assert not deploy_log.exists()
