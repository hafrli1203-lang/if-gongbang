"""Unit tests for engine.evidence_graph: build() ties plan.requirements
to their covering scenes/tasks and the job's one security certificate,
purely from existing ledger events (acceptance.recorded, app_security.
recorded, full_orbit.cursor, commit.recorded); missing() is the
completeness gate engine.release.job_certificate_status (and engine.
handoff's requirement table, and engine.board's "완료") all defer to.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from engine import evidence_graph
from engine.evidence_graph import RequirementEvidence, SceneExecution
from engine.ledger import Event
from engine.plan import Plan, Requirement, Scene, Task
from engine.steps.open_job import open_job
from tests.conftest import Workspace


def _open(ws: Workspace, job_name: str) -> str:
    return open_job(ws.config, ws.ledger, "alpha", job_name, "full", roles=())


def _seed_acceptance(ws: Workspace, attempt_id: str, job: str, *,
                     passed: bool, scenes: list[dict], fingerprint: str) -> None:
    ws.ledger.append(Event(
        project="alpha", kind="acceptance.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job, "passed": passed, "scenes": scenes,
                 "full_suite_passed": passed, "tree_changed": False,
                 "reason": None, "fingerprint": fingerprint},
    ))


def _seed_app_security(ws: Workspace, attempt_id: str, *,
                       fingerprint: str = "sec-fp", policy_hash: str = "sec-hash",
                       passed: bool = True) -> None:
    ws.ledger.append(Event(
        project="alpha", kind="app_security.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"passed": passed, "findings": [], "ran": ["secret_scan"],
                 "fingerprint": fingerprint, "policy_hash": policy_hash},
    ))


def _seed_task_commit(ws: Workspace, job: str, attempt_id: str, task_id: str,
                      sha: str) -> None:
    """Mirrors engine.full_orbit.record_cursor's own 'full_orbit.cursor'
    (stage="task") + engine.steps.commit's own 'commit.recorded' --
    the only two event kinds engine.evidence_graph reads to tie a
    covering TASK to a commit sha (see evidence_graph._task_attempt_ids'
    own docstring)."""
    ws.ledger.append(Event(
        project="alpha", kind="full_orbit.cursor", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job, "stage": "task", "task_id": task_id,
                 "next_action": "run_station", "attempt_id": attempt_id},
    ))
    ws.ledger.append(Event(
        project="alpha", kind="commit.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": "r1", "sha": sha, "fp": "tree-fp"},
    ))


def _seed_task_commit_via_job_position(ws: Workspace, job: str, attempt_id: str,
                                       task_id: str, sha: str) -> None:
    """Same pairing as _seed_task_commit, but through the P1 event kind
    (engine.position.record_position's own 'job.position') instead of
    the pre-P1 'full_orbit.cursor' -- a ledger that only ever wrote
    job.position (a project that started after the P1 cutover) must
    resolve the task<->commit link exactly the same way."""
    ws.ledger.append(Event(
        project="alpha", kind="job.position", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job, "stage": "task", "attempt_id": attempt_id,
                 "task_id": task_id, "remaining_task_ids": [], "fixing": False,
                 "fix": None, "pending_requests": [], "stop_reason": None, "extra": {}},
    ))
    ws.ledger.append(Event(
        project="alpha", kind="commit.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": "r1", "sha": sha, "fp": "tree-fp"},
    ))


def _seed_plan_reviewed(ws: Workspace, job: str, *,
                        requirement_ids: tuple[str, ...] = (),
                        task_ids: tuple[str, ...] = ()) -> None:
    """Mirrors engine.plan.record_plan_reviewed's own 'plan.reviewed'
    event shape -- the FROZEN manifest engine.evidence_graph.build()
    checks the live `plan` argument's requirement ids against (see
    build()'s own docstring, 'plan.reviewed' bullet)."""
    ws.ledger.append(Event(
        project="alpha", kind="plan.reviewed", actor_kind="processor",
        actor_id="processor", attempt_id=None,
        payload={"job": job, "digest": "stub-digest", "scene_digests": {},
                 "requirement_ids": list(requirement_ids), "task_ids": list(task_ids)},
    ))


def _seed_task_review(ws: Workspace, attempt_id: str, *, passed: bool = True,
                      fp: str = "review-fp", stale: bool = False) -> None:
    """Mirrors engine.steps.review._run_record_review's own "review"
    evidence row (engine.evidence.record, a different table from plain
    ledger events -- see evidence_graph._task_review_passed's own
    docstring). `stale` marks it invalidated right after (engine.
    evidence.invalidate_after), the same "checked-then-invalidated"
    shape a later install step produces."""
    from engine import evidence

    evidence.record(ws.ledger, "alpha", attempt_id, "review", fp,
                    payload={"passed": passed, "reviewer_passed": passed,
                             "findings": [], "coverage": "full",
                             "reviewer_coverage": "full", "request_id": "r-review"})
    if stale:
        evidence.invalidate_after(ws.ledger, "alpha", attempt_id, ("review",))


def _seed_probe(ws: Workspace, attempt_id: str, target_id: str, url: str, *,
                ok: bool = True) -> None:
    """Mirrors engine.acceptance.run_probe_scripts' own 'probe.executed'
    event -- 'ok' is never a stored field (see evidence_graph._probe_ok's
    own docstring); this seeds returncode/stdout_tail so recomputing it
    yields `ok`."""
    tail = f"PROBE OK {url}" if ok else "boom"
    ws.ledger.append(Event(
        project="alpha", kind="probe.executed", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"id": target_id, "name": target_id, "script": "probe.py",
                 "returncode": 0 if ok else 1, "stdout_tail": tail,
                 "url": url, "sha256": "x" * 64, "fingerprint": "probe-fp"},
    ))


