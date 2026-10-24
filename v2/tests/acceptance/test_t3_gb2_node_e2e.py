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

# Under GB2_REQUIRE_T3=1 (the CI job that exists to run this test) a
# missing prerequisite FAILS instead of skipping -- see _t3_prereqs below.
_REQUIRE_T3 = os.environ.get("GB2_REQUIRE_T3") == "1"

_needs_bwrap = pytest.mark.skipif(
    not _REQUIRE_T3 and not sandbox.available(), reason="bwrap not available on this host",
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
    not _REQUIRE_T3 and _node_modules_src is None,
    reason="GB2_TEST_NODE_MODULES unset (or has no vitest) -- set it to an "
          "existing node_modules dir with vitest already installed, e.g. "
          "GB2_TEST_NODE_MODULES=<apps-root>/search/node_modules; nothing in "
          "this test ever runs `npm install`",
)



@pytest.fixture(autouse=True)
def _t3_prereqs() -> None:
    """Under GB2_REQUIRE_T3=1 a missing prerequisite is a failure."""
    if not _REQUIRE_T3:
        return
    if not sandbox.available():
        pytest.fail("GB2_REQUIRE_T3=1 but bwrap is not available")
    if _node_modules_src is None:
        pytest.fail("GB2_REQUIRE_T3=1 but GB2_TEST_NODE_MODULES is unset or has no vitest")


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

# A committed package-lock.json for this app -- see the pre-seed block
# in the tests below for why `gb2 new`'s own skeleton never produces
# one. Never actually consumed by `npm ci`/`npm install` (node_modules
# is pre-seeded via a symlink, not installed from this file) -- its
# only job is to make engine.app_security's dependency-audit check see
# a committed lockfile at all. A REAL lockfile, not hand-written: it is
# the verbatim output of `npm install --package-lock-only --offline`,
# run once (outside this test) in a scratch dir with the skeleton's own
# package.json and this project's GB2_TEST_NODE_MODULES symlinked in as
# node_modules -- confirmed to resolve and exit 0 fully offline against
# that tree (lockfileVersion 3, npm >=7's own shape); a hand-written
# lockfile would risk a shape npm itself would never actually produce.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "t3_node_app"
_PACKAGE_LOCK_JSON = (_FIXTURES_DIR / "memo-package-lock.json").read_text(encoding="utf-8")

# Fake, offline `npm audit --json` stand-ins (see each script's own
# docstring) -- security.audit_cmd is pointed at one of these per test
# below, replacing the node-vitest template's own default (real `npm
# audit`, which needs network) so the app-wide security gate
# (engine.app_security.check's dependency-audit check) runs
# deterministically offline either way: "passed" (fake_audit_clean.py,
# exit 0) or "vulnerabilities" (fake_audit_vulnerable.py, exit 1) --
# never "unavailable"/"no_audit_cmd_configured".
_FAKE_AUDIT_CLEAN = _FIXTURES_DIR / "fake_audit_clean.py"
_FAKE_AUDIT_VULNERABLE = _FIXTURES_DIR / "fake_audit_vulnerable.py"


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


def _run_gb2_with_approval(
    args: list[str], env: dict[str, str], owner_text: str,
) -> subprocess.CompletedProcess:
    """Drives a real `gb2` subprocess (e.g. `ship`) whose own TTY check
    (tools/gb2's cmd_ship: `if not sys.stdin.isatty(): refuse`) must see
    a real terminal -- `_run_gb2`'s plain `capture_output=True` gives it
    a pipe instead, which is why `ship` needs this separate helper. Only
    STDIN is a pseudo-terminal (`os.openpty`); stdout/stderr stay plain
    pipes, which is enough: Python's own `input()` only engages the
    readline/echo path when BOTH stdin and stdout are ttys, so a pty
    stdin + pipe stdout still gets the plain "write prompt to stdout,
    read one line from stdin" behavior `isatty()`-gated code expects,
    with none of a pty's own line-echo duplicated into captured stdout.
    `owner_text` (the "승인 R-..." line) is written to the pty's master
    fd, with its own trailing newline, right after the child starts --
    the pty's line-discipline queues it in the slave's read buffer
    until the child's own `input()` call actually reads it, so no
    explicit synchronization is needed before `communicate()`.

    The returned `.stdout` has `input()`'s own prompt text (which it
    writes to stdout, not stderr, right before reading -- the earlier
    owner-facing summary table cmd_ship prints is on stderr already,
    unaffected) stripped off: everything up to and including the FIRST
    "{" is dropped, so a caller can `json.loads(result.stdout)`
    directly, the same as `_run_gb2`'s own plain, non-interactive
    commands."""
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


