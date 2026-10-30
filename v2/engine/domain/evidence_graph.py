"""engine.domain.evidence_graph -- P4 ("증거는 요구사항 ID로 끝까지 묶는다", ENGINE.md
flow-map (라)/(라)묶음): one requirement id must tie, end to end, to every
scene that demonstrates it, that scene's own EXECUTED result (never a
bare claim), the task commit(s) that implement it, and the job's one
security certificate -- all read straight out of the ledger, never
re-derived from a bare "passed" flag without the fingerprint that flag
was produced against.

build() is read-only: it never runs anything, never touches the repo,
and never writes to the ledger. It reads exactly these event kinds,
every one of them already written by some other module before this
one ever runs:
  - 'plan.reviewed' (engine.domain.plan_review.record_plan_reviewed) -- the
    newest one for this job gives the FROZEN manifest (requirement_ids/
    task_ids/probe_target_ids AS THEY WERE the moment a reviewer
    actually judged this plan) that build() walks as its OWN inventory
    (union with whatever ids are in the CALLER'S `plan` argument --
    see _ordered_union's own docstring), rather than trusting `plan`
    (whatever the caller's own load_plan call just read off disk,
    possibly edited since review) as the whole truth on its own: an id
    the frozen manifest has but the current plan does not (removed
    since review) still gets its own entry, marked removed_since_
    review, rather than silently never appearing at all. See
    _latest_plan_reviewed's own docstring for exactly what this catches
    and what it cannot (no 'repo' is passed into build() at all, so it
    can never re-run plan_digest itself -- only the id-membership check
    is possible here).
  - 'acceptance.recorded' (engine.application.acceptance) -- the NEWEST one, among
    this job's own attempts, regardless of whether that run's own
    overall AcceptanceResult.passed was True: a scene's own pass/fail
    is independent of the full suite (or a sibling scene) also having
    to pass for the SAME run to be "accepted" overall, so the newest
    report of each scene's own result must never be withheld just
    because something else in that same run failed. That same event
    also gives the single snapshot fingerprint every scene in that one
    run shared (AcceptanceResult.fingerprint is one value for the
    whole run -- see engine.application.acceptance.run_acceptance's own module
    docstring: every scene/the full suite gets its own fresh snapshot
    of the SAME `before_fp`), and whether THAT run's own `passed` was
    True (EvidenceGraph.acceptance_passed) -- the latter is a SEPARATE
    signal missing() still requires on its own (see missing()'s own
    docstring): a per-requirement scene can individually read "pass"
    while the run that produced it still failed overall (an unrelated
    scene, or the full suite, failed in that same run), and that must
    still refuse release the same as before engine.domain.evidence_graph
    existed.
  - 'app_security.recorded' (engine.application.app_security) -- the NEWEST one,
    among this job's own attempts, gives the job's one security
    certificate (fingerprint + policy_hash) -- but ONLY when that
    newest one's own `passed` is True (see _latest_passing_app_
    security's own docstring: a pass followed by a LATER fail on the
    same source/policy must never resurrect the older pass).
  - 'full_orbit.cursor' (pre-P1) / 'job.position' (P1, engine.application.job_state.
    record_position), both stage="task" -- the only two places
    attempt_id<->task_id is recorded at all; see _task_attempt_ids' own
    docstring for the stability risk this module
    takes on by depending on it.
  - 'commit.recorded' (engine.application.steps.commit) -- the commit sha for
    whichever attempt actually ran a given task.
  - evidence kind "review" (engine.application.steps.review.record, read via
    Ledger.evidence(attempt_id, "review"), never Ledger.events) -- the
    newest NON-STALE row for a task's own attempt gives whether that
    task's own code review actually passed (same staleness rule
    engine.application.release's own full-bench check already applied to "bench"
    evidence: a stale row, e.g. invalidated by a later install, is
    never read as still current).
  - 'probe.executed' (engine.application.acceptance.run_probe_scripts) -- one per
    external target (engine.domain.stage_checks.check_probe_targets, by POSITION id
    "t1", "t2", ...) this job's own attempts ever actually probed; the
    newest one per id, recomputed the same way engine.application.
    acceptance_probe judges it (exit 0, the target's own url named
    alongside the "PROBE OK" marker in the engine-captured output, AND
    the event's own proxy "contacts" showing the target's host
    answered -- engine.domain.probe_observation), must read "ok". The inventory of ids this is checked
    against is the FROZEN plan.reviewed manifest's own
    "probe_target_ids" (facts.md's declared targets AT REVIEW TIME --
    see engine.domain.plan_review.record_plan_reviewed), union whatever this
    job actually probed -- so a target declared at review time but
    NEVER actually probed at all still shows up (EvidenceGraph.
    probe_targets' own ProbeTargetEvidence.ran is False), closing the
    gap docs/flow-map.md's P4 row named ("실측이 대상에 닿았는지 증명
    없음"). A target added to facts.md strictly AFTER the plan was
    last reviewed (never in plan.reviewed's own frozen list, and never
    actually probed either) is NOT caught -- there is no event and no
    frozen entry to hang a check off of; this is the same "drift after
    the last freeze point" class of gap a requirement added after
    review has (see RequirementEvidence.in_reviewed_plan), just with
    no way to even notice the id exists at all without `repo` access,
    which build()'s fixed signature does not have.

missing() is the completeness gate every caller should use instead of
checking any of the above piecemeal: engine.application.release.job_certificate_
status calls build() + missing() and refuses release unless missing()
is empty (see that function's own docstring for exactly which of its
own former per-kind existence checks this replaced). A caller that
instead inspected "is there SOME passing acceptance.recorded event"
and "is there SOME passing app_security.recorded event" separately --
the pre-P4 shape of that same check -- could not tell a requirement
whose only covering scene never actually ran (plan.json edited after
the job's last real acceptance run, or the scene removed since) apart
from a job that is simply fine; missing() can, because it walks every
requirement's own covering scenes/tasks rather than asking only "did
acceptance pass at all, once, for SOME requirement."

engine.application.pipeline._requirements_without_evidence (the runtime half of
engine.domain.plan.validate_plan's own static coverage rule) calls missing()
too, but only keeps lines whose "<id>:" prefix is an actual requirement
id in graph.requirements -- the job-wide lines this module also emits
("plan: no reviewed manifest", "acceptance: no certificate",
"app_security: no certificate", "probe: ...", "task <id>: ...") are
deliberately prefixed with something that is NEVER a requirement id,
so that filter drops them there without full_orbit needing to know
this module grew new job-wide checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from engine.domain.plan import requirement_coverers
from engine.domain.probe_observation import target_reached

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger
    from engine.domain.plan import Plan

# A scene's recorded execution result -- "absent" is never a claim
# anyone wrote; it is this module's own conclusion when no acceptance.
# recorded event (see build()'s own selection rule) ever reported that
# scene id at all.
_PASS, _FAIL, _ABSENT = "pass", "fail", "absent"


@dataclass(frozen=True)
class SceneExecution:
    scene_id: str
    result: str  # "pass" | "fail" | "absent"
    # The acceptance run's own fingerprint (engine.infrastructure.evidence.fingerprint
    # of the snapshot this scene actually ran against) -- "" iff result
    # is "absent" (nothing to have run against). Every scene reported
    # by the SAME acceptance.recorded event always carries the SAME
    # fingerprint (see this module's own docstring); it is still
    # recorded per-scene, not once for the whole graph, so missing()
    # never has to assume that invariant holds and can catch it being
    # violated instead (a graph built by hand, in a test, with one
    # scene's fingerprint edited to differ from the others').
    fingerprint: str = ""


@dataclass(frozen=True)
class RequirementEvidence:
    requirement_id: str
    # plan.json's own covers-claiming scene ids for this requirement,
    # in plan order (engine.domain.plan.requirement_coverers) -- paired
    # one-to-one with `scenes` below.
    scene_ids: tuple[str, ...]
    scenes: tuple[SceneExecution, ...]
    # plan.json's own covers-claiming task ids for this requirement, in
    # plan order -- tracking only (see engine.domain.plan.Task.covers' own
    # docstring and missing()'s own docstring: a requirement's own
    # completeness NEVER depends on these -- EvidenceGraph.tasks is the
    # thing that actually gates on task commit/review, job-wide, not
    # per-requirement). Still carried here for the handoff report's own
    # "덮는 장면/작업" column.
    task_ids: tuple[str, ...]
    # The commit sha of whichever attempt actually ran that task ("" if
    # no commit.recorded event was ever found for it -- see
    # _task_commits' own docstring for why that can legitimately
    # happen). Tracking only, same as task_ids.
    commits: tuple[str, ...]
    # False iff a plan.reviewed event exists for this job (graph.
    # plan_reviewed is True) AND this requirement's own id was NOT in
    # that event's own frozen requirement_ids -- i.e. this requirement
    # was added to plan.json strictly AFTER the plan was last reviewed,
    # so no reviewer ever actually judged its `covers` claims. Always
    # True when graph.plan_reviewed is False (the job-wide "plan: no
    # reviewed manifest" line already covers that case once, so this
    # field never doubles up on it per requirement). Always True for a
    # removed_since_review entry too (irrelevant there; see that
    # field's own docstring).
    in_reviewed_plan: bool = True
    # True iff this id came from the FROZEN plan.reviewed manifest but
    # is no longer in the current `plan` passed to build() at all --
    # i.e. it existed when reviewed and was removed since (see build()'s
    # own docstring on why the inventory is frozen-ids UNION current-
    # ids, never current-ids alone). scene_ids/scenes/task_ids/commits
    # are always empty tuples on a removed entry -- there is nothing
    # left in the current plan to look up coverage for.
    removed_since_review: bool = False


@dataclass(frozen=True)
class TaskEvidence:
    """One plan.json task's own completeness, independent of which (if
    any) requirement it `covers` -- see EvidenceGraph.tasks' own
    docstring for why this is job-wide rather than nested under
    RequirementEvidence."""

    task_id: str
    commit: str  # "" if no commit.recorded event was ever found for it
    # True iff the attempt that ran this task has a non-stale "review"
    # evidence row (Ledger.evidence(attempt_id, "review")) whose own
    # payload["passed"] is True -- False if the task's attempt is
    # unknown (_task_attempt_ids has no entry for it) or has no review
    # evidence at all, never trusted as "nothing recorded, so fine".
    review_passed: bool
    # True iff this task_id came from the FROZEN plan.reviewed manifest
    # but is no longer in the current `plan.tasks` at all -- see
    # RequirementEvidence.removed_since_review's own docstring, same
    # reasoning.
    removed_since_review: bool = False


@dataclass(frozen=True)
class ProbeTargetEvidence:
    """One external target's own probe evidence, keyed by engine.domain.stage_checks.
    probe_target_id (position-based, "t1"/"t2"/...) -- see EvidenceGraph.
    probe_targets' own docstring for where the inventory of ids this
    iterates over comes from."""

    target_id: str
    # True iff at least one 'probe.executed' event was ever recorded
    # for this id among this job's own attempts. False means the
    # target was DECLARED (it is in the frozen plan.reviewed manifest)
    # but never actually probed at all -- the exact gap docs/flow-
    # map.md's P4 row names ("실측이 대상에 닿았는지 증명 없음").
    ran: bool
    # The NEWEST such event's own recomputed "ok" (_probe_ok) -- always
    # False when `ran` is False (nothing to have read ok).
    ok: bool


@dataclass(frozen=True)
class EvidenceGraph:
    job: str
    # True iff a 'plan.reviewed' event (engine.domain.plan.record_plan_
    # reviewed) exists for this job at all -- missing() refuses the
    # job outright when this is False ("plan: no reviewed manifest"):
    # release.py's own _plan_or_empty (a missing/unparseable plan.json
    # degrades to a Plan with zero requirements) would otherwise make
    # this function trivially pass with an empty `requirements` tuple,
    # exactly the "missing manifest becomes a zero-requirement graph
    # that passes" gap this field exists to close.
    plan_reviewed: bool
    # The newest acceptance.recorded event's own fingerprint for this
    # job, regardless of whether that run's own `passed` was True --
    # "" iff no acceptance.recorded event exists at all. Every
    # SceneExecution with result "pass"/"fail" is expected to carry
    # exactly this fingerprint; missing() refuses a "pass" one that
    # does not (see missing()'s own docstring).
    fingerprint: str
    # Whether that SAME newest acceptance.recorded event's own
    # AcceptanceResult.passed was True -- missing() refuses the job
    # outright when this is False, independent of any per-requirement
    # scene check (see missing()'s own docstring for why this is still
    # necessary: a per-requirement scene can read "pass" while the run
    # that produced it failed overall on something unrelated).
    acceptance_passed: bool
    # The job's one security certificate -- the newest PASSING
    # app_security.recorded event's own fingerprint/policy_hash, both
    # "" iff none exists.
    certificate_fingerprint: str
    certificate_policy_hash: str
    # One entry per DISTINCT probe target id this job's own
    # plan.reviewed manifest ever declared (payload["probe_target_ids"]
    # -- engine.domain.plan_review.record_plan_reviewed, facts.md's "## 외부
    # 대상" section AT REVIEW TIME) UNION every id any 'probe.executed'
    # event for this job ever actually used -- the union, not just the
    # frozen list, because an id probed under an older plan.reviewed
    # event shape (recorded before this field existed) must still be
    # checked by whatever it actually ran. A declared id with no
    # matching event at all (ProbeTargetEvidence.ran is False) is the
    # exact gap docs/flow-map.md's P4 row names: a target facts.md
    # named at review time but that was never actually probed must
    # still block release, not silently vanish because build() only
    # ever iterated events that exist. Empty when plan.reviewed has no
    # 'probe_target_ids' at all (an old-shape event, recorded before
    # this field existed -- documented migration gap, see plan_review.
    # record_plan_reviewed's own docstring) AND nothing was ever
    # probed.
    probe_targets: tuple[ProbeTargetEvidence, ...]
    # job-wide, every task in plan.tasks (not filtered by `covers` --
    # see TaskEvidence's own docstring).
    tasks: tuple[TaskEvidence, ...]
    requirements: tuple[RequirementEvidence, ...]
    # a review finding: the newest acceptance.recorded/passing
    # app_security.recorded event's own "evidence_fingerprint" payload
    # field (engine.application.acceptance.AcceptanceResult.evidence_fingerprint /
    # engine.application.app_security.SecurityResult.evidence_fingerprint) --
    # the file-dep-aware value, separate from `fingerprint`/
    # `certificate_fingerprint` above, which stay plain git tree
    # hashes for job_certificate_status's own tree-diff logic. "" for
    # an event recorded before this field existed (a plain "" never
    # matches a real combined value except in the no-file-deps case,
    # where evidence.with_file_deps's own no-op contract makes it
    # equal the plain fingerprint instead -- see job_certificate_
    # status's own use of these two fields for exactly how that old-
    # shape gap is tolerated).
    evidence_fingerprint: str = ""
    certificate_evidence_fingerprint: str = ""


def _latest_acceptance(
    ledger: Ledger, project: str, attempt_ids: frozenset[str],
) -> tuple[str, str, dict[str, bool], bool]:
    """(fingerprint, {scene_id: passed}, overall_passed) of the newest
    'acceptance.recorded' event among `attempt_ids` -- ("", {}, False)
    if no such event exists at all. Deliberately the newest one
    REGARDLESS of its own overall `passed` (unlike engine.application.release's
    pre-P4 _latest_passing_fingerprint, which this replaces): a scene's
    own per-scene pass/fail is independent of the full suite (or a
    sibling scene) also failing in that SAME run, so withholding it
    just because the run's own aggregate `passed` was False would make
    engine.application.handoff's requirement table under-report a scene that
    genuinely passed. `overall_passed` carries that aggregate value
    through separately, for missing()'s own job-wide check (see
    EvidenceGraph.acceptance_passed's own docstring)."""
    rows = [
        (seq, event) for seq, event in ledger.events(project, "acceptance.recorded")
        if event.attempt_id in attempt_ids
    ]
    if not rows:
        return "", "", {}, False
    _seq, latest = max(rows, key=lambda row: row[0])
    fp = latest.payload.get("fingerprint")
    evidence_fp = latest.payload.get("evidence_fingerprint")
    scenes_raw = latest.payload.get("scenes", [])
    scenes: dict[str, bool] = {}
    if isinstance(scenes_raw, list):
        for entry in scenes_raw:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                scenes[entry["id"]] = entry.get("passed") is True
    return (str(fp) if fp else "", str(evidence_fp) if evidence_fp else "",
            scenes, latest.payload.get("passed") is True)