def _plan_two_requirements() -> Plan:
    """R1 is covered by scene s1; R2 is covered ONLY by task t1 (never
    a scene) -- used by the tests that specifically exercise task-only
    coverage's own commit-linkage/"never sufficient" behavior."""
    return Plan(
        goal="x", ui=False,
        requirements=(
            Requirement(id="R1", text="scene-covered", kind="function"),
            Requirement(id="R2", text="task-covered", kind="function"),
        ),
        scenes=(Scene(id="s1", actor_does="a", app_shows="b", where="cli",
                     cmd=(sys.executable, "-c", "pass"), covers=("R1",)),),
        tasks=(Task(id="t1", title="x", detail="", owns=("src/",), depends_on=(),
                   covers=("R2",)),),
        acceptance=("x",),
    )


def _plan_one_requirement() -> Plan:
    """R1 covered by scene s1 only -- used by tests whose point has
    nothing to do with task-only coverage, so a complete graph can
    actually be complete (owner decision: task-only coverage is NEVER
    sufficient, so _plan_two_requirements' own R2 can never be made to
    pass missing() no matter what is seeded for it)."""
    return Plan(
        goal="x", ui=False,
        requirements=(Requirement(id="R1", text="scene-covered", kind="function"),),
        scenes=(Scene(id="s1", actor_does="a", app_shows="b", where="cli",
                     cmd=(sys.executable, "-c", "pass"), covers=("R1",)),),
        tasks=(), acceptance=("x",),
    )


def _plan_one_requirement_and_one_task() -> Plan:
    """R1 covered by scene s1 (satisfied independently of any task);
    task t1 covers nothing at all -- isolates the job-wide task
    commit/review check (EvidenceGraph.tasks) from per-requirement
    scene coverage, since a task's own incompleteness must never
    change whether R1 itself reads as covered."""
    return Plan(
        goal="x", ui=False,
        requirements=(Requirement(id="R1", text="scene-covered", kind="function"),),
        scenes=(Scene(id="s1", actor_does="a", app_shows="b", where="cli",
                     cmd=(sys.executable, "-c", "pass"), covers=("R1",)),),
        tasks=(Task(id="t1", title="x", detail="", owns=("src/",), depends_on=()),),
        acceptance=("x",),
    )