def _scaffold_memo_source_repo(apps_root: Path, node_modules_src: Path) -> Path:
    """Scaffold+commit `apps_root/memo` as a real git repo BEFORE any
    `gb2` call ever sees it: the skeleton's own package.json/README.md/
    .gitignore/tests/scenes/.gitkeep (content read verbatim from
    tools/templates/new-app/, substituting "__APP__" the same one-line
    way tools/gb2's own _render_skeleton_text does, so this cannot
    drift out of sync with a skeleton file change), PLUS a
    package-lock.json `gb2 new`'s own skeleton never seeds (it is not
    in tools/gb2's `_NEW_APP_SKELETON_FILES`) -- engine.app_security's
    dependency-audit check (engine/app_security.py's own
    _dependency_audit_state) fails closed as "no_lockfile" otherwise,
    which this app (committed, but never actually `npm install`-ed)
    would hit with no way to satisfy for real. Later scaffolding by
    `gb2 init`/`gb2 new` (tools/gb2's own _scaffold_new_app_repo) skips
    re-rendering/re-committing every one of these -- an existing
    skeleton file is left untouched, and the initial commit is skipped
    once `source_repo` already has a HEAD -- the exact same "a previous
    `gb2 new` crashed partway through" resume path
    test_gb2_new.py's own
    test_cmd_new_resumes_an_interrupted_init_via_its_marker exercises,
    one step earlier: there is no point inside `gb2 new`'s own single
    synchronous call (scaffold -> workspace setup -> the full orbit)
    for this test to inject a commit mid-flight, and `gb2 init` (used
    below instead, specifically so the test can edit config.json
    in between init and the orbit run -- see
    _init_memo_workspace_with_audit_cmd) requires `source_repo/
    package.json` to ALREADY exist, so scaffolding happens here either
    way. node_modules itself is pre-seeded the same way as every other
    T3 run (a symlink, never committed, never touched by `npm install`
    -- see this module's own docstring for why)."""
    source_repo = apps_root / _APP
    source_repo.mkdir(parents=True)
    skeleton_dir = _GB2_PATH.parent / "templates" / "new-app"
    for rel in ("README.md", ".gitignore", "package.json", "tests/scenes/.gitkeep"):
        text = (skeleton_dir / rel).read_text(encoding="utf-8").replace("__APP__", _APP)
        _write(source_repo / rel, text)
    _write(source_repo / "package-lock.json", _PACKAGE_LOCK_JSON)
    _git(source_repo, "init", "-q", "-b", "master")
    _git(source_repo, "add", "README.md", ".gitignore", "package.json",
        "tests/scenes/.gitkeep", "package-lock.json")
    _git(source_repo, "-c", "user.name=gb2-t3-acceptance",
        "-c", "user.email=gb2-t3-acceptance@local",
        "commit", "-q", "-m", "chore: scaffold memo via test fixture (with lockfile)")
    (source_repo / _NEW_APP_MARKER_NAME).write_text(_APP, encoding="utf-8")
    (source_repo / "node_modules").symlink_to(node_modules_src, target_is_directory=True)
    # `source_repo` IS release.master_repo for a `--with-
    # release` app (_setup_app_workspace sets master_repo=source_repo,
    # never a separate clone) -- advance_master's own master_is_clean
    # check refuses ("master_not_clean") on ANY untracked, non-ignored
    # path in it. Two such paths, both verified empirically against a
    # real run:
    #  - The skeleton's own .gitignore has "node_modules/" (trailing
    #    slash), which git's ignore matcher only applies to an actual
    #    directory ENTRY -- a symlink NAMED node_modules still shows as
    #    untracked ("??", not "!!") under that pattern.
    #  - The marker this function just wrote, when the caller goes on
    #    to use `gb2 init` + `gb2 change` (see _init_memo_workspace_
    #    with_audit_cmd's own docstring for why) instead of `gb2 new`:
    #    only `cmd_new`'s own tail (`_run_full_job_and_report`'s
    #    `new_app_marker.unlink()`) ever consumes/removes this marker --
    #    `cmd_init` never references it at all, so it is left sitting
    #    here, untracked and non-ignored, for as long as this repo
    #    lives.
    # Without excluding both, every real `gb2 ship` against this
    # fixture fails before ever reaching anything this module is
    # actually testing. `.git/info/exclude` (a real git mechanism for a
    # private, per-checkout ignore rule the shared repo never sees --
    # same one test_release.py's own P1-4 test already relies on) with
    # NO trailing slash on the node_modules line correctly ignores a
    # symlink too (verified the same way) -- this is a test-fixture-
    # only fix, never touching the skeleton's own shared .gitignore.
    (source_repo / ".git" / "info" / "exclude").write_text(
        f"node_modules\n{_NEW_APP_MARKER_NAME}\n", encoding="utf-8")
    return source_repo


