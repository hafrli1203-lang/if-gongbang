"""Unit tests for engine.steps.merge: collect/apply by config-derived
allowed path, conflict detection, never touching pre-existing dirty paths,
the P0-1/P2-9 hardening (allowed paths are never taken from the request,
paths cannot escape the repo, symlinks/binary content are handled
safely), the #2 copy-identity hardening (a copy's path must be EXACTLY
the one open_job created for the claimed role), and the B-10/B-11/#10/#11
base-tree and frozen-plan hardening.

Since copy_root(config, attempt_id, role) is a pure function of
(attempt_id, role), two copies in one merge request can never
legitimately share a role. Tests that need two independently-edited
copies either use two different REAL roles with disjoint config prefixes
(builder/tester), or -- when the test specifically needs two roles that
CAN touch the same path, to exercise conflict detection -- a config
variant (`_overlap_config`) that adds a second, synthetic role sharing
"src/" with "builder". That variant is built locally with
dataclasses.replace and never touches the shared tests/conftest.py
fixture.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
from types import SimpleNamespace

import pytest

from engine.config import EngineConfig
from engine.faults import CrashInjected, Faults
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.policy import Policy
from engine.processor import Fakes, Processor, StepFailed, submit
from engine.steps import merge
from engine.steps.open_job import copy_root, open_job
from tests.conftest import Workspace, git


def _processor(workspace: Workspace, holder: str = "p1",
                faults: Faults | None = None,
                config: EngineConfig | None = None) -> Processor:
    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller())
    proc = Processor(config=config or workspace.config, ledger=workspace.ledger,
                      fakes=fakes, holder=holder, faults=faults)
    proc.register("merge", merge.steps)
    return proc


def _overlap_config(config: EngineConfig) -> EngineConfig:
    """A config variant adding a second, synthetic role ("shadow"/
    "shadow_builder") whose allowed prefixes overlap "build"/"builder"'s
    "src/" (plus one exclusive path of its own) -- the only way, under
    the #2 fix, to have two independently-edited copies that can touch
    the same relative path in one request."""
    return dataclasses.replace(config, station_owners={
        **config.station_owners,
        "shadow": {"shadow_builder": ("src/", "shadow_notes.md")},
    })


def test_two_disjoint_copies_merge_cleanly(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "parallel",
                           "full", ("builder", "tester"))
    copy_a = copy_root(workspace.config, attempt_id, "builder")
    copy_b = copy_root(workspace.config, attempt_id, "tester")
    (copy_a / "src" / "app.py").write_text("print('a')\n", encoding="utf-8")
    (copy_b / "tests" / "bench" / "cases.json").write_text("[1]\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [
            {"role": "builder", "station": "build", "path": str(copy_a)},
            {"role": "tester", "station": "register", "path": str(copy_b)},
        ],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('a')\n"
    assert (workspace.alpha / "tests" / "bench" / "cases.json").read_text(encoding="utf-8") == "[1]\n"


def test_conflicting_edit_fails_and_writes_nothing(workspace: Workspace) -> None:
    cfg = _overlap_config(workspace.config)
    attempt_id = open_job(cfg, workspace.ledger, "alpha", "conflict", "full",
                           ("builder", "shadow_builder"))
    copy_a = copy_root(cfg, attempt_id, "builder")
    copy_b = copy_root(cfg, attempt_id, "shadow_builder")
    (copy_a / "src" / "app.py").write_text("print('a')\n", encoding="utf-8")
    (copy_b / "src" / "app.py").write_text("print('b')\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [
            {"role": "builder", "station": "build", "path": str(copy_a)},
            {"role": "shadow_builder", "station": "shadow", "path": str(copy_b)},
        ],
    }, "owner")
    proc = _processor(workspace, config=cfg)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "merge_conflict"}
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"


def test_same_content_from_two_copies_is_not_a_conflict(workspace: Workspace) -> None:
    cfg = _overlap_config(workspace.config)
    attempt_id = open_job(cfg, workspace.ledger, "alpha", "same", "full",
                           ("builder", "shadow_builder"))
    copy_a = copy_root(cfg, attempt_id, "builder")
    copy_b = copy_root(cfg, attempt_id, "shadow_builder")
    (copy_a / "src" / "app.py").write_text("print('same')\n", encoding="utf-8")
    (copy_b / "src" / "app.py").write_text("print('same')\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [
            {"role": "builder", "station": "build", "path": str(copy_a)},
            {"role": "shadow_builder", "station": "shadow", "path": str(copy_b)},
        ],
    }, "owner")
    proc = _processor(workspace, config=cfg)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('same')\n"


def test_delete_in_copy_removes_file_in_repo(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "del",
                           "full", ("tester",))
    copy_a = copy_root(workspace.config, attempt_id, "tester")
    (copy_a / "tests" / "bench" / "cases.json").unlink()

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "tester", "station": "register", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert not (workspace.alpha / "tests" / "bench" / "cases.json").exists()


def test_pre_existing_dirty_path_is_never_touched(workspace: Workspace) -> None:
    (workspace.alpha / "docs" / "readme.md").write_text("owner edit\n", encoding="utf-8")
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "dirtykeep",
                           "full", ("builder",))
    assert workspace.ledger.project("alpha").dirty_paths == ("docs/readme.md",)

    copy_a = copy_root(workspace.config, attempt_id, "builder")
    # relative to base_head this still counts as a change, even though the
    # copy's starting point (the owner's working tree) already had "owner edit"
    (copy_a / "docs" / "readme.md").write_text("copy edit\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert (workspace.alpha / "docs" / "readme.md").read_text(encoding="utf-8") == "owner edit\n"


def test_outside_config_allowed_prefix_changes_are_ignored(workspace: Workspace) -> None:
    """station_owners["register"]["tester"] only allows tests/bench/: an
    edit to src/app.py by the same copy must never land, regardless of
    anything the request payload claims."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "outside",
                           "full", ("tester",))
    copy_a = copy_root(workspace.config, attempt_id, "tester")
    (copy_a / "src" / "app.py").write_text("print('in-scope')\n", encoding="utf-8")
    (copy_a / "tests" / "bench" / "cases.json").write_text("[1]\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "tester", "station": "register", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"
    assert (workspace.alpha / "tests" / "bench" / "cases.json").read_text(encoding="utf-8") == "[1]\n"


# ---- P0-1: allowed paths come only from config, never from the request ---

def test_allowed_in_payload_is_ignored_not_trusted(workspace: Workspace) -> None:
    """Even if a payload tries to smuggle an `allowed` key (the old, no
    longer supported contract), it has no effect: the attacker-controlled
    field is simply absent from the new contract, so a tester copy still
    cannot write outside tests/bench/."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "ignore-allowed",
                           "full", ("tester",))
    copy_a = copy_root(workspace.config, attempt_id, "tester")
    (copy_a / ".git" / "HEAD").write_text("ref: refs/heads/evil\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{
            "role": "tester", "station": "register", "path": str(copy_a),
            "allowed": [".git/"],
        }],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    # the project's real .git/ was never touched
    assert (workspace.alpha / ".git" / "HEAD").exists()


def test_unknown_station_role_fails(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "unknown-owner",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "nope", "station": "nowhere", "path": "/nonexistent"}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "unknown_owner"}


def test_copy_path_outside_home_fails(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "outside-home",
                           "full", ("builder",))
    # a copy living outside config.home/"copies"/{attempt_id}/ entirely
    rogue = workspace.root / "rogue_copy"
    shutil.copytree(workspace.alpha, rogue)

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(rogue)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "copy_path_mismatch"}
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"


def test_nested_payload_dir_claiming_different_role_is_rejected(workspace: Workspace) -> None:
    """#2: a tester nests an arbitrary subdirectory inside its OWN copy
    and submits that nested dir claiming the "builder" role, which has
    broader write access (src/, lib/, docs/, package.json) than tester's
    own tests/bench/. The path must be EXACTLY copy_root(..., role), so
    this nested dir -- real and inside home/copies/{attempt_id}/, but not
    the canonical builder copy -- is rejected."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "nested-payload",
                           "full", ("tester",))
    tester_copy = copy_root(workspace.config, attempt_id, "tester")
    nested = tester_copy / "tests" / "bench" / "payload"
    (nested / "src").mkdir(parents=True)
    (nested / "src" / "app.py").write_text("print('pwned')\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(nested)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "copy_path_mismatch"}
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"


def test_validate_rel_rejects_dotdot_absolute_and_protected(workspace: Workspace) -> None:
    """Direct unit test of the per-path guard used by every merge step:
    '..' components, absolute paths, and policy-protected paths (e.g.
    .gitignore, which this workspace's protected_paths names) are all
    rejected with path_rejected; an ordinary in-repo path is accepted."""
    policy = Policy(workspace.config, workspace.ledger)
    ctx = SimpleNamespace(project="alpha")
    repo = workspace.alpha

    with pytest.raises(StepFailed) as exc1:
        merge._validate_rel(ctx, repo, policy, "../outside.txt")
    assert exc1.value.reason == "path_rejected"

    with pytest.raises(StepFailed) as exc2:
        merge._validate_rel(ctx, repo, policy, "/etc/passwd")
    assert exc2.value.reason == "path_rejected"

    with pytest.raises(StepFailed) as exc3:
        merge._validate_rel(ctx, repo, policy, ".gitignore")
    assert exc3.value.reason == "path_rejected"

    merge._validate_rel(ctx, repo, policy, "src/app.py")  # does not raise


def test_symlink_in_copy_is_rejected(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "symlink",
                           "full", ("builder",))
    copy_a = copy_root(workspace.config, attempt_id, "builder")
    outside_secret = workspace.root / "outside_secret.txt"
    outside_secret.write_text("TOP SECRET\n", encoding="utf-8")
    (copy_a / "src" / "app.py").unlink()
    os.symlink(outside_secret, copy_a / "src" / "app.py")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "symlink_in_copy"}
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"