def _latest_passing_app_security(
    ledger: Ledger, project: str, attempt_ids: frozenset[str],
) -> tuple[str, str, str]:
    """(fingerprint, evidence_fingerprint, policy_hash) of the NEWEST 'app_security.recorded'
    event among `attempt_ids` -- but ONLY if that newest one's own
    payload["passed"] is True; ("", "") otherwise, including when no
    such event exists at all, or the newest one carries no non-""
    value for either field (an app_security.recorded event from before
    engine.application.app_security.security_policy_hash existed must refuse the
    same as a real missing certificate, never read as "nothing to
    compare, so it matches").

    Deliberately picks the newest event FIRST, then checks `passed` on
    THAT one -- never "the newest one that happens to have passed": a
    security check that passed, followed by a LATER check on the same
    source that FAILED (e.g. the policy was tightened, or a regression
    was introduced and caught), must certify nothing at all -- the
    older pass is stale the instant a newer check exists, pass or
    fail. Filtering to passed=True first and taking the max seq among
    just those would instead silently resurrect that older pass and
    let release through."""
    rows = [
        (seq, event) for seq, event in ledger.events(project, "app_security.recorded")
        if event.attempt_id in attempt_ids
    ]
    if not rows:
        return "", "", ""
    _seq, latest = max(rows, key=lambda row: row[0])
    if latest.payload.get("passed") is not True:
        return "", "", ""
    fp = latest.payload.get("fingerprint")
    evidence_fp = latest.payload.get("evidence_fingerprint")
    policy_hash = latest.payload.get("policy_hash")
    return (str(fp) if fp else "", str(evidence_fp) if evidence_fp else "",
            str(policy_hash) if policy_hash else "")