# ---------------------------------------------------------------------
# build(): ties requirement -> scene/task -> commit -> certificate
# ---------------------------------------------------------------------

def test_build_reports_a_passing_scene_with_its_run_fingerprint(workspace: Workspace) -> None:
    attempt_id = _open(workspace, "job1")
    plan = _plan_two_requirements()
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)
    _seed_task_commit(workspace, "job1", attempt_id, "t1", "c" * 40)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)

    by_id = {r.requirement_id: r for r in graph.requirements}
    assert by_id["R1"].scenes == (SceneExecution("s1", "pass", "fp-1"),)
    assert by_id["R2"].commits == ("c" * 40,)
    assert graph.acceptance_passed is True
    assert graph.certificate_fingerprint == "sec-fp"
    assert graph.certificate_policy_hash == "sec-hash"


def test_build_resolves_task_commit_from_job_position_alone(workspace: Workspace) -> None:
    """P1: a ledger that only ever wrote 'job.position' (never
    'full_orbit.cursor' at all) must still resolve the task<->commit
    link the same way -- R2's commit is recorded regardless (still
    useful, tracking-only, data), even though task-only coverage never
    satisfies missing() on its own (R1's scene makes it the only one
    that matters for completeness here)."""
    attempt_id = _open(workspace, "job1")
    plan = _plan_two_requirements()
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)
    _seed_task_commit_via_job_position(workspace, "job1", attempt_id, "t1", "d" * 40)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)

    by_id = {r.requirement_id: r for r in graph.requirements}
    assert by_id["R2"].commits == ("d" * 40,)
    gaps = evidence_graph.missing(graph)
    assert not any(gap.startswith("R1:") for gap in gaps)
    assert "R2: no passing scene" in gaps


def test_build_reports_absent_for_a_scene_the_latest_run_never_mentions(
    workspace: Workspace,
) -> None:
    attempt_id = _open(workspace, "job1")
    plan = _plan_two_requirements()
    # The latest acceptance.recorded run reports a DIFFERENT scene
    # entirely (as if plan.json's covers-claiming scene were added
    # after this run) -- s1 itself is never mentioned at all.
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "unrelated", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)

    by_id = {r.requirement_id: r for r in graph.requirements}
    assert by_id["R1"].scenes == (SceneExecution("s1", "absent", ""),)


# ---------------------------------------------------------------------
# missing(): the completeness gate
# ---------------------------------------------------------------------

def test_a_complete_graph_passes(workspace: Workspace) -> None:
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement()
    _seed_plan_reviewed(workspace, "job1", requirement_ids=("R1",))
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)

    assert evidence_graph.missing(graph) == ()


def test_a_complete_graph_with_a_fully_evidenced_task_and_probe_passes(
    workspace: Workspace,
) -> None:
    """End-to-end happy path exercising every new P1 check at once:
    reviewed plan, passing scene, a task with both a commit and a
    passing non-stale review, and a probed external target that read
    ok -- missing() must still be empty."""
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement_and_one_task()
    _seed_plan_reviewed(workspace, "job1", requirement_ids=("R1",), task_ids=("t1",))
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)
    _seed_task_commit(workspace, "job1", attempt_id, "t1", "c" * 40)
    _seed_task_review(workspace, attempt_id, passed=True)
    _seed_probe(workspace, attempt_id, "t1", "https://example.com", ok=True)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)

    assert evidence_graph.missing(graph) == ()


def test_a_requirement_whose_scene_is_absent_blocks_release(workspace: Workspace) -> None:
    """A requirement's covering scene was never even reported by the
    job's newest acceptance.recorded run (plan.json edited, or the
    scene removed, since the last real run) -- missing() must refuse
    it the same as a scene that actually failed, never silently treat
    'never ran' as 'nothing to worry about'."""
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement()
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[], fingerprint="fp-1")  # s1 absent
    _seed_app_security(workspace, attempt_id)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)
    gaps = evidence_graph.missing(graph)

    assert "R1: no passing scene" in gaps


