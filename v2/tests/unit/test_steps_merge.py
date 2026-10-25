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


def _scenes_overlap_config(config: EngineConfig) -> EngineConfig:
    """A config variant that (unlike the shared STATION_OWNERS fixture)
    deliberately gives "build"/"builder" a prefix wide enough to ALSO
    cover tests/scenes/ -- the only way to prove merge's own reserved-
    prefix denial (engine.steps.merge._deny_if_planner_reserved) holds
    even when an operator's own config would otherwise allow it."""
    return dataclasses.replace(config, station_owners={
        **config.station_owners,
        "build": {"builder": ("tests/",)},
    })


def test_merge_denies_builder_write_under_tests_scenes(workspace: Workspace) -> None:
    cfg = _scenes_overlap_config(workspace.config)
    attempt_id = open_job(cfg, workspace.ledger, "alpha", "scenes-grab", "full", ("builder",))
    copy_a = copy_root(cfg, attempt_id, "builder")
    (copy_a / "tests" / "scenes" / "evil.py").parent.mkdir(parents=True, exist_ok=True)
    (copy_a / "tests" / "scenes" / "evil.py").write_text("exit(0)\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace, config=cfg)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "tests_scenes_reserved_for_planner"}
    assert not (workspace.alpha / "tests" / "scenes" / "evil.py").exists()
    denied = [e for _seq, e in workspace.ledger.events("alpha", "merge.denied")]
    assert denied and denied[0].payload["role"] == "builder"


def test_planner_role_may_still_write_tests_scenes(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "scenes-ok", "full",
                           ("planner",))
    copy_a = copy_root(workspace.config, attempt_id, "planner")
    cfg = dataclasses.replace(workspace.config, station_owners={
        **workspace.config.station_owners,
        "plan": {"planner": ("tests/scenes/", "plan.json")},
    })
    (copy_a / "tests" / "scenes").mkdir(parents=True, exist_ok=True)
    (copy_a / "tests" / "scenes" / "scene1.py").write_text("exit(0)\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "planner", "station": "plan", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace, config=cfg)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert (workspace.alpha / "tests" / "scenes" / "scene1.py").read_text(
        encoding="utf-8") == "exit(0)\n"


def test_merge_narrows_to_copy_owns_when_given(workspace: Workspace) -> None:
    """A copy's "owns" entry (engine.full_orbit's per-task narrowing)
    must shrink merge's own allowed prefixes below the FULL configured
    "build"/"builder" set ("src/", "lib/", "docs/", "package.json") --
    a file under an allowed-by-config-but-not-by-owns prefix is simply
    never a merge candidate at all, even though the broader station
    prefix would otherwise have accepted it."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "narrow", "full",
                           ("builder",))
    copy_a = copy_root(workspace.config, attempt_id, "builder")
    (copy_a / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (copy_a / "docs" / "b.md").parent.mkdir(parents=True, exist_ok=True)
    (copy_a / "docs" / "b.md").write_text("notes\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a),
                    "owns": ["src/"]}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert (workspace.alpha / "src" / "a.py").exists()
    assert not (workspace.alpha / "docs" / "b.md").exists()


def test_merge_rejects_owns_widening_beyond_station_prefixes(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "widen", "full",
                           ("builder",))
    copy_a = copy_root(workspace.config, attempt_id, "builder")
    (copy_a / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a),
                    "owns": ["src/", "secrets/"]}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "owns_escapes_station_prefixes"}


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
        merge._validate_rel(ctx, repo, policy, "../outside.txt", "builder")
    assert exc1.value.reason == "path_rejected"

    with pytest.raises(StepFailed) as exc2:
        merge._validate_rel(ctx, repo, policy, "/etc/passwd", "builder")
    assert exc2.value.reason == "path_rejected"

    with pytest.raises(StepFailed) as exc3:
        merge._validate_rel(ctx, repo, policy, ".gitignore", "builder")
    assert exc3.value.reason == "path_rejected"

    merge._validate_rel(ctx, repo, policy, "src/app.py", "builder")  # does not raise


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


def test_remerge_on_same_attempt_catches_an_owner_edit_of_an_already_merged_path(
    workspace: Workspace,
) -> None:
    """A first merge on this attempt writes
    src/app.py; the OWNER then hand-edits that same path directly
    (simulating an owner-approval/install pause that leaves this same
    attempt active -- engine.full_orbit's own owner stops), without
    committing. A SECOND merge request on the SAME attempt_id/copy
    (the builder continuing work on the same path -- exactly engine.
    full_orbit._rerun_build_task_stages' own shape) must not silently
    overwrite that edit just because this attempt already applied
    this path's name once before -- the path's bare NAME being
    "already applied" is not, by itself, proof that nothing else
    touched it since. Before this fix, _attempt_already_applied_paths
    exempted any such path from _verify_destinations_unchanged
    entirely, so request 2 here would have silently clobbered the
    owner's edit with the builder's new content and reported 'done'."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "reedit",
                           "full", ("builder",))
    copy_a = copy_root(workspace.config, attempt_id, "builder")
    (copy_a / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")

    rid1 = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid1).status == "done"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('v1')\n"

    # The owner hand-edits the already-merged path during an owner stop
    # (an install/approval pause), WITHOUT committing -- this attempt
    # stays active the whole time.
    (workspace.alpha / "src" / "app.py").write_text("print('OWNER-EDIT')\n", encoding="utf-8")

    # The builder keeps working on the same copy/path.
    (copy_a / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")

    rid2 = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
        "merge_retry_n": 1,
    }, "owner")
    n = proc.run_once("alpha")
    assert n == 0
    req2 = workspace.ledger.request(rid2)
    assert req2.status == "failed"
    assert req2.result == {"reason": "merge_conflict_with_owner_edit"}
    # The owner's edit is still there -- never overwritten.
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('OWNER-EDIT')\n"


def test_remerge_on_same_attempt_catches_an_owner_revert_to_exactly_base_content(
    workspace: Workspace,
) -> None:
    """Same shape as the owner-edit test above, but the owner's edit
    here is specifically a REVERT back to exactly base_head's own
    bytes (undoing this attempt's first merge on purpose), not some
    other arbitrary content. _verify_destinations_unchanged checks
    the last-applied hash FIRST for every already-applied rel,
    regardless of what the destination's current bytes happen to
    equal right now -- checking "does the destination still equal
    base_head" first instead would short-circuit straight past the
    already-applied hash and right past the owner's revert, letting
    the second merge silently overwrite it. Checking the last-applied
    hash first catches it instead."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "revert-to-base",
                           "full", ("builder",))
    copy_a = copy_root(workspace.config, attempt_id, "builder")
    base_content = (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8")
    (copy_a / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")

    rid1 = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid1).status == "done"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('v1')\n"

    # The owner deliberately REVERTS the already-merged path back to
    # exactly its base_head content, during an owner stop, WITHOUT
    # committing -- this attempt stays active the whole time.
    (workspace.alpha / "src" / "app.py").write_text(base_content, encoding="utf-8")

    # The builder keeps working on the same copy/path.
    (copy_a / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")

    rid2 = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
        "merge_retry_n": 1,
    }, "owner")
    n = proc.run_once("alpha")
    assert n == 0
    req2 = workspace.ledger.request(rid2)
    assert req2.status == "failed"
    assert req2.result == {"reason": "merge_conflict_with_owner_edit"}
    # The owner's revert is still there -- never overwritten with v2.
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == base_content


def test_remerge_on_same_attempt_of_an_untouched_already_applied_path_succeeds(
    workspace: Workspace,
) -> None:
    """The safe counterpart of the test above: nobody touches the
    destination between the two merges, so the second merge's
    destination still equals exactly what the first merge itself
    wrote -- re-applying (here, a second, different path from the
    same copy) must still succeed."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "reedit-ok",
                           "full", ("builder",))
    copy_a = copy_root(workspace.config, attempt_id, "builder")
    (copy_a / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")

    rid1 = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
    }, "owner")
    proc = _processor(workspace)
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid1).status == "done"

    # Nobody touches src/app.py; the builder adds a second change on
    # the SAME path (still untouched by anyone else) in a later task.
    (copy_a / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")

    rid2 = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy_a)}],
        "merge_retry_n": 1,
    }, "owner")
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid2).status == "done"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('v2')\n"


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