def _latest_plan_reviewed(ledger: Ledger, project: str, job: str) -> dict[str, object] | None:
    """The payload of the newest 'plan.reviewed' event for this
    (project, job), or None if none exists. Implemented locally rather
    than importing engine.domain.plan's own identically-shaped private
    `_latest_plan_reviewed` (that module is outside this one's owned
    scope for this task, same reasoning engine.application.release._job_name_for_
    attempt already documents for its own local copy of a
    similarly-shaped lookup)."""
    payload: dict[str, object] | None = None
    for _seq, event in ledger.events(project, "plan.reviewed"):
        if event.payload.get("job") == job:
            payload = event.payload
    return payload


def _task_review_passed(ledger: Ledger, attempt_id: str) -> bool:
    """True iff the NEWEST "review" evidence row (Ledger.evidence,
    never Ledger.events -- engine.application.steps.review.record writes this kind
    through engine.infrastructure.evidence.record, a different table from ledger
    events) for `attempt_id` is both non-stale and payload["passed"] is
    True. False for an attempt with no review evidence at all -- never
    "nothing recorded, so assume fine". Rows come back oldest-first
    (Ledger.evidence's own contract), so the last one is the newest."""
    rows = ledger.evidence(attempt_id, "review")
    if not rows:
        return False
    latest = rows[-1]
    return not latest.stale and latest.payload.get("passed") is True