def test_a_scene_that_passed_in_a_different_snapshot_than_the_certificate_blocks(
) -> None:
    """A regression guard on missing() itself, independent of build():
    a SceneExecution reporting "pass" must still be refused if its own
    fingerprint does not match the graph's -- missing() never trusts a
    bare "pass" result without also checking the snapshot it claims to
    have run in. (Organically, build() always stamps every scene from
    the SAME acceptance.recorded event with that event's own single
    fingerprint -- see SceneExecution.fingerprint's own docstring --
    so this scenario is only reachable by constructing the graph
    directly, exactly as this test does, never through build() on a
    real ledger.)"""
    graph = evidence_graph.EvidenceGraph(
        job="job1", plan_reviewed=True, fingerprint="fp-certificate",
        acceptance_passed=True, certificate_fingerprint="sec-fp",
        certificate_policy_hash="sec-hash", probe_ok=True, tasks=(),
        requirements=(
            RequirementEvidence(
                requirement_id="R1", scene_ids=("s1",),
                scenes=(SceneExecution("s1", "pass", "fp-stale"),),
                task_ids=(), commits=(),
            ),
        ),
    )

    gaps = evidence_graph.missing(graph)

    assert len(gaps) == 1
    assert gaps[0].startswith("R1:")
    assert "snapshot" in gaps[0]


def test_a_task_only_requirement_is_always_missing_even_with_a_commit(
    workspace: Workspace,
) -> None:
    """Owner decision: every requirement needs a passing SCENE. A
    requirement covered only by a task (never a scene) must stay
    "missing" even when that task has a real, recorded commit --
    task-only coverage is tracking only (engine.plan.Task.covers' own
    docstring), never sufficient on its own."""
    attempt_id = _open(workspace, "job1")
    plan = _plan_two_requirements()
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)
    _seed_task_commit(workspace, "job1", attempt_id, "t1", "c" * 40)  # R2's task, committed

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)
    gaps = evidence_graph.missing(graph)

    assert "R2: no passing scene" in gaps
    assert not any(gap.startswith("R1:") for gap in gaps)


def test_a_security_pass_followed_by_a_later_fail_blocks_release(workspace: Workspace) -> None:
    """An older PASSING app_security.recorded event must never be
    resurrected once a NEWER one on the same source/policy failed --
    the newest record wins, full stop, and only certifies anything
    when IT passed."""
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement()
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id, fingerprint="fp-old", policy_hash="hash-old",
                       passed=True)
    _seed_app_security(workspace, attempt_id, fingerprint="fp-new", policy_hash="hash-new",
                       passed=False)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)

    assert graph.certificate_fingerprint == ""
    assert graph.certificate_policy_hash == ""
    gaps = evidence_graph.missing(graph)
    assert "app_security: no certificate" in gaps


def test_no_security_certificate_blocks_even_with_every_requirement_covered(
    workspace: Workspace,
) -> None:
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement()
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    # No app_security.recorded event seeded at all.

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)
    gaps = evidence_graph.missing(graph)

    assert not any(gap.startswith("R1:") for gap in gaps)
    assert "app_security: no certificate" in gaps


def test_overall_failed_run_blocks_even_if_the_covering_scene_itself_passed(
    workspace: Workspace,
) -> None:
    """acceptance_passed (the newest run's own AGGREGATE outcome) is
    checked independently of any single requirement's own scene(s):
    a sibling scene or the full suite failing in that same run must
    still refuse release, even though R1's own covering scene s1 read
    "pass" within it."""
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement()
    _seed_acceptance(workspace, attempt_id, "job1", passed=False,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)
    gaps = evidence_graph.missing(graph)

    assert not any(gap.startswith("R1:") for gap in gaps)
    assert "acceptance: no certificate" in gaps


# ---------------------------------------------------------------------
# Frozen reviewed plan, task commit+review, probe evidence -- a
# missing/malformed manifest must never become a zero-requirement
# graph that trivially passes.
# ---------------------------------------------------------------------

