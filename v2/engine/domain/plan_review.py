"""plan.json's digest (its own bytes plus every tests/scenes/ file any
scene's cmd references) and the ledger-side record of when a plan was
last reviewed -- record_plan_reviewed, is_plan_reviewed,
verify_reviewed_scenes, check_resumed_plan. Built on top of
engine.domain.plan_model (Plan/PlanError/plan_path/load_plan/validate_plan)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain.records import Event
from engine.domain.plan_model import PlanError, load_plan, plan_path, validate_plan

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


def _file_sha256(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def scene_file_digests(path: Path) -> dict[str, str]:
    """{scene file path (exactly as it appears in that scene's own
    `cmd`, relative to the repo) -> sha256 hex digest of its current
    bytes}, one entry per DISTINCT tests/scenes/ file any scene's cmd
    in the plan.json at `path` references -- "" for a referenced file
    that cannot be read (missing/unreadable), never raising, so a
    scene script that disappears after review still changes the
    digest plan_digest folds this into, rather than silently keeping
    the old one. {} if plan.json itself cannot be read/parsed (the
    same tolerant shape plan_digest needs: a resume must always get
    SOME digest to compare, never an exception, even for a job whose
    plan.json went missing between attempts)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    repo = path.parent.parent.parent  # repo/.gongbang/<job>/plan.json
    scene_paths: set[str] = set()
    for scene in raw.get("scenes", []) if isinstance(raw.get("scenes"), list) else []:
        if not isinstance(scene, dict):
            continue
        cmd = scene.get("cmd", [])
        if not isinstance(cmd, list):
            continue
        for token in cmd:
            if isinstance(token, str) and token.startswith("tests/scenes/"):
                scene_paths.add(token)
    return {p: _file_sha256(repo / p) for p in sorted(scene_paths)}


def plan_digest(path: Path) -> str:
    """sha256 hex digest over plan.json's own raw bytes (at `path`)
    plus every distinct tests/scenes/ file any scene's cmd references
    (scene_file_digests, folded in sorted path order as
    "<path>:<sha256>") -- a later edit to plan.json OR to any scene
    script it names changes this digest. engine.application.pipeline's own
    record_plan_reviewed stores this (plus the per-file breakdown)
    in a plan.reviewed ledger event right after plan review passes; a
    resume (engine.application.pipeline.continue_full_job) recomputes it and
    compares against that recorded value to decide whether the plan
    needs reviewing again. Never raises: an unreadable plan.json folds
    in as b"", same tolerant shape scene_file_digests already uses, so
    a resume always has SOME digest to compare rather than crashing."""
    try:
        plan_bytes = path.read_bytes()
    except OSError:
        plan_bytes = b""
    hasher = hashlib.sha256(plan_bytes)
    for scene_path, file_digest in scene_file_digests(path).items():
        hasher.update(f"\n{scene_path}:{file_digest}".encode("utf-8"))
    return hasher.hexdigest()


def _probe_target_ids_at_review(repo: Path, job: str) -> list[str]:
    """engine.domain.stage_checks.probe_target_id(i) for every external target
    facts.md declares for this job RIGHT NOW (engine.domain.stage_checks.
    check_probe_targets) -- the FROZEN inventory engine.domain.evidence_graph.
    build() checks every recorded 'probe.executed' event against (P4,
    docs/flow-map.md 「재설계 반영 상태」 P4 row: "실측이 대상에 닿았는지
    증명 없음" -- a declared target that was never actually probed at
    all must still show up as missing, not silently absent). [] for
    every outcome check_probe_targets' own docstring treats as "no
    targets at all" (no facts.md yet, or its "## 외부 대상" section is
    the explicit "(없음)" marker) AND for every malformed-section
    outcome it raises ProbeTargetsError for (a missing heading, an
    empty section, a bad bullet line, or both a real target and the
    empty marker at once) -- record_plan_reviewed must never raise
    just because facts.md looks broken at review time; a plan review
    reaching this point already means the probe station (gated on
    check_probe_targets succeeding) ran first, so this should only
    ever legitimately return [] or the real target count, never
    actually hit one of those error branches in practice."""
    from engine.domain.stage_checks import ProbeTargetsError, check_probe_targets, probe_target_id

    try:
        targets = check_probe_targets(repo, job)
    except ProbeTargetsError:
        return []
    return [probe_target_id(i) for i in range(len(targets))]