# Mirrors engine.application.acceptance_probe's own pass rule: the
# legacy output check (exit 0 and "PROBE OK <url>", kept as its own
# local copy) AND the engine's proxy contact records showing the
# target's host actually answered (engine.domain.probe_observation).
# The output check alone proves nothing -- the script prints it itself.
_PROBE_OK_MARK = "PROBE OK"


def _probe_ok(returncode: object, stdout_tail: object, url: object,
              contacts: object) -> bool:
    if not isinstance(url, str) or not isinstance(contacts, list):
        return False
    return (returncode == 0 and f"{_PROBE_OK_MARK} {url}" in str(stdout_tail or "")
            and target_reached(url, contacts))


def _probe_target_results(
    ledger: Ledger, project: str, attempt_ids: frozenset[str],
) -> dict[str, bool]:
    """probe target id -> whether its NEWEST 'probe.executed' event
    (among `attempt_ids`) reads "ok" (_probe_ok) -- one entry per
    DISTINCT id ever recorded for this job, never per event (a target
    reprobed after a fix round must be judged by its latest run only)."""
    latest_seq: dict[str, int] = {}
    results: dict[str, bool] = {}
    for seq, event in ledger.events(project, "probe.executed"):
        if event.attempt_id not in attempt_ids:
            continue
        target_id = event.payload.get("id")
        if not isinstance(target_id, str) or not target_id:
            continue
        if seq < latest_seq.get(target_id, -1):
            continue
        latest_seq[target_id] = seq
        results[target_id] = _probe_ok(
            event.payload.get("returncode"), event.payload.get("stdout_tail"),
            event.payload.get("url"), event.payload.get("contacts"),
        )
    return results