def test_no_plan_reviewed_event_blocks_release(workspace: Workspace) -> None:
    """engine.release._plan_or_empty degrades a missing/unparseable
    plan.json to a Plan with zero requirements -- without this check,
    that empty graph would have nothing left to refuse and missing()
    would read as complete."""
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement()
    # No 'plan.reviewed' event ever seeded.
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)

    assert graph.plan_reviewed is False
    assert "plan: no reviewed manifest" in evidence_graph.missing(graph)


def test_no_plan_reviewed_event_with_zero_requirements_still_blocks(
    workspace: Workspace,
) -> None:
    """The exact gap reported: a plan.json so broken load_plan raises
    (release._plan_or_empty's own fallback: zero requirements, zero
    tasks) must still refuse -- never silently pass just because there
    is nothing left to check per-requirement."""
    from engine.plan import Plan as EmptyPlan

    attempt_id = _open(workspace, "job1")
    empty_plan = EmptyPlan(goal="", ui=False, scenes=(), tasks=(), acceptance=())

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", empty_plan)
    gaps = evidence_graph.missing(graph)

    assert gaps != ()
    assert "plan: no reviewed manifest" in gaps


def test_requirement_added_after_review_blocks_even_with_a_passing_scene(
    workspace: Workspace,
) -> None:
    """R1 was reviewed; R2 was added to plan.json strictly AFTER that
    review (never in the frozen requirement_ids) -- even though R2's
    own scene s2 genuinely passed in the newest acceptance run, no
    reviewer ever judged that R2 was actually covered by it, so it
    must still refuse."""
    attempt_id = _open(workspace, "job1")
    plan = Plan(
        goal="x", ui=False,
        requirements=(
            Requirement(id="R1", text="reviewed", kind="function"),
            Requirement(id="R2", text="added after review", kind="function"),
        ),
        scenes=(
            Scene(id="s1", actor_does="a", app_shows="b", where="cli",
                 cmd=(sys.executable, "-c", "pass"), covers=("R1",)),
            Scene(id="s2", actor_does="a", app_shows="b", where="cli",
                 cmd=(sys.executable, "-c", "pass"), covers=("R2",)),
        ),
        tasks=(), acceptance=("x",),
    )
    _seed_plan_reviewed(workspace, "job1", requirement_ids=("R1",))  # R2 not frozen
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}, {"id": "s2", "passed": True}],
                     fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)
    gaps = evidence_graph.missing(graph)

    by_id = {r.requirement_id: r for r in graph.requirements}
    assert by_id["R2"].scenes[0].result == "pass"  # the scene itself genuinely passed
    assert "R2: not in the reviewed plan" in gaps
    assert not any(gap.startswith("R1:") for gap in gaps)


def test_task_without_a_commit_blocks_release_job_wide(workspace: Workspace) -> None:
    """A task with no commit.recorded event at all blocks release even
    though the requirement it has nothing to do with (R1) already has
    its own passing scene -- EvidenceGraph.tasks is job-wide, not
    nested under whichever requirement happens to already be covered."""
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement_and_one_task()
    _seed_plan_reviewed(workspace, "job1", requirement_ids=("R1",), task_ids=("t1",))
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)
    # t1 never committed at all.

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)
    gaps = evidence_graph.missing(graph)

    assert not any(gap.startswith("R1:") for gap in gaps)
    assert "task t1: no commit" in gaps


def test_task_without_a_passing_review_blocks_release_job_wide(workspace: Workspace) -> None:
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement_and_one_task()
    _seed_plan_reviewed(workspace, "job1", requirement_ids=("R1",), task_ids=("t1",))
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)
    _seed_task_commit(workspace, "job1", attempt_id, "t1", "c" * 40)
    # No review evidence seeded for t1's attempt at all.

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)
    gaps = evidence_graph.missing(graph)

    assert not any(gap.startswith("R1:") for gap in gaps)
    assert "task t1: review did not pass" in gaps