# ---- B-10: a copy's delta is against its own base_head, not live HEAD ---

def test_second_copys_merge_does_not_revert_first_copys_commit(workspace: Workspace) -> None:
    """copy_b is made from the SAME base_head as copy_a (both from the
    same open_job). After copy_a's merge is committed (HEAD advances past
    base_head), merging copy_b -- which never touched src/app.py -- must
    not report src/app.py as changed (it is unchanged relative to
    copy_b's own base) and so must never revert or delete it."""
    cfg = _overlap_config(workspace.config)
    attempt_id = open_job(cfg, workspace.ledger, "alpha", "basehead", "full",
                           ("builder", "shadow_builder"))
    copy_a = copy_root(cfg, attempt_id, "builder")
    copy_b = copy_root(cfg, attempt_id, "shadow_builder")
    (copy_a / "src" / "app.py").write_text("print('a')\n", encoding="utf-8")

    rid_a = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace, config=cfg)
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid_a).status == "done"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('a')\n"

    # simulate the processor's commit step advancing HEAD past base_head
    git(workspace.alpha, "add", "-A")
    git(workspace.alpha, "commit", "-q", "-m", "merge a")

    # copy_b's own src/app.py is untouched (pristine base content); its
    # own, unrelated edit under its exclusive path must still land.
    (copy_b / "shadow_notes.md").write_text("b notes\n", encoding="utf-8")
    rid_b = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "shadow_builder", "station": "shadow", "path": str(copy_b)}],
    }, "owner")
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid_b).status == "done"
    # A's committed change survives; nothing was reverted or deleted
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('a')\n"
    assert (workspace.alpha / "shadow_notes.md").read_text(encoding="utf-8") == "b notes\n"