_TASK_POSITION_EVENT_KINDS = ("full_orbit.cursor", "job.position")


def _task_attempt_ids(ledger: Ledger, project: str, job: str) -> dict[str, str]:
    """task_id -> the attempt_id that LAST ran it. Two event kinds both
    record this pairing, stage="task", payload task_id + attempt_id:
    the pre-P1 'full_orbit.cursor' (engine.application.pipeline.record_cursor) and
    the P1 replacement 'job.position' (engine.application.job_state.record_position
    -- same two fields, plus request_id and friends moved under its own
    `extra`, which this function never reads). job.opened never carries
    a task_id at all (see engine.application.steps.open_job.open_job), so these are
    the ONLY place this pairing exists. Both kinds are read together,
    in seq order across the two, so a job whose ledger straddles the P1
    cutover (old cursor events from before, new position events from
    after) still resolves correctly -- later entries for the same
    task_id (engine.application.repair.fix opening a replacement attempt after a
    failed task) overwrite earlier ones regardless of which kind they
    are, since the replacement attempt is the one whose commit actually
    counts.

    Stability risk (report to whoever changes this position-tracking
    next): if BOTH kinds are ever renamed, merged into a different
    event kind, or stop carrying `task_id`/`attempt_id` on their
    stage="task" entries, this function silently returns {} for every
    task (never raises) and every task-covered requirement's `commits`
    comes back empty -- missing() then refuses every such requirement's
    commit evidence, which fails LOUD at release time rather than
    silently trusting an absent commit. The fix, if that happens, is to
    keep emitting an event (any kind) that pairs a task_id with the
    attempt_id that ran it; this function only needs to be pointed at
    the new kind/field names."""
    rows: list[tuple[int, object]] = []
    for kind in _TASK_POSITION_EVENT_KINDS:
        rows.extend(ledger.events(project, kind))
    rows.sort(key=lambda row: row[0])

    task_attempt: dict[str, str] = {}
    for _seq, event in rows:
        if event.payload.get("job") != job or event.payload.get("stage") != "task":
            continue
        task_id = event.payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        attempt_id = event.payload.get("attempt_id") or event.attempt_id
        if isinstance(attempt_id, str) and attempt_id:
            task_attempt[task_id] = attempt_id
    return task_attempt