def _init_memo_workspace_with_audit_cmd(
    env: dict[str, str], workspace: Path, audit_script: Path, *, with_release: bool,
) -> None:
    """`gb2 init memo [--with-release]` (writes config.json from the
    node-vitest template, bench_cases.json, the gb2/work worktree,
    project registration -- NO orbit run; `_setup_app_workspace`'s own
    docstring), then edits that config.json's top-level "security"."
    audit_cmd" in place to point at `audit_script` -- there is no env/
    CLI override for this (tools/gb2's `_setup_app_workspace` always
    loads TEMPLATES_DIR/"node-vitest.json" by a hardcoded path), and
    the template's own default (real `npm audit`) needs network. `gb2
    change` (used right after this by both tests below, instead of
    `gb2 new`) reads this same, now-edited config.json back via
    EngineConfig.from_json and never re-renders it from the template --
    tools/gb2's own cmd_change docstring: "same full orbit as `gb2
    new`" -- `_run_full_job_and_report`/`engine.full_orbit.run_full_job`
    is the exact same call either way (cmd_new's own tail call), so
    this substitutes only WHICH CLI subcommand drives it, never the
    engine code path T3 exists to exercise."""
    init_result = _run_gb2(["init", _APP, *(["--with-release"] if with_release else [])], env)
    assert init_result.returncode == 0, (init_result.stdout, init_result.stderr)

    config_path = workspace / "apps" / _APP / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["security"] = {**config.get("security", {}), "audit_cmd": [sys.executable, str(audit_script)]}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


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

    assert _node_modules_src is not None  # narrowed by @_needs_node_modules
    _scaffold_memo_source_repo(apps_root, _node_modules_src)

    env = {
        **os.environ,
        "GB2_APPS_ROOT": str(apps_root),
        "GB2_WORKSPACE": str(workspace),
        "GB2_DEPLOY_SCRIPT": str(deploy_script),
        "GB2_SCRIPTED_WORKERS": str(scripts_dir),
    }

    # -- `gb2 init memo --with-release` (+ security.audit_cmd pointed at
    # the clean fake offline audit), then `gb2 change memo "<요구사항>"
    # --job-name memo-job` -- see _init_memo_workspace_with_audit_cmd
    # for why this replaces a single `gb2 new ... --with-release` call.
    _init_memo_workspace_with_audit_cmd(env, workspace, _FAKE_AUDIT_CLEAN, with_release=True)
    new_result = _run_gb2(
        ["change", _APP, _REQUEST, "--job-name", _JOB_NAME], env,
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


@_needs_bwrap
@_needs_node_modules
def test_gb2_change_node_app_security_gate_blocks_on_vulnerability(
    tmp_path: Path, scripts_dir: Path,
) -> None:
    """The counterpart to the test above: security.audit_cmd pointed at
    fake_audit_vulnerable.py (exit 1, one finding) instead of
    fake_audit_clean.py -- engine.app_security's dependency-audit check
    (engine/app_security.py's own _dependency_audit_state) reaches
    "vulnerabilities" for real (never "unavailable"/"no_lockfile": the
    lockfile is the same committed fixture as the test above, and the
    fake audit script itself always runs -- no allow_* flag ever
    suppresses a real finding, per that module's own docstring), so the
    whole job stops needs_owner/"security_failed" instead of reaching
    the release track at all, same shape as every other needs_owner
    stop `cmd_change`'s own _exit_code_for_full_outcome maps to exit 2."""
    apps_root = tmp_path / "source"
    workspace = tmp_path / "workspace"
    apps_root.mkdir()
    _write_scripted_worker_scripts(scripts_dir)

    assert _node_modules_src is not None  # narrowed by @_needs_node_modules
    _scaffold_memo_source_repo(apps_root, _node_modules_src)

    env = {
        **os.environ,
        "GB2_APPS_ROOT": str(apps_root),
        "GB2_WORKSPACE": str(workspace),
        "GB2_SCRIPTED_WORKERS": str(scripts_dir),
    }

    _init_memo_workspace_with_audit_cmd(env, workspace, _FAKE_AUDIT_VULNERABLE, with_release=False)
    change_result = _run_gb2(["change", _APP, _REQUEST, "--job-name", _JOB_NAME], env)
    assert change_result.returncode == 2, (change_result.stdout, change_result.stderr)
    change_summary = json.loads(change_result.stdout)
    assert change_summary["status"] == "needs_owner"
    assert change_summary["reason"].startswith("security_failed")
    # The finding itself (kind:path, no values) is visible to the person.
    assert "vuln" in change_summary["reason"] or "audit" in change_summary["reason"], change_summary["reason"]

    # -- `gb2 status memo` -- still runs (the job's own state is a
    # consistent, queryable needs_owner, not a crash).
    status_result = _run_gb2(["status", _APP], env)
    assert status_result.returncode == 0, (status_result.stdout, status_result.stderr)
    status = json.loads(status_result.stdout)
    assert status["state"] == "CLOSED"

    # The finding (kind:path, no values) is recorded as app_security.recorded
    # and carried in the stop reason checked above; `gb2 status` itself
    # lists event kinds only.


# ---------------------------------------------------------------------------
# A REAL `gb2 ship memo` through a pseudo-terminal: the approval line is
# typed, not stubbed. The normal case ships exactly once; a source change
# after the job reached release approval is refused (check_artifact stops
# it before the job-certificate check, which test_release.py covers).

@_needs_bwrap
@_needs_node_modules
def test_gb2_ship_memo_deploys_once_and_board_shows_shipped(
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

    assert _node_modules_src is not None  # narrowed by @_needs_node_modules
    _scaffold_memo_source_repo(apps_root, _node_modules_src)

    env = {
        **os.environ,
        "GB2_APPS_ROOT": str(apps_root),
        "GB2_WORKSPACE": str(workspace),
        "GB2_DEPLOY_SCRIPT": str(deploy_script),
        "GB2_SCRIPTED_WORKERS": str(scripts_dir),
    }

    _init_memo_workspace_with_audit_cmd(env, workspace, _FAKE_AUDIT_CLEAN, with_release=True)
    change_result = _run_gb2(["change", _APP, _REQUEST, "--job-name", _JOB_NAME], env)
    assert change_result.returncode == 2, (change_result.stdout, change_result.stderr)
    change_summary = json.loads(change_result.stdout)
    assert change_summary["status"] == "needs_owner"
    assert change_summary["reason"] == "release_approval"

    status_result = _run_gb2(["status", _APP], env)
    assert status_result.returncode == 0, (status_result.stdout, status_result.stderr)
    status = json.loads(status_result.stdout)
    publish_requests = [r for r in status["queued_requests"] if r["kind"] == "publish"]
    assert len(publish_requests) == 1
    request_id = publish_requests[0]["request_id"]

    # -- `gb2 ship memo`, typing "승인 R-..." into a real pty -- never a
    # stubbed `input()`; see _run_gb2_with_approval's own docstring.
    ship_result = _run_gb2_with_approval(
        ["ship", _APP], env, f"승인 {request_id}",
    )
    assert ship_result.returncode == 0, (ship_result.stdout, ship_result.stderr)
    ship_summary = json.loads(ship_result.stdout)
    assert ship_summary["publish"]["status"] == "done"
    assert ship_summary["status"] == "done"

    # The fake deploy script ran exactly once -- one line per real
    # invocation (see _DEPLOY_SCRIPT_SH).
    assert deploy_log.exists()
    deploy_lines = deploy_log.read_text(encoding="utf-8").splitlines()
    assert len(deploy_lines) == 1, deploy_lines

    # -- `gb2 board memo --out <file>` -- the release decision this
    # module's own engine.full_orbit.record_release_decision call
    # (tools/gb2's cmd_ship) writes shows up as 배포됨 (engine/board.py's
    # own _RELEASE_DECISION_LABEL), not 완료/건너뜀.
    board_out = tmp_path / "board.html"
    board_result = _run_gb2(["board", _APP, "--out", str(board_out)], env)
    assert board_result.returncode == 0, (board_result.stdout, board_result.stderr)
    board_html = board_out.read_text(encoding="utf-8")
    assert "배포됨" in board_html

    # Shipping again must find nothing pending -- the request is done,
    # the job is closed.
    status_after = json.loads(_run_gb2(["status", _APP], env).stdout)
    assert not [r for r in status_after["queued_requests"] if r["kind"] == "publish"]


@_needs_bwrap
@_needs_node_modules
def test_gb2_ship_refuses_when_source_changed_after_acceptance(
    tmp_path: Path, scripts_dir: Path,
) -> None:
    """A real source change committed directly into the app repo AFTER
    the job already reached needs_owner/release_approval (acceptance/
    app_security already ran and were recorded against the tree at
    that point) must make `gb2 ship` refuse -- never silently deploy a
    tree nobody's acceptance/security check ever actually saw.

    Refuses with "artifact_mismatch", not "release_requires_job_
    certificate": engine.steps.publish's check_artifact step (HEAD
    must still equal the sha the publish request was built for) always
    runs BEFORE advance_master's own job-certificate check, for ANY
    post-job tree change, committed or not -- see this module's own
    comment above for the live evidence. Both are real, intentional
    refusals; this one is just the MORE SPECIFIC reason for this exact
    scenario. The job-certificate-specific refusal (a real diff outside
    .gongbang/ between two already-fingerprinted trees, with
    check_artifact's own sha gate never in the way) is covered instead
    by tests/unit/test_release.py's own test_job_certificate_status_
    refuses_when_source_changes_outside_gongbang, driving engine.
    release.job_certificate_status directly."""
    apps_root = tmp_path / "source"
    workspace = tmp_path / "workspace"
    apps_root.mkdir()
    _write_scripted_worker_scripts(scripts_dir)

    deploy_log = tmp_path / "deploy-calls.log"
    deploy_script = tmp_path / "fake-deploy.sh"
    _write(deploy_script, _DEPLOY_SCRIPT_SH.format(log_path=deploy_log))
    deploy_script.chmod(deploy_script.stat().st_mode | stat.S_IEXEC)

    assert _node_modules_src is not None  # narrowed by @_needs_node_modules
    _scaffold_memo_source_repo(apps_root, _node_modules_src)

    env = {
        **os.environ,
        "GB2_APPS_ROOT": str(apps_root),
        "GB2_WORKSPACE": str(workspace),
        "GB2_DEPLOY_SCRIPT": str(deploy_script),
        "GB2_SCRIPTED_WORKERS": str(scripts_dir),
    }

    _init_memo_workspace_with_audit_cmd(env, workspace, _FAKE_AUDIT_CLEAN, with_release=True)
    change_result = _run_gb2(["change", _APP, _REQUEST, "--job-name", _JOB_NAME], env)
    assert change_result.returncode == 2, (change_result.stdout, change_result.stderr)
    assert json.loads(change_result.stdout)["status"] == "needs_owner"

    app_repo = workspace / "apps" / _APP / "repo"
    _git(app_repo, "-c", "user.name=drift", "-c", "user.email=drift@local",
        "commit", "--allow-empty", "-q", "-m", "drift: a change acceptance never saw")

    status_result = _run_gb2(["status", _APP], env)
    status = json.loads(status_result.stdout)
    request_id = [r for r in status["queued_requests"] if r["kind"] == "publish"][0]["request_id"]

    ship_result = _run_gb2_with_approval(["ship", _APP], env, f"승인 {request_id}")
    assert ship_result.returncode == 3, (ship_result.stdout, ship_result.stderr)
    ship_summary = json.loads(ship_result.stdout)
    # tools/gb2's cmd_ship prints `publish_outcome` directly (no
    # "publish" wrapper key) on this early-return path and returns 3 --
    # the wrapper/receipt_path shape only exists on the later, "done"
    # success path (see test_gb2_ship_memo_deploys_once_and_board_
    # shows_shipped).
    assert ship_summary["status"] == "failed"
    assert ship_summary["result"]["reason"] == "artifact_mismatch"

    assert not deploy_log.exists()