def test_merge_conflict_with_head_when_destination_diverged_from_base(workspace: Workspace) -> None:
    """The copy changes a path whose current destination already differs
    from the copy's base (someone else's committed change landed there
    since this copy was made) -> merge_conflict_with_head, nothing
    written, even though no OTHER copy in this same request touches it."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "diverged",
                           "full", ("builder",))
    copy_a = copy_root(workspace.config, attempt_id, "builder")
    (copy_a / "src" / "app.py").write_text("print('from-copy')\n", encoding="utf-8")

    # someone else commits a change to the same path, independent of this copy
    (workspace.alpha / "src" / "app.py").write_text("print('committed-elsewhere')\n", encoding="utf-8")
    git(workspace.alpha, "add", "-A")
    git(workspace.alpha, "commit", "-q", "-m", "someone else's change")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "merge_conflict_with_head"}
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('committed-elsewhere')\n"


# ---- B-11/#10/#11 remainder: the copy must not be mutable between steps -

def test_copy_mutated_after_check_conflicts_crash_fails_apply_nothing_written(
    workspace: Workspace,
) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "mutated",
                           "full", ("builder",))
    copy_a = copy_root(workspace.config, attempt_id, "builder")
    (copy_a / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
    }, "owner")

    proc1 = _processor(workspace, holder="p1", faults=Faults(crash_after={"check_conflicts"}))
    with pytest.raises(CrashInjected):
        proc1.run_once("alpha")
    assert workspace.ledger.request(rid).status == "running"

    # mutate the copy after collect_copies already froze its plan
    (copy_a / "src" / "app.py").write_text("print('v2-mutated')\n", encoding="utf-8")

    proc2 = _processor(workspace, holder="p2")
    n = proc2.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "copy_changed_after_check"}
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"


def test_binary_file_is_copied_as_bytes(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "binary",
                           "full", ("builder",))
    copy_a = copy_root(workspace.config, attempt_id, "builder")
    binary_content = bytes([0, 1, 2, 255, 254, 0, 9, 10, 13])
    (copy_a / "src" / "app.py").write_bytes(binary_content)

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert (workspace.alpha / "src" / "app.py").read_bytes() == binary_content


def test_gitignored_build_artifact_is_never_a_merge_candidate(workspace: Workspace) -> None:
    """A raw filesystem walk under an allowed prefix would pick up
    tests/__pycache__/*.pyc, a build artifact a worker's own pytest
    run creates while building, even though .gitignore already marks
    `__pycache__/` ignored. If the destination repo has its OWN,
    different bytes for that same ignored path (e.g. from an earlier
    local pytest run), that would fail the merge with
    "merge_conflict_with_head" over a file nobody asked to change.
    Candidates instead come from the copy's own `git status` (open_job.
    dirty_paths), which never reports a gitignored path at all -- the
    artifact is never even considered, and the destination's own
    (different) copy of it is never touched or compared against."""
    (workspace.alpha / ".gitignore").write_text(
        (workspace.alpha / ".gitignore").read_text(encoding="utf-8") + "__pycache__/\n",
        encoding="utf-8",
    )
    git(workspace.alpha, "add", ".gitignore")
    git(workspace.alpha, "commit", "-q", "-m", "ignore __pycache__")

    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job1",
                           "full", ("tester",))
    copy_t = copy_root(workspace.config, attempt_id, "tester")
    # A real, legitimate change under the allowed prefix.
    (copy_t / "tests" / "bench" / "new_test.py").write_text("x = 1\n", encoding="utf-8")
    # The gitignored build artifact a pytest run would leave behind --
    # present in the copy, under the SAME allowed prefix.
    copy_pycache = copy_t / "tests" / "bench" / "__pycache__"
    copy_pycache.mkdir(parents=True)
    (copy_pycache / "x.pyc").write_bytes(b"copy bytes")

    # The destination repo already has its own, DIFFERENT bytes for
    # that same ignored path -- this must never register as a
    # conflict, since the ignored file is never a candidate at all.
    dest_pycache = workspace.alpha / "tests" / "bench" / "__pycache__"
    dest_pycache.mkdir(parents=True)
    (dest_pycache / "x.pyc").write_bytes(b"destination bytes, different")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "tester", "station": "register", "path": str(copy_t)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"

    recorded = [e for _s, e in workspace.ledger.events("alpha", "merge.recorded")
                if e.payload.get("request_id") == rid][0]
    assert recorded.payload["paths"] == ["tests/bench/new_test.py"]
    assert (workspace.alpha / "tests" / "bench" / "new_test.py").read_text(
        encoding="utf-8") == "x = 1\n"
    # The destination's own ignored artifact is untouched.
    assert (dest_pycache / "x.pyc").read_bytes() == b"destination bytes, different"