def test_task_with_stale_review_evidence_blocks_release(workspace: Workspace) -> None:
    """A review that passed but was later invalidated (engine.evidence.
    invalidate_after -- e.g. a dependency/install change after review)
    must never be read as still current."""
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement_and_one_task()
    _seed_plan_reviewed(workspace, "job1", requirement_ids=("R1",), task_ids=("t1",))
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)
    _seed_task_commit(workspace, "job1", attempt_id, "t1", "c" * 40)
    _seed_task_review(workspace, attempt_id, passed=True, stale=True)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)
    gaps = evidence_graph.missing(graph)

    assert "task t1: review did not pass" in gaps


def test_probe_target_that_never_verified_blocks_release(workspace: Workspace) -> None:
    """An external target this job actually probed, but whose script
    never printed the PROBE OK marker for its own url, blocks release
    job-wide -- independent of every requirement's own scene status."""
    attempt_id = _open(workspace, "job1")
    plan = _plan_one_requirement()
    _seed_plan_reviewed(workspace, "job1", requirement_ids=("R1",))
    _seed_acceptance(workspace, attempt_id, "job1", passed=True,
                     scenes=[{"id": "s1", "passed": True}], fingerprint="fp-1")
    _seed_app_security(workspace, attempt_id)
    _seed_probe(workspace, attempt_id, "t1", "https://example.com", ok=False)

    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)
    gaps = evidence_graph.missing(graph)

    assert not any(gap.startswith("R1:") for gap in gaps)
    assert "probe: external target not verified" in gaps


# ---------------------------------------------------------------------
# cross-module consistency: engine.handoff's requirement table renders
# from this SAME graph (P4: "one place decides coverage, not two")
# ---------------------------------------------------------------------

def test_handoff_table_status_equals_the_graphs_own_per_requirement_result(
    workspace: Workspace,
) -> None:
    import json

    from engine.acceptance import run_acceptance
    from engine.handoff import write_handoff
    from engine.plan import load_plan, plan_path, record_plan_reviewed
    from tests.fixtures.sample_app import make_sample

    sample = make_sample(workspace.root / "sample", 1, Path(sys.executable))
    from dataclasses import replace
    project_config = replace(workspace.config.projects["alpha"],
                             repo=sample.repo, test_cmd=sample.test_cmd)
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project_config})

    attempt_id = open_job(config, workspace.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    scenes_dir = sample.repo / "tests" / "scenes"
    scenes_dir.mkdir(parents=True)
    (scenes_dir / "s1.py").write_text(
        "import sys; sys.path.insert(0, 'src')\n"
        "from greeter import greet\n"
        "assert greet('World') == 'Hello, World'\n",
        encoding="utf-8",
    )
    data = {
        "goal": "x", "ui": False,
        "scenes": [{"id": "s1", "actor_does": "x", "app_shows": "x", "where": "cli",
                   "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]}],
        "tasks": [{"id": "t1", "title": "x", "detail": "", "owns": ["src/"],
                   "depends_on": []}],
        "acceptance": ["x"],
        "requirements": [{"id": "R1", "text": "x", "kind": "function"}],
    }
    path = plan_path(sample.repo, "job1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    record_plan_reviewed(workspace.ledger, "alpha", attempt_id, sample.repo, "job1")
    plan = load_plan(sample.repo, "job1")

    acc = run_acceptance(config, workspace.ledger, "alpha", attempt_id, plan)
    graph = evidence_graph.build(workspace.ledger, "alpha", "job1", plan)

    report_path = write_handoff(config, workspace.ledger, "alpha", attempt_id, "job1",
                                plan, acc)
    text = report_path.read_text(encoding="utf-8")
    r1_line = next(line for line in text.splitlines() if line.startswith("| R1"))

    r1_evidence = next(r for r in graph.requirements if r.requirement_id == "R1")
    graph_says_passed = all(s.result == "pass" for s in r1_evidence.scenes)

    assert ("통과" in r1_line) == graph_says_passed
    assert graph_says_passed is True  # the scene genuinely ran and passed