def _task_commits(
    ledger: Ledger, project: str, job: str, attempt_ids: frozenset[str],
) -> dict[str, str]:
    """task_id -> commit sha ("" if no commit.recorded event was ever
    found for the attempt that ran it -- legitimately possible for a
    task whose own attempt never reached the commit stage at all, e.g.
    it is still in flight, or it failed before merge/review/bench ever
    passed)."""
    task_attempt = _task_attempt_ids(ledger, project, job)
    commit_by_attempt: dict[str, str] = {}
    for _seq, event in ledger.events(project, "commit.recorded"):
        if event.attempt_id in attempt_ids:
            sha = event.payload.get("sha")
            if isinstance(sha, str) and sha:
                commit_by_attempt[event.attempt_id] = sha
    return {
        task_id: commit_by_attempt.get(attempt_id, "")
        for task_id, attempt_id in task_attempt.items()
    }


def _ordered_union(current_ids: list[str], frozen_ids: frozenset[str]) -> list[str]:
    """`current_ids` (plan order, duplicates dropped) followed by any
    `frozen_ids` not already in it (sorted, for a deterministic order
    -- these have no "plan order" of their own any more, since they
    are not in the current plan at all) -- the inventory build() walks
    for requirements/tasks: current ids first (so an ADDED-since-
    review id still reads in the position it has in plan.json today),
    then frozen-but-removed ids appended after (see RequirementEvidence/
    TaskEvidence.removed_since_review's own docstring for why these
    must still get an entry at all)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for cid in current_ids:
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)
    for fid in sorted(frozen_ids):
        if fid not in seen:
            seen.add(fid)
            ordered.append(fid)
    return ordered


def build(ledger: Ledger, project: str, job: str, plan: "Plan") -> EvidenceGraph:
    """The end-to-end evidence graph for `job` (a job_name, NOT an
    attempt_id -- a job's requirements/acceptance/security certificate
    are properties of the whole job across however many attempts it
    took, exactly the scoping engine.application.release.job_certificate_status's
    own docstring already established for this same reason).

    The requirement/task/probe-target INVENTORY this walks is frozen-
    ids UNION current-plan-ids, never current-plan-ids alone (docs/
    flow-map.md 「재설계 반영 상태」 P4 row: "그래프가 고정된 검토 목록이
    아니라 지금 계획 목록을 돌아서, 지운 요구사항·실행 안 된 실측 대상은
    빠짐"): a requirement/task the reviewer judged and that was then
    quietly removed from plan.json must still show up (as
    removed_since_review) for missing() to refuse, and a probe target
    facts.md declared at review time but that no attempt ever actually
    ran a probe.executed event for must still show up (ProbeTargetEvidence.
    ran=False) -- iterating `plan.requirements`/`plan.tasks` alone, the
    pre-this-fix shape, could never produce either entry at all."""
    attempt_ids = frozenset(
        row.attempt_id for row in ledger.attempts_for_job_name(project, job)
    )
    reviewed = _latest_plan_reviewed(ledger, project, job)
    plan_reviewed = reviewed is not None
    frozen_requirement_ids: frozenset[str] = frozenset(
        str(rid) for rid in reviewed.get("requirement_ids", ())
    ) if reviewed is not None else frozenset()
    frozen_task_ids: frozenset[str] = frozenset(
        str(tid) for tid in reviewed.get("task_ids", ())
    ) if reviewed is not None else frozenset()
    frozen_probe_target_ids: frozenset[str] = frozenset(
        str(pid) for pid in reviewed.get("probe_target_ids", ())
    ) if reviewed is not None else frozenset()

    fingerprint, evidence_fingerprint, scene_passed, acceptance_passed = _latest_acceptance(
        ledger, project, attempt_ids)
    cert_fp, cert_evidence_fp, cert_policy_hash = _latest_passing_app_security(
        ledger, project, attempt_ids)
    task_attempt = _task_attempt_ids(ledger, project, job)
    task_commits = _task_commits(ledger, project, job, attempt_ids)
    probe_results = _probe_target_results(ledger, project, attempt_ids)
    coverers = requirement_coverers(plan)

    current_tasks_by_id = {t.id: t for t in plan.tasks}
    tasks: list[TaskEvidence] = []
    for task_id in _ordered_union([t.id for t in plan.tasks], frozen_task_ids):
        if task_id not in current_tasks_by_id:
            tasks.append(TaskEvidence(task_id=task_id, commit="", review_passed=False,
                                      removed_since_review=True))
            continue
        tasks.append(TaskEvidence(
            task_id=task_id,
            commit=task_commits.get(task_id, ""),
            review_passed=(
                _task_review_passed(ledger, task_attempt[task_id])
                if task_id in task_attempt else False
            ),
        ))

    current_requirements_by_id = {r.id: r for r in plan.requirements}
    requirements: list[RequirementEvidence] = []
    for requirement_id in _ordered_union(
        [r.id for r in plan.requirements], frozen_requirement_ids,
    ):
        if requirement_id not in current_requirements_by_id:
            requirements.append(RequirementEvidence(
                requirement_id=requirement_id, scene_ids=(), scenes=(), task_ids=(),
                commits=(), removed_since_review=True,
            ))
            continue
        pairs = coverers.get(requirement_id, ())
        scene_ids = tuple(cid for kind, cid in pairs if kind == "scene")
        task_ids = tuple(cid for kind, cid in pairs if kind == "task")
        scenes = tuple(
            SceneExecution(
                scene_id=sid,
                result=(_PASS if scene_passed.get(sid) is True
                        else _FAIL if sid in scene_passed else _ABSENT),
                fingerprint=fingerprint if sid in scene_passed else "",
            )
            for sid in scene_ids
        )
        commits = tuple(task_commits.get(tid, "") for tid in task_ids)
        in_reviewed_plan = (not plan_reviewed) or requirement_id in frozen_requirement_ids
        requirements.append(RequirementEvidence(
            requirement_id=requirement_id, scene_ids=scene_ids, scenes=scenes,
            task_ids=task_ids, commits=commits, in_reviewed_plan=in_reviewed_plan,
        ))

    probe_targets = tuple(
        ProbeTargetEvidence(
            target_id=target_id, ran=target_id in probe_results,
            ok=probe_results.get(target_id, False),
        )
        for target_id in sorted(frozen_probe_target_ids | set(probe_results))
    )

    return EvidenceGraph(
        job=job, plan_reviewed=plan_reviewed, fingerprint=fingerprint,
        acceptance_passed=acceptance_passed, certificate_fingerprint=cert_fp,
        certificate_policy_hash=cert_policy_hash, probe_targets=probe_targets,
        evidence_fingerprint=evidence_fingerprint,
        certificate_evidence_fingerprint=cert_evidence_fp,
        tasks=tuple(tasks),
        requirements=tuple(requirements),
    )


def missing(graph: EvidenceGraph) -> tuple[str, ...]:
    """One line per broken link -- empty iff release is allowed.

    Per requirement (owner decision: every requirement needs a passing
    SCENE -- the same rule engine.domain.plan.validate_plan already enforces
    statically at review time, requiring at least one scene coverer for
    every requirement; this is the runtime half of that same rule, for
    a plan that bypassed validate_plan, or whose only passing scene
    stopped passing/running since review):
      - refuses unless at least one covering scene has result "pass"
        AND that scene's own fingerprint equals graph.fingerprint --
        the fingerprint check is never skipped even though build() only
        ever stamps a passing scene with graph.fingerprint today (see
        SceneExecution.fingerprint's own docstring for why this is
        still checked explicitly rather than assumed). An "absent"
        scene (never reported by the newest acceptance.recorded event
        at all -- e.g. added to plan.json after the job's last real
        acceptance run) blocks the same as a "fail" one, and so does a
        requirement with NO covering scene at all -- a covering TASK
        (requirement.task_ids/commits) is tracking only (see engine.
        plan.Task.covers' own docstring) and is NEVER sufficient on its
        own, no matter how many covering tasks have a commit: a task's
        own attempt reaching "done" only means its own review/bench
        passed, never that any scene actually exercised the
        requirement's real behavior.

    Also per requirement: refuses if graph.plan_reviewed is True but
    this requirement's own `in_reviewed_plan` is False -- it was added
    to plan.json strictly after the plan was last reviewed, so no
    reviewer ever actually judged its `covers` claims, even if (by
    coincidence, e.g. reusing an existing scene id) it happens to
    report a passing scene. And refuses outright, with its OWN
    "<id>: removed since review" line (never also the "no passing
    scene" line -- there is nothing left to re-check), for a
    requirement_id build() found only in the FROZEN plan.reviewed
    manifest and nowhere in the current plan at all (removed_since_
    review) -- the reviewer's own judgment of that requirement's
    coverage must never simply stop mattering because the id quietly
    disappeared from plan.json afterward.

    Independent of the per-requirement loop above (and checked even
    when plan.requirements is empty -- the "zero requirements" edge
    case engine.domain.plan.validate_plan already refuses at review time, but
    this function never assumes that validation ran):
      - refuses if graph.plan_reviewed is False (no 'plan.reviewed'
        event for this job at all -- "plan: no reviewed manifest").
        Closes the gap where a missing/unparseable plan.json
        (engine.application.release._plan_or_empty's own fallback) used to degrade
        to a Plan with zero requirements and sail through with nothing
        else to check at all.
      - refuses unless the job's own newest acceptance.recorded run
        passed OVERALL (graph.acceptance_passed) and has a passing
        app_security.recorded certificate (graph.certificate_
        fingerprint/certificate_policy_hash both non-""). A
        per-requirement scene can individually read "pass" while the
        SAME run's own aggregate failed on something unrelated (an
        unrelated scene, or the full suite) -- that must still refuse
        release, which is exactly what checking acceptance_passed here
        (rather than re-deriving it from the per-requirement scenes,
        which could not see it at all) catches.
      - refuses, by id, for every entry in graph.probe_targets (the
        FROZEN plan.reviewed manifest's own declared ids UNION every
        id anything actually probed -- see that field's own docstring
        for exactly what inventory this is): "probe <id>: never ran"
        when nothing ever recorded a 'probe.executed' event for it at
        all (declared but never actually probed -- the gap docs/flow-
        map.md's P4 row names), else "probe <id>: not verified" when
        it ran but its newest result did not read "ok".
      - refuses for every plan.tasks entry (graph.tasks, job-wide, NOT
        nested per requirement -- see TaskEvidence's own docstring)
        that has no commit ("task <id>: no commit") or whose own
        review never passed ("task <id>: review did not pass") -- or,
        for one found only in the frozen manifest and removed from the
        current plan since (removed_since_review), its OWN "task <id>:
        removed since review" line instead of those two (same reasoning
        as a removed requirement, above). A requirement already
        satisfied by its own passing scene is UNAFFECTED by a sibling
        task's own incompleteness -- none of these lines (task or
        probe) ever carry a "<requirement_id>:" prefix, so engine.
        full_orbit._requirements_without_evidence's own filter (which
        only keeps lines whose prefix is a real requirement id) drops
        them there; only a caller reading missing()'s raw result
        (engine.application.release.job_certificate_status, engine.interface.board) sees
        them."""
    out: list[str] = []
    if not graph.plan_reviewed:
        out.append("plan: no reviewed manifest")
    for requirement in graph.requirements:
        if requirement.removed_since_review:
            out.append(f"{requirement.requirement_id}: removed since review")
            continue
        passing = [s for s in requirement.scenes if s.result == _PASS]
        matched = [s for s in passing if s.fingerprint and s.fingerprint == graph.fingerprint]
        if matched:
            pass
        elif passing:
            out.append(
                f"{requirement.requirement_id}: scene ran in a different "
                "snapshot than the certificate"
            )
        else:
            out.append(f"{requirement.requirement_id}: no passing scene")
        if not requirement.in_reviewed_plan:
            out.append(f"{requirement.requirement_id}: not in the reviewed plan")
    if not graph.acceptance_passed:
        out.append("acceptance: no certificate")
    if not graph.certificate_fingerprint or not graph.certificate_policy_hash:
        out.append("app_security: no certificate")
    for target in graph.probe_targets:
        if not target.ran:
            out.append(f"probe {target.target_id}: never ran")
        elif not target.ok:
            out.append(f"probe {target.target_id}: not verified")
    for task in graph.tasks:
        if task.removed_since_review:
            out.append(f"task {task.task_id}: removed since review")
            continue
        if not task.commit:
            out.append(f"task {task.task_id}: no commit")
        if not task.review_passed:
            out.append(f"task {task.task_id}: review did not pass")
    return tuple(out)