def record_plan_reviewed(ledger: Ledger, project: str, attempt_id: str,
                         repo: Path, job: str) -> None:
    """Appends one plan.reviewed ledger event ({"job": job, "digest":
    plan_digest(...), "scene_digests": scene_file_digests(...),
    "requirement_ids": [...], "task_ids": [...], "probe_target_ids":
    [...]}) for this (project, job) attempt -- engine.application.pipeline calls
    this once, right after the planner's own plan review Stage comes
    back "done" (ENGINE.md: plan -> plan review -> commit), so
    plan.reviewed always names the exact plan.json + scene files +
    declared external targets that review actually judged, never a
    later edit. A resume reads the latest such event for this job back
    and recomputes plan_digest against the CURRENT plan.json/scene
    files to decide whether the plan changed since it was last
    reviewed (see plan_digest's own docstring). requirement_ids/
    task_ids (plan order, read via load_plan -- [] if the plan cannot
    be loaded at all, which should never happen here since review
    already ran against this same plan.json) are the ids
    check_resumed_plan compares against a LATER plan.json to catch a
    requirement or task quietly removed (rather than changed) since
    this review -- a case plan_digest alone would also catch (any edit
    changes the digest), but naming exactly which id disappeared is
    more useful than just "the plan changed". probe_target_ids
    (_probe_target_ids_at_review, facts.md's own declared targets at
    THIS moment, in position order) is engine.domain.evidence_graph.build()'s
    own frozen inventory for probe evidence -- the same reasoning,
    applied to a target that was declared but never actually probed at
    all, which plan_digest alone cannot catch (facts.md, not
    plan.json, is what it reads)."""
    path = plan_path(repo, job)
    try:
        plan = load_plan(repo, job)
        requirement_ids = [r.id for r in plan.requirements]
        task_ids = [t.id for t in plan.tasks]
    except PlanError:
        requirement_ids, task_ids = [], []
    ledger.append(Event(
        project=project, kind="plan.reviewed", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job, "digest": plan_digest(path),
                 "scene_digests": scene_file_digests(path),
                 "requirement_ids": requirement_ids, "task_ids": task_ids,
                 "probe_target_ids": _probe_target_ids_at_review(repo, job)},
    ))


def _latest_plan_reviewed(ledger: Ledger, project: str,
                          job_name: str) -> dict[str, object] | None:
    """The payload of the latest "plan.reviewed" ledger event recorded
    for this (project, job_name), or None if there is none -- the one
    place both check_resumed_plan and verify_reviewed_scenes read this
    back from, so the two can never disagree about which event is
    "the latest" one."""
    payload: dict[str, object] | None = None
    for _seq, event in ledger.events(project, "plan.reviewed"):
        if event.payload.get("job") == job_name:
            payload = event.payload
    return payload


def is_plan_reviewed(ledger: Ledger, project: str, job_name: str) -> bool:
    """True iff a plan.reviewed event has EVER been recorded for this
    (project, job_name) -- the one ledger-side, un-tamperable signal
    engine.application.acceptance.run_acceptance uses to decide whether this job's
    Plan must be loaded fresh from that call's own certified snapshot
    (never the live repo, and never whatever Plan object a caller
    already parsed earlier) rather than trusting a caller-supplied Plan
    object as-is.

    Deliberately NOT based on whether the CURRENT plan.json still has
    any scene claiming `covers` (the previous gating condition both
    here and in run_acceptance): a plan.json edited after review to
    simply remove every `covers` entry is exactly the kind of edit
    plan_digest/verify_reviewed_scenes exists to catch, but a check
    gated on "does the (possibly already-edited) content still claim
    coverage" would skip itself entirely on precisely that edit --
    stripping `covers` would have silently disabled the one check that
    exists to catch stripping `covers`. Whether a plan was EVER
    reviewed lives only in the ledger, which an edit to the repo's own
    plan.json cannot touch.

    Public wrapper around _latest_plan_reviewed so a caller outside
    this module never has to reach for that private name (the pattern
    engine.application.pipeline's own `_facts_path` import used to need)."""
    return _latest_plan_reviewed(ledger, project, job_name) is not None


def verify_reviewed_scenes(ledger: Ledger, project: str, job_name: str,
                           snapshot: Path) -> list[str]:
    """[] iff plan.json's CURRENT plan_digest (plan.json's own bytes
    plus every tests/scenes/ file any scene's cmd references), read
    from `snapshot`, still matches the latest plan.reviewed event's
    digest for this (project, job_name) -- the narrow, cheap check
    engine.application.acceptance.run_acceptance runs against EACH STAGE'S OWN
    disposable snapshot, right after that snapshot is built and before
    anything in it executes, never against the live project repo: a
    check against the repo and an execution against a snapshot built
    moments later are two separate reads of two different (if usually
    identical) things, so only checking the exact bytes about to run
    -- the snapshot itself -- proves a scene script (or plan.json
    itself) edited after review, even within the SAME attempt with no
    owner-stop/resume involved at all, can never be certified as if a
    reviewer had actually seen it. This is the single-purpose digest
    half of check_resumed_plan, factored out on its own: run_acceptance
    has no reason to also re-run the full validate_plan sweep (build
    prefixes, requirement coverage, dependency cycles -- all already
    enforced once at plan review and again at every resume point) on
    every single acceptance call.

    "plan_never_reviewed" if no plan.reviewed event exists at all;
    "plan_changed_since_review" if the digest no longer matches. Never
    raises -- plan_digest's own tolerant shape always yields SOME
    digest to compare, even for a plan.json that cannot be read."""
    reviewed = _latest_plan_reviewed(ledger, project, job_name)
    if reviewed is None:
        return ["plan_never_reviewed"]
    if reviewed.get("digest") != plan_digest(plan_path(snapshot, job_name)):
        return ["plan_changed_since_review"]
    return []


def check_resumed_plan(ledger: Ledger, project: str, job_name: str, repo: Path) -> list[str]:
    """[] iff the CURRENT plan.json (+ scene files) for `job_name` at
    `repo` is still, in every sense that matters, the plan a reviewer
    last approved -- the check every resume point in engine.application.pipeline/
    engine.application.quick_pipeline must run before trusting a job's plan to keep
    going with build/merge/commit work under it -- every resume point
    must re-check this, not just STAGE_DESIGN_APPROVAL, so a task/
    security/cursor resume never accepts a plan.json edited since
    review with no check at all.

    Problems, in order:
      - plan.json fails to load/parse: [f"plan:{PlanError.reason}"],
        nothing else is checked (there is no Plan to check further)
      - validate_plan(plan, repo, build_prefixes=None) -- every
        prefix-INDEPENDENT structural rule (scene cmd shape, scene
        file existence, requirement/scene/task coverage, dependency
        cycles); build_prefixes=None skips the owns-subset check,
        since this function has no config of its own to get a real
        build_prefixes from (a caller that also needs the owns check
        still has validate_plan itself, called with the real one)
      - "plan_never_reviewed" if no "plan.reviewed" ledger event exists
        yet for this (project, job_name) -- a resume reaching a point
        that needs a reviewed plan with none on record is itself a
        problem, not something to silently pass
      - "plan_changed_since_review" if the plan's current plan_digest
        no longer matches the latest plan.reviewed event's digest --
        plan.json or some scene file it references was edited since
        that review
      - "requirement_removed_since_review: <id>" / "task_removed_
        since_review: <id>" for any requirement/task id the latest
        plan.reviewed event recorded (record_plan_reviewed's own
        requirement_ids/task_ids) that is no longer in the CURRENT
        plan -- reported even when, for some reason, the digest check
        above did not already catch the same edit, so a shrunk plan is
        never accepted on an id-by-id basis either.

    Never raises. [] means the caller may trust this job's plan.json
    as-is and keep going; any other return is a reason to stop and
    treat the job as needs_owner, the same way a first-pass plan
    review failure already does."""
    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        return [f"plan:{exc.reason}"]

    problems = validate_plan(plan, repo, None)

    reviewed = _latest_plan_reviewed(ledger, project, job_name)
    if reviewed is None:
        problems.append("plan_never_reviewed")
        return problems

    if reviewed.get("digest") != plan_digest(plan_path(repo, job_name)):
        problems.append("plan_changed_since_review")

    reviewed_requirement_ids = tuple(reviewed.get("requirement_ids", ()))
    reviewed_task_ids = tuple(reviewed.get("task_ids", ()))
    current_requirement_ids = {r.id for r in plan.requirements}
    current_task_ids = {t.id for t in plan.tasks}
    for rid in reviewed_requirement_ids:
        if rid not in current_requirement_ids:
            problems.append(f"requirement_removed_since_review: {rid}")
    for tid in reviewed_task_ids:
        if tid not in current_task_ids:
            problems.append(f"task_removed_since_review: {tid}")
    return problems


__all__ = [
    "scene_file_digests", "plan_digest", "record_plan_reviewed", "is_plan_reviewed",
    "verify_reviewed_scenes", "check_resumed_plan",
]
