"""The full orbit (docs/v2.md 「궤도 9칸」, on top of the existing single-
attempt engine): run one job all the way from a plain-text request to a
usable app with a hand-off report, or stop cleanly for the owner (a
design to approve, an install/approval a builder station asked for) and
resume later.

Stations, in order, each writing only its own files:
  facts    (role "facts")    -> .gongbang/<job>/facts.md
  probe    (role "prober", only if facts.md's own "## 외부 대상"
           section lists at least one external API/CLI/file format
           this job depends on) -> .gongbang/<job>/probe/ -- one real
           call per target, logged with "PROBE OK"/"PROBE FAIL"
           (engine.gates.check_probe_targets/check_probe). Skipped
           entirely (no attempt, just a "probe.skipped" ledger event)
           when facts.md names no such target.
  planner  (role "planner")  -> .gongbang/<job>/plan.json, tests/scenes/
  designer (role "designer", only if plan.ui) -> .gongbang/<job>/design/
  builder  (role "builder", once per plan.tasks entry, in dependency
           order) -> the task's own `owns` paths, narrowed from the
           project's configured build prefixes (engine.policy,
           engine.steps.station) -- never tests/scenes/. A UI task
           additionally gets write access to this one job's own
           .gongbang/<job>/screens/ (see _config_for_build_task below)
           to save screenshots of what it built, without widening any
           other builder-writable path.
  reviewer (role "reviewer") judges each builder task's change, same as
           a plain single-task job (engine.steps.review)

Facts/probe/planner/(designer) share ONE attempt (run_station -> merge
-> a gate-specific check -- engine.gates / engine.plan -- for each),
closed once the plan is validated and (if plan.ui) the design is
approved. facts.md/probe logs/plan.json/design.md are committed
directly (see
_commit_job_trail) right after each one's merge, rather than waiting
for a worker-gated commit -- nothing a worker wrote is in that diff,
only files the orchestrator itself just wrote, so there is nothing for
a reviewer or the test suite to re-judge.

EACH build task (and each acceptance-fix round) then runs in its OWN
FRESH attempt: open_job -> run_station(build) -> merge -> review ->
bench -> commit -> report -> close (the same seven-stage shape
engine.orbit.default_stages() already uses for a plain single-task
job). A fresh attempt per task, rather than reusing one across all of
them, is required by engine.steps.merge's own conflict check, which
compares a copy's changes against the ATTEMPT's base_head (fixed at
open_job time), never the project's live HEAD -- reusing one attempt
across several tasks' own commits would make every task after the
first look like it conflicts with "someone else's committed change"
(that change being its OWN earlier sibling task, in the very same
job). engine.fixup.py already handles this exact base_head fact for a
fix/resume attempt the same way: close, then open_job() again, never
continue on the old attempt.

A design that is not yet approved, or a run_station that asked the
owner for something (e.g. an install), stops the job with
FullOutcome(status="needs_owner", ...) -- `continue_full_job` resumes
it later. Progress needed to resume (which stage, which task, which
attempt) is journaled as a "full_orbit.progress" ledger event; nothing
about it lives only in this process's memory. Which attempt to resume
is always "the latest attempt for this job_name" (engine.ledger.
Ledger.attempts_for_job_name) -- the same attempt the progress event
itself was journaled against, since a needs_owner stop never closes
the attempt it stopped in.

`processor_factory`, on both entry points, is the same optional seam
engine.orbit.run_job/continue_job already expose (a callable building
the engine.processor.Processor each stage list runs against) -- plain
production callers (tools/gb2) never pass it (defaults to a real
Processor with register_defaults()); it exists so a test can register
a deterministic run_station/review stub instead of spawning a real
worker/reviewer process, the same way tests/unit/test_orbit.py already
does for engine.orbit itself. It is NOT part of the fixed full-orbit
interface contract -- gb2 never needs to pass it -- but adding an
optional keyword-only parameter does not change the documented
positional/keyword call shape either.

Known limitation: if the project has a `release` track configured
(EngineConfig.projects[...].release), engine.orbit's own commit-stage
special case (engine.orbit._run_stages) stops EVERY task's own attempt
at needs_owner right after its commit, not only the last -- this
module does not special-case that away. A release-configured project
driven through the full orbit today needs an owner to approve/skip-ship
after each task, same as a plain single-task job would."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from engine import context as context_mod
from engine import orbit
from engine.config import EngineConfig
from engine.gates import _facts_path as _facts_md_path
from engine.gates import (
    ProbeTargetsError, check_design_approved, check_facts, check_probe_targets,
)
from engine.gitutil import git_argv
from engine.ledger import Event, Ledger
from engine.owner import PENDING_KIND_WHITELIST
from engine.plan import (
    Plan, PlanError, Task, classify_skill_tools, load_plan, ordered_tasks, plan_path,
    requirement_coverers, skill_catalog_text, tools_for_station,
    validate_plan, validate_tools,
)
from engine.processor import Fakes
from engine.station_prompts import (
    builder_task_text, designer_prompt, facts_prompt, fix_task_text, planner_prompt,
    prober_prompt,
)
from engine.steps.open_job import copy_root, dirty_paths, open_job

FACTS_ROLE = "facts"
PROBER_ROLE = "prober"
PLANNER_ROLE = "planner"
DESIGNER_ROLE = "designer"
BUILDER_ROLE = "builder"
REVIEWER_ROLE = "reviewer"

# full_orbit.progress "stage" values a needs_owner stop may record --
# continue_full_job dispatches on exactly these (see _RESUME_TABLE).
STAGE_DESIGN_APPROVAL = "awaiting_design_approval"
STAGE_TASK_OWNER = "awaiting_task_owner"
STAGE_PROBE_RETRY = "awaiting_probe_retry"
STAGE_PLAN_REVIEW = "awaiting_plan_review"
STAGE_REQUIREMENTS_REVIEW = "awaiting_requirements_review"
STAGE_SECURITY_FIX = "awaiting_security_fix"
STAGE_RELEASE_APPROVAL = "awaiting_release_approval"
# A facts/probe/plan/design station's own run_station stop (an owner
# request, or orbit's own exhausted-continuation "worker_stuck") --
# distinct from each station's own CONTENT-level retry stage
# (STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW/STAGE_DESIGN_APPROVAL, which only
# ever fire once that station's run_station has already finished and
# merged). Before this stage existed, a stop here recorded nothing at
# all, so continue_full_job's own _load_progress found no pending stop
# and raised FullOrbitError -- even though the job was genuinely just
# waiting on an owner.
STAGE_EARLY_STATION = "awaiting_early_station_owner"

# Characters of one probe log kept for the planner prompt's own "실측
# 결과" section -- bounds a single huge/looping log from blowing up the
# planner's prompt budget (same spirit as engine.context's PER_ITEM_CAP).
PROBE_LOG_CAP = 4000

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class FullOutcome:
    status: str  # "done" | "needs_owner" | "failed"
    reason: str | None
    attempt_id: str | None
    report_path: str | None


class FullOrbitError(ValueError):
    """Nothing to resume (continue_full_job called with no pending stop
    recorded for this job) or the job's attempt cannot be found."""


def _facts_md_text(repo, job_name: str) -> str:
    try:
        return _facts_md_path(repo, job_name).read_text(encoding="utf-8")
    except OSError:
        return ""


def record_release_decision(ledger: Ledger, project: str, attempt_id: str, job_name: str,
                            decision: str) -> None:
    """Journals a `full_orbit.release_decision` event -- the one durable
    record of how this job's release track ended: "not_configured" (no
    `release` in this project's config -- _maybe_release_or_done's own
    straight-to-done path), "shipped" (the pending publish request
    reached "done"), "skipped" (the owner abandoned it -- `skip-ship`,
    a full-bench refusal, a cancel-before-dispatch, or a security fix
    superseding it -- see finalize_release's own _RELEASE_ABANDON_
    REASONS), or "failed" (the publish request reached a terminal
    "failed"/"unknown" status for any OTHER reason -- a real deploy/
    fast-forward failure). `finalize_release` is the one place that
    decides which of these four applies and is the only caller that
    should ever invoke this directly; `ship`/`skip-ship`/continue_full_
    job's own STAGE_RELEASE_APPROVAL resume all go through it rather
    than calling this themselves, so the decision logic lives in
    exactly one place. Idempotent in effect (not in storage): callers
    check `_release_decision_already_recorded` first, since this
    itself always appends a fresh event. Without this, a release-
    configured job's own completion was only ever visible as engine.
    orbit's own JobOutcome from that one `continue_job` call -- nothing
    on the ledger ever said the WHOLE full-orbit job (not just this one
    attempt) had reached a terminal state, and a project with no
    release configured reached "done" with no record at all of why
    there was nothing left to approve."""
    ledger.append(Event(
        project=project, kind="full_orbit.release_decision", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job_name, "decision": decision},
    ))


# Every reason a publish request's own "result" dict carries when it was
# driven to a terminal non-"done" status by a deliberate owner/engine
# ABANDON -- `cancel_pending_publish`/`cancel_running_publish_before_
# dispatch` (engine.release), called with each of these exact strings --
# never a reason recorded by an actual dispatch failure (e.g.
# advance_master/call_publisher raising StepFailed with some OTHER
# reason, like "artifact_mismatch" or a real deploy exit code). Read by
# finalize_release to tell "the owner chose not to ship this" (decision
# "skipped") apart from "the release genuinely failed" (decision
# "failed") -- both reach the SAME terminal request status
# ("failed"/"unknown"), so the status alone can never tell them apart.
_RELEASE_ABANDON_REASONS = frozenset({
    "skip_ship", "full_bench_failed", "cancelled_before_dispatch", "superseded_by_fix",
})


def _release_decision_for_request(request_row: "object") -> str:
    """"shipped" (status "done"), "skipped" (a terminal non-"done"
    status whose own result.reason is one of _RELEASE_ABANDON_REASONS),
    or "failed" (terminal non-"done" for any other reason -- a real
    deploy/fast-forward failure, e.g. "artifact_mismatch" or a
    nonzero-exit deploy script)."""
    if request_row.status == "done":  # type: ignore[attr-defined]
        return "shipped"
    result = request_row.result  # type: ignore[attr-defined]
    reason = result.get("reason") if isinstance(result, dict) else None
    if reason in _RELEASE_ABANDON_REASONS:
        return "skipped"
    return "failed"


def _release_decision_already_recorded(ledger: Ledger, project: str, job_name: str,
                                       attempt_id: str) -> bool:
    for _seq, event in ledger.events(project, "full_orbit.release_decision"):
        if event.attempt_id == attempt_id and event.payload.get("job") == job_name:
            return True
    return False


def finalize_release(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                     attempt_id: str, job_name: str, request_id: str, report_path: str,
                     on_progress: ProgressCallback | None = None,
                     processor_factory: "orbit.ProcessorFactory | None" = None) -> FullOutcome:
    """The ONE place a release-configured job's publish request --
    shipped (`gb2 ship` drove it to "done"), abandoned (`gb2 skip-ship`,
    a full-bench refusal, a cancel-before-dispatch, or a security fix
    superseding it), or genuinely failed (any other terminal non-"done"
    status) -- is turned into this job's final FullOutcome: report ->
    close (_close_attempt, idempotent -- engine.processor.submit/
    run_once's own content-hash idempotency makes a second call on an
    already-"done" report/close request a no-op) THEN record_release_
    decision (idempotent in effect: never a second event for the same
    (project, job_name, attempt_id), see _release_decision_already_
    recorded), in that fixed order, no matter which of the three ways
    the request actually ended. `gb2 ship`/`gb2 skip-ship` (tools/gb2)
    and continue_full_job's own STAGE_RELEASE_APPROVAL resume (a crash
    between the publish request reaching a terminal status and this
    function ever running) all call this instead of duplicating the
    decide-close-record sequence themselves -- before this existed, a
    crash in that exact window left the project OPEN forever (close
    never ran) with no release_decision ever recorded, and a ship that
    ended in a genuine failure (as opposed to "done") skipped report/
    close and the decision record entirely, the same orphaned-OPEN way.

    request_id's own status not yet terminal (still "queued"/
    "running") is a plain no-op: returns FullOutcome("needs_owner",
    "release_approval", attempt_id, None) without touching the ledger
    at all -- the owner has not finished approving/rejecting yet (a
    `gb2 ship` call that typed the wrong approval line, for instance,
    leaves the request exactly like this; calling this function again
    after the owner fixes that is always safe)."""
    request_row = ledger.request(request_id)
    if request_row.status not in ("done", "failed", "unknown"):
        return FullOutcome("needs_owner", "release_approval", attempt_id, None)
    decision = _release_decision_for_request(request_row)
    # _close_attempt's own report/close stages are each individually
    # idempotent (engine.processor.submit/run_once's content-hash
    # idempotency, which now also tolerates reusing an already-
    # submitted request on a no-longer-active attempt -- see
    # engine.processor.submit's own docstring), so calling it again on a
    # job this function (or a same-process caller's own earlier
    # _close_attempt) already closed is a safe no-op. What actually
    # needs skipping is the whole project already reaching CLOSED --
    # checking the ATTEMPT's own status instead (as this used to) is
    # wrong: engine.steps.close's set_closed flips the attempt to
    # "closed"/"abandoned" as its own FIRST effect, before the project's
    # own CLOSING -> CLOSED transition (its second effect) necessarily
    # ran -- a crash in exactly that window left the attempt non-active
    # but the project stuck CLOSING forever, and the old check here
    # silently treated that as "already closed", never finishing it.
    if ledger.project(project).state != "CLOSED":
        close_outcome = _close_attempt(config, ledger, fakes, project, attempt_id, job_name,
                                       on_progress, processor_factory)
        if close_outcome.status != "done":
            return FullOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None)
    if not _release_decision_already_recorded(ledger, project, job_name, attempt_id):
        record_release_decision(ledger, project, attempt_id, job_name, decision)
    if decision == "failed":
        return FullOutcome("failed", "release_rejected", attempt_id, None)
    return FullOutcome("done", None, attempt_id, report_path)


def _record_progress(ledger: Ledger, project: str, attempt_id: str, job_name: str,
                     **fields: object) -> None:
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job_name, "attempt_id": attempt_id, **fields},
    ))


def record_cursor(ledger: Ledger, project: str, job_name: str, *, stage: str,
                  task_id: str | None = None, next_action: str,
                  attempt_id: str | None = None, **extra: object) -> None:
    """Journals a `full_orbit.cursor` event -- a breadcrumb written
    BEFORE every stage/task transition (facts, probe, plan, design,
    each task's build/merge/review/bench/commit, acceptance, handoff,
    security, release), unlike `full_orbit.progress` (only ever written
    when a station or task stops needs_owner for the owner). `stage` is
    one of "facts"/"probe"/"plan"/"design" (the shared early attempt),
    "task" (one build or acceptance-fix task, `task_id` set), or
    "post_tasks" (acceptance -> handoff -> security -> release, once
    every task is done). `next_action` is a short human label (e.g.
    "run_station", "merge", "review", "bench", "commit", "acceptance",
    "handoff", "security", "release") -- read back only for
    observability (`gb2 status`/`gb2 board`), never branched on by
    `continue_full_job` itself, which dispatches on `stage` alone.

    continue_full_job falls back to the LATEST cursor for `job_name`
    whenever there is no pending `full_orbit.progress` stop recorded at
    all (see _load_cursor/_resume_from_cursor) -- an ordinary "failed"
    outcome (engine.full_orbit never records progress for those, only
    for needs_owner stops) or a crash (the process died mid-stage,
    never reaching whatever WOULD have recorded progress) both leave
    exactly this situation. Without a cursor, either case left
    `continue_full_job` with nothing to resume from at all (a plain
    FullOrbitError, "no pending stop recorded") even though the job's
    own ledger/working tree already has everything needed to pick back
    up.

    `small_orbit` calls this same function for its own one-task
    "small" jobs, with stage "facts"."""
    ledger.append(Event(
        project=project, kind="full_orbit.cursor", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job_name, "stage": stage, "task_id": task_id,
                 "next_action": next_action, "attempt_id": attempt_id, **extra},
    ))


def _load_cursor(ledger: Ledger, project: str, job_name: str) -> dict[str, object] | None:
    """The most recently journaled `full_orbit.cursor` payload for
    `job_name` (across every attempt -- a cursor is written before an
    attempt even exists yet for some stages, e.g. "task" before
    open_job runs), or None if none was ever recorded (a job started
    before this field existed, or `job_name` is simply unknown)."""
    latest: dict[str, object] | None = None
    for _seq, event in ledger.events(project, "full_orbit.cursor"):
        if event.payload.get("job") == job_name:
            latest = dict(event.payload)
    return latest


def _record_early_station_stop(ledger: Ledger, project: str, attempt_id: str, job_name: str,
                               station: str, outcome: "orbit.JobOutcome", *, request: str,
                               max_fix_rounds: int, probe_text: str = "") -> None:
    """The ONE place a facts/probe/plan/design station's own run_station
    stop is journaled (see STAGE_EARLY_STATION's own comment) --
    `station` is one of "facts"/"probe"/"plan"/"design", read back by
    continue_full_job's STAGE_EARLY_STATION dispatch to know which
    function to resume. `probe_text` is only ever non-empty for a
    "plan"/"design" stop (probe already succeeded and committed by
    then, so a resume must not rerun it) -- station=="plan" reads it
    back; station=="design" does not need it (plan.json is already
    committed by the time design runs, so a design resume reloads the
    plan from disk instead of needing probe_text at all)."""
    _record_progress(ledger, project, attempt_id, job_name,
                     stage=STAGE_EARLY_STATION, resume_at=STAGE_EARLY_STATION,
                     station=station, stop_reason=outcome.reason, request=request,
                     max_fix_rounds=max_fix_rounds, probe_text=probe_text,
                     pending_requests=list(outcome.pending_requests))


def _load_progress(ledger: Ledger, project: str, attempt_id: str,
                   job_name: str) -> dict[str, object] | None:
    latest: dict[str, object] | None = None
    for _seq, event in ledger.events(project, "full_orbit.progress"):
        if event.payload.get("attempt_id") == attempt_id and event.payload.get("job") == job_name:
            latest = dict(event.payload)
    return latest


def pending_owner_requests(ledger: Ledger, project: str, job_name: str) -> tuple[dict, ...]:
    """The station requests (e.g. {"kind": "install", "manifest": ...,
    "change": ...}) a job is currently stopped on -- a thin, PUBLIC
    read-only wrapper for tools/gb2's own `approve-install` to show the
    owner what a station is actually waiting on, without reaching into
    this module's (or engine.small_orbit's) private progress/cursor
    format itself.

    Checks `full_orbit.progress` first (a full job's own STAGE_
    TASK_OWNER build-task stop or STAGE_EARLY_STATION facts/probe/
    plan/design stop -- see _record_early_station_stop; both record
    "pending_requests" the same way, so one field read covers either).
    () if the job's latest attempt has no pending progress recorded at
    all, that progress's own stage is neither of those two, or it
    carries no "pending_requests" (e.g. a worker_stuck stop, which has
    nothing to approve).

    Falls back to the latest `full_orbit.cursor` (record_cursor) when
    progress gives nothing: engine.small_orbit's own combined planning
    call deliberately never writes a `full_orbit.progress` event at
    all for its own owner-request stops (see engine.small_orbit's own
    module docstring) -- only a cursor at stage "small_plan", carrying
    "pending_requests" the exact same shape. Without this fallback, a
    small job's own pending install request was invisible to this
    function -- `gb2 approve-install` reported "no_pending_install_
    request" even while one was genuinely outstanding, and `gb2
    continue` stayed blocked forever waiting for an approval the owner
    was never shown (codex-diff-review-s18.md's own finding)."""
    attempts = ledger.attempts_for_job_name(project, job_name)
    if not attempts:
        return ()
    attempt_id = attempts[-1].attempt_id
    progress = _load_progress(ledger, project, attempt_id, job_name)
    if progress is not None and progress.get("stage") in (STAGE_TASK_OWNER, STAGE_EARLY_STATION):
        pending = progress.get("pending_requests")
        if isinstance(pending, list):
            return tuple(req for req in pending if isinstance(req, dict))
    cursor = _load_cursor(ledger, project, job_name)
    if cursor is not None and cursor.get("stage") == "small_plan":
        pending = cursor.get("pending_requests")
        if isinstance(pending, list):
            return tuple(req for req in pending if isinstance(req, dict))
    return ()


_ORCHESTRATOR_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "processor", "GIT_AUTHOR_EMAIL": "processor@local",
    "GIT_COMMITTER_NAME": "processor", "GIT_COMMITTER_EMAIL": "processor@local",
}


def _latest_merge_paths(ledger: Ledger, project: str, attempt_id: str) -> tuple[str, ...]:
    """The exact relative paths engine.steps.merge's own "record" step
    applied in the MOST RECENT merge.recorded event on this attempt
    (that event's own "paths" payload, engine.steps.merge._record) --
    not every path under some owned prefix that happens to exist on
    disk. facts/probe/plan/design all share one attempt (see this
    module's own docstring), each running its own run_station -> merge
    in turn, so "most recent" at the moment _commit_job_trail is called
    right after a merge always means THAT station's own merge, never an
    earlier sibling's."""
    latest: tuple[str, ...] = ()
    for _seq, event in ledger.events(project, "merge.recorded"):
        if event.attempt_id == attempt_id:
            latest = tuple(str(p) for p in event.payload.get("paths", ()))
    return latest


def _commit_job_trail(config: EngineConfig, ledger: Ledger, project: str, attempt_id: str | None,
                      job_name: str, message: str,
                      station: str | None = None, role: str | None = None) -> None:
    """Commit whatever the orchestrator itself (never a worker) just
    wrote under .gongbang/<job>/ -- a merge's own apply_allowed_paths
    step already landed facts.md/plan.json/design.md/report.md there
    as plain, uncommitted files in the real repo, and leaving them
    uncommitted would both (a) make the NEXT station's own base_head
    stale (see this module's docstring) and (b) let engine.orbit's own
    "close" stage (git checkout/clean to HEAD) wipe them right back
    out. This is a plain, UNGATED engine-side commit -- unlike
    engine.steps.commit, it never checks a review/bench fingerprint,
    because nothing a worker wrote is in this diff; only files the
    processor itself just wrote (plus, when `station`/`role` is given,
    exactly the paths THIS station's own merge just applied outside
    .gongbang/ -- see _latest_merge_paths; never every path that
    happens to exist under the role's whole owned prefix, which could
    sweep in some unrelated pre-existing uncommitted change under that
    same prefix, e.g. from the owner's own working tree or a different
    job's leftover file). A no-op (nothing actually changed under
    .gongbang/<job>/) commits nothing.

    The commit itself is scoped to exactly `paths` via a trailing `--
    <paths>` pathspec (codex-goal-review-4.md item 4 / codex-diff-
    review-s17.md "Ungated trail commits can absorb unrelated staged
    source", engine/steps/open_job.py:215 and this function): a bare
    `git commit` with no pathspec commits EVERY currently staged
    change, not just what the `git add` right above this staged --
    anything the owner (or anything else) had already `git add`ed
    under some OTHER path before this ran would be swept into this
    engine-authored, ungated commit right alongside it. Passing the
    same `paths` list to `commit` that `add` just used means only
    those paths' content is committed; any other already-staged path
    is left exactly as staged as it was, untouched, for whatever
    later, real commit (engine.steps.commit's gated one, or the owner's
    own) is actually supposed to include it."""
    repo = config.projects[project].repo
    paths = [f".gongbang/{job_name}"]
    if station and role:
        # Other paths this station's role owns (e.g. the planner's
        # tests/scenes/) were merged by the same gated step; leaving them
        # uncommitted would let the close stage wipe them -- but only the
        # paths that merge actually touched, not the whole owned prefix.
        owned = config.station_owners.get(station, {}).get(role, ())
        extra_prefixes = tuple(p for p in owned if not p.startswith(".gongbang"))
        if extra_prefixes:
            merged = _latest_merge_paths(ledger, project, attempt_id)
            paths += [
                p for p in merged
                if any(p.startswith(prefix) for prefix in extra_prefixes) and (repo / p).exists()
            ]
    env = {**os.environ, **_ORCHESTRATOR_GIT_IDENTITY}
    subprocess.run(git_argv(repo, "add", "--", *paths), check=True, capture_output=True, env=env)
    status = subprocess.run(
        git_argv(repo, "status", "--porcelain", "--", *paths),
        capture_output=True, text=True, env=env,
    ).stdout
    if not status.strip():
        return
    subprocess.run(git_argv(repo, "commit", "-q", "-m", message, "--", *paths),
                   check=True, capture_output=True, env=env)


def _build_prefixes(config: EngineConfig) -> tuple[str, ...]:
    return tuple(config.station_owners.get("build", {}).get(BUILDER_ROLE, ()))


def _probe_logs_text(repo, job_name: str, targets: list[tuple[str, str]]) -> str:
    """Every target's own probe log (capped at PROBE_LOG_CAP chars
    each), under one "## 실측 결과" heading -- "" if `targets` is empty
    or none of the logs can be read (check_probe already gated on
    them existing and saying PROBE OK before this is ever called, so
    a read failure here would mean the file vanished in between)."""
    parts: list[str] = []
    from engine.gates import probe_target_id

    for i, (name, _url) in enumerate(targets):
        path = repo / ".gongbang" / job_name / "probe" / f"{probe_target_id(i)}.log"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parts.append(f"### {name}\n{text[:PROBE_LOG_CAP]}")
    if not parts:
        return ""
    return "## 실측 결과\n" + "\n\n".join(parts)


def _config_for_build_task(config: EngineConfig, job_name: str, plan: Plan) -> EngineConfig:
    """`config`, unchanged, except (only when plan.ui) the build
    station's builder role additionally owns THIS job's own
    .gongbang/<job_name>/screens/ -- never the rest of
    .gongbang/<job_name>/ (facts.md, plan.json, report.md stay out of
    reach) and never another job's screens/, so a UI task's build
    attempt can save its own screenshots there without widening any
    other builder-writable path. Used only for the run_station/merge
    stages of that one task's own attempt (see _run_task_attempt) --
    every other stage (review/bench/commit/...) only reads fields this
    never touches, so passing it there too would be harmless but is
    avoided for clarity."""
    if not plan.ui:
        return config
    screens_prefix = f".gongbang/{job_name}/screens/"
    build_owners = dict(config.station_owners.get("build", {}))
    build_owners[BUILDER_ROLE] = tuple(build_owners.get(BUILDER_ROLE, ())) + (screens_prefix,)
    station_owners = {**config.station_owners, "build": build_owners}
    return replace(config, station_owners=station_owners)


def _config_without_release(config: EngineConfig, project: str) -> EngineConfig:
    """`config`, unchanged, except this project's own `release` is None --
    used for every PER-TASK attempt (each build task and each fix round,
    see _run_task_attempt/continue_full_job's awaiting_task_owner resume)
    so engine.orbit's own commit-stage release special case
    (_run_stages: "a project with `release` configured stops EVERY
    task's own attempt at needs_owner right after its commit") never
    fires mid-job. The full-orbit contract makes exactly ONE publish
    request for the whole job, submitted directly by
    _maybe_release_or_done once every task + acceptance + the security
    check have all passed -- never one per task."""
    if config.projects[project].release is None:
        return config
    stripped = replace(config.projects[project], release=None)
    return replace(config, projects={**config.projects, project: stripped})


def config_without_release(config: EngineConfig, project: str) -> EngineConfig:
    """Public wrapper around _config_without_release -- for a caller
    OUTSIDE this module (tools/gb2's `fix` subcommand) that must strip a
    project's `release` before repairing one task of a multi-task
    full-orbit job via engine.fixup.fix, for the exact same reason every
    per-task attempt inside this module already does (see
    _config_without_release's own docstring): fixup.fix() drives its
    repaired task through orbit.fix_stages -> orbit.continue_job, the
    SAME commit-stage release special case _config_without_release
    exists to dodge, and fixup.py has no awareness of "this task belongs
    to a bigger job" at all."""
    return _config_without_release(config, project)


def is_full_orbit_job(ledger: Ledger, project: str, job_name: str) -> bool:
    """True iff `job_name` was ever driven through run_full_job/
    continue_full_job -- i.e. a full_orbit.cursor was ever recorded for
    it (record_cursor; a plain single-task `gb2 run`/`gb2 fix` job never
    gets one). Used by tools/gb2's `fix` subcommand to decide whether
    the attempt it is about to repair belongs to a multi-task job (and
    so must have its own per-task release disabled -- see
    config_without_release -- before fixup.fix repairs it) or is itself
    the whole job (where stopping needs_owner for release approval
    right after this one task's own commit is the correct, intended
    behavior, not something to suppress)."""
    return _load_cursor(ledger, project, job_name) is not None


def _uncovered_requirement_ids(plan: Plan, acc: object) -> tuple[str, ...]:
    """requirement ids (plan.requirements, every kind alike) not
    actually covered once acceptance has run: covered by AT LEAST ONE
    SCENE that passed. A task's own `covers` is tracking only (the
    handoff report's own "도구표 대 실제 사용"-style section reads it)
    and is NEVER sufficient here, even for a task that finished "done"
    -- "done" only means that task's own review/bench passed, never
    that any scene actually exercised the requirement's real behavior
    (a function requirement used to count as
    satisfied by task coverage alone). validate_plan already refuses a
    plan where some requirement has no SCENE coverer at all (static
    check, at plan-load time, tightened the same way) -- this is the
    runtime half: a requirement whose only covering scene actually
    FAILED still reads as uncovered here, even though the plan itself
    was valid."""
    if not plan.requirements:
        return ()
    coverers = requirement_coverers(plan)
    passed_scene_ids = {s.id for s in acc.scenes if s.passed}  # type: ignore[attr-defined]
    uncovered: list[str] = []
    for requirement in plan.requirements:
        covered = any(
            kind == "scene" and cid in passed_scene_ids
            for kind, cid in coverers.get(requirement.id, ())
        )
        if not covered:
            uncovered.append(requirement.id)
    return tuple(uncovered)


def _maybe_release_or_done(config: EngineConfig, ledger: Ledger, project: str, job_name: str,
                           attempt_id: str, report_path: Path, max_fix_rounds: int) -> FullOutcome:
    """The LAST step of a whole job (ENGINE.md release track, an earlier round
    contract item 6): a project with no `release` configured reaches
    "done" straight away, exactly as before. A project WITH `release`
    configured instead submits ONE publish request for the whole job
    (engine.release.prepare, bound to `attempt_id`'s own current repo
    HEAD -- by now that HEAD already carries every task's commits plus
    the handoff commit) and stops needs_owner "release_approval";
    gb2's ship/skip-ship flow (outside this module) drives that request
    to done/failed, after which continue_full_job's own
    STAGE_RELEASE_APPROVAL branch turns it into this job's final
    FullOutcome."""
    record_cursor(ledger, project, job_name, stage="post_tasks", next_action="release",
                  attempt_id=attempt_id, max_fix_rounds=max_fix_rounds)
    release_config = config.projects[project].release
    if release_config is None:
        record_release_decision(ledger, project, attempt_id, job_name, "not_configured")
        return FullOutcome("done", None, attempt_id, str(report_path))
    from engine import release as release_mod  # worker C's module; lazy import

    # engine.release.prepare's own submit() requires an ACTIVE attempt --
    # `attempt_id` (the last task/fix attempt) already closed by the time
    # this runs (its own "close" stage already ran). Open a fresh, roleless
    # attempt purely to carry this one publish request -- "a final attempt
    # bound to the final commit": its base_head is
    # this exact repo HEAD (every task's commit plus the handoff commit),
    # and it is left OPEN (never closed here) while the job waits for the
    # owner's approval -- continue_full_job's own attempts_for_job_name
    # lookup resumes on exactly this attempt.
    release_attempt_id = open_job(config, ledger, project, job_name, "full", ())
    request_id, _summary = release_mod.prepare(config, ledger, project, release_attempt_id)
    _record_progress(ledger, project, release_attempt_id, job_name,
                     stage=STAGE_RELEASE_APPROVAL, stop_reason="release_approval",
                     resume_at=STAGE_RELEASE_APPROVAL, request_id=request_id,
                     report_path=str(report_path), max_fix_rounds=max_fix_rounds)
    return FullOutcome("needs_owner", "release_approval", release_attempt_id, None)


def _record_context(ledger: Ledger, project: str, attempt_id: str | None, station: str,
                    sc: "context_mod.StationContext") -> None:
    """Appends the context.injected ledger event for this station's
    prompt (and, if the library lookup failed, context.failed right
    after it) -- see engine.context.build's own docstring for what each
    field means. `attempt_id` may be None (a build/fix task's attempt
    is only opened AFTER its prompt text is built -- see
    engine.full_orbit's own module docstring on fresh-attempt-per-task)."""
    ledger.append(Event(
        project=project, kind="context.injected", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={
            "station": station,
            "items": [
                {"kind": i.kind, "title": i.title, "source": i.source, "trust": i.trust}
                for i in sc.injected
            ],
            "skills": list(sc.skills), "missing": list(sc.missing),
            "base": list(sc.base), "planned": list(sc.planned),
        },
    ))
    if sc.failed:
        ledger.append(Event(
            project=project, kind="context.failed", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id, payload={"station": station},
        ))


def _planned_skills(config: EngineConfig, plan: Plan, station: str) -> tuple[str, ...]:
    """The skills this job's planner picked for `station` that the engine
    can actually load (needs_install ones are never injected)."""
    usable, _needs_install = classify_skill_tools(plan, config)
    return tuple(tools_for_station(plan, station, usable_skills=usable)["skill"])


def _build_station_context(config: EngineConfig, ledger: Ledger, project: str,
                           attempt_id: str | None, station: str, query: str,
                           planned_skills: tuple[str, ...] = ()) -> "context_mod.StationContext":
    """engine.context.build for this station's prompt, recorded on the
    ledger (context.injected/context.failed) exactly once -- the
    shared first half of _prompt_with_context, broken out so a caller
    that also needs to check `.unmet_required()` (ContextConfig.
    required_skills/require_library -- see that module's own
    docstring) BEFORE ever submitting run_station can do so without
    building/recording the context a second time."""
    sc = context_mod.build(config, project, station, query, planned_skills)
    _record_context(ledger, project, attempt_id, station, sc)
    return sc


def _prompt_with_context(config: EngineConfig, ledger: Ledger, project: str,
                         attempt_id: str | None, station: str, query: str,
                         prompt: str, planned_skills: tuple[str, ...] = ()) -> str:
    """Builds this station's injected-context appendix (engine.context.
    build), records it on the ledger, and returns `prompt` with that
    appendix appended (unchanged if there is nothing to inject). Used
    by every call site that never needs to stop on `.unmet_required()`
    itself (review's own context, which never gates a needs_owner stop
    -- a review failure is an ordinary rejection, not an owner-request)."""
    sc = _build_station_context(config, ledger, project, attempt_id, station, query,
                                planned_skills)
    if not sc.text:
        return prompt
    return f"{prompt}\n\n{sc.text}"


def _unmet_context_reason(sc: "context_mod.StationContext") -> str:
    """A short, stable reason code for a `.unmet_required()` stop --
    read back by nothing (this job always needs the SAME config fix
    either way), but kept informative for `gb2 status`/`gb2 board`."""
    if sc.required_missing:
        return f"context_required_missing:{','.join(sc.required_missing)}"[:200]
    return "context_required_library_failed"


def _stop_early_station_for_unmet_context(ledger: Ledger, project: str, attempt_id: str,
                                          job_name: str, station: str, request: str,
                                          max_fix_rounds: int,
                                          sc: "context_mod.StationContext",
                                          probe_text: str = "") -> FullOutcome:
    """A facts/probe/plan/design station's own required-context gap --
    stopped needs_owner on the SAME shared early attempt, in the exact
    STAGE_EARLY_STATION shape _record_early_station_stop already uses
    for a run_station owner-request, but WITHOUT ever calling
    run_station at all: there is nothing a worker could do about a
    missing skill/company-library lookup the engine itself could not
    load. `pending_requests=[]` (nothing to approve) means
    continue_full_job's own STAGE_EARLY_STATION dispatch reruns this
    station fresh once `gb2 continue` is called -- the owner fixing the
    underlying config/skill root between stops is what actually
    changes anything, not an approval."""
    reason = _unmet_context_reason(sc)
    _record_progress(ledger, project, attempt_id, job_name,
                     stage=STAGE_EARLY_STATION, resume_at=STAGE_EARLY_STATION,
                     station=station, stop_reason=reason, request=request,
                     max_fix_rounds=max_fix_rounds, probe_text=probe_text,
                     pending_requests=[])
    return FullOutcome("needs_owner", reason, attempt_id, None)


def _stop_build_task_for_unmet_context(config: EngineConfig, ledger: Ledger, project: str,
                                       job_name: str, task: Task, task_text: str,
                                       base_task_text: str, sc: "context_mod.StationContext",
                                       *, fixing: bool, remaining_task_ids: tuple[str, ...],
                                       max_fix_rounds: int) -> FullOutcome:
    """A build task's own required-context gap -- opens this task's own
    fresh attempt (the same roles/cursor _run_task_attempt would, so a
    later resume sees exactly the same "task" cursor shape) but never
    submits run_station at all, going straight to a STAGE_TASK_OWNER
    needs_owner stop -- the same shape a run_station owner-request stop
    already uses, so `gb2 continue` reruns this task's build fresh
    (never a second, wasted worker call for a gap the engine itself,
    not the worker, must resolve)."""
    attempt_id = open_job(config, ledger, project, job_name, "full", (BUILDER_ROLE,))
    record_cursor(ledger, project, job_name, stage="task", next_action="run_station",
                 attempt_id=attempt_id, task_id=task.id, task_text=task_text,
                 base_task_text=base_task_text, fixing=fixing,
                 remaining_task_ids=list(remaining_task_ids), max_fix_rounds=max_fix_rounds)
    reason = _unmet_context_reason(sc)
    _record_progress(
        ledger, project, attempt_id, job_name,
        stage=STAGE_TASK_OWNER, resume_at=STAGE_TASK_OWNER,
        remaining_task_ids=list(remaining_task_ids), current_task_id=task.id,
        current_task_text=task_text, current_base_task_text=base_task_text,
        stop_reason=reason, fixing=fixing, max_fix_rounds=max_fix_rounds,
        pending_requests=[],
        # Read back by _resume_task_owner: a context gap is NEITHER a
        # worker_stuck/crash_resume rerun-from-scratch NOR an owner-
        # request fulfillment (pending_requests is deliberately empty
        # here -- there is nothing to approve) -- without this marker,
        # _resume_task_owner's own "else" branch (install fulfilled,
        # append the install-followup note, rerun) silently launches
        # the builder on the SAME possibly-still-missing context,
        # never rebuilding or rechecking unmet_required() at all
        # (codex-diff-review-s18.md's own finding).
        context_gap=True,
    )
    return FullOutcome("needs_owner", reason, attempt_id, None)



def _fakes_for(fakes: "Fakes | Callable[[str], Fakes]", attempt_id: str) -> Fakes:
    """`fakes` is either a ready Fakes or a `fakes_for(attempt_id)` factory (the real
    installer's marker directory depends on the attempt, which is only known once
    each attempt is opened)."""
    if isinstance(fakes, Fakes):
        return fakes
    return fakes(attempt_id)

def _run_and_merge(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                   attempt_id: str, station: str, role: str, task_text: str,
                   on_progress: ProgressCallback | None,
                   processor_factory: orbit.ProcessorFactory | None, *,
                   extra_payload: dict[str, object] | None = None) -> orbit.JobOutcome:
    """run_station(station, role, task_text) -> merge(role's own copy).
    Never commits/report/close -- the caller does, once it knows this
    station's own gate passed (see _commit_job_trail). `extra_payload`
    (the design call site's own plan.tools table, see
    engine.plan.tools_for_station) is merged into the run_station
    payload on top of the fixed station/role/task keys. `task_text`
    already differs on a STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW resume
    (the caller appends the previous attempt's own log/plan.json as
    context -- see _run_probe/_run_plan_and_review), so run_station's
    own deterministic request_id is never the problem; the merge
    stage's payload, on its own, names only role/station/path (fixed
    for this attempt), so a SECOND merge of the same role here would
    otherwise collide with the first merge's own already-"done" request
    and never actually reapply anything (engine.processor.submit's
    idempotency) -- `merge_retry_n` (how many merges this attempt has
    already recorded) makes each retry's merge payload genuinely new."""
    payload: dict[str, object] = {"station": station, "role": role, "task": task_text}
    if extra_payload:
        payload.update(extra_payload)
    copy = str(copy_root(config, attempt_id, role))
    merge_retry_n = sum(1 for _seq, event in ledger.events(project, "merge.recorded")
                        if event.attempt_id == attempt_id)
    merge_payload: dict[str, object] = {
        "copies": [{"role": role, "station": station, "path": copy}],
    }
    if merge_retry_n:
        merge_payload["merge_retry_n"] = merge_retry_n
    stages = [
        orbit.Stage("run_station", payload),
        orbit.Stage("merge", merge_payload),
    ]
    return orbit.continue_job(config, ledger, _fakes_for(fakes, attempt_id), project, attempt_id, stages,
                              processor_factory=processor_factory, on_progress=on_progress)


def _normalized_manifest_rel(raw: object) -> str:
    import posixpath
    return posixpath.normpath(str(raw))


def _install_request_fulfilled(ledger: Ledger, project: str, request: dict[str, object]) -> bool:
    """True iff some "install.recorded" event for `project` already
    applied EXACTLY this manifest+change -- engine.steps.install's own
    request payload shape, shared by all three kinds engine.owner.
    PENDING_KIND_WHITELIST allows a station to ask the owner for
    (install/scripts_change/test_config_change). Matched by manifest+
    change only, never also by kind: every install.recorded event this
    project has ever appended (across every kind and every one of this
    project's jobs/attempts alike) shares the same payload shape, and
    tests/fixtures across this codebase that inject one directly
    predate "kind" being a field worth comparing at all -- narrowing
    the match to also require kind would silently stop treating those
    as fulfilled.

    Matched by CONTENT, never by request id: the worker's raw proposal
    (a station.finished "requests" entry) was never itself submitted as
    a ledger request, so there is nothing else to match it against.
    `manifest` is compared posix-normalized the same way
    engine.steps.install._normalized_manifest_rel compares it, so
    "./package.json" and "package.json" are recognized as the same
    file."""
    manifest = _normalized_manifest_rel(request.get("manifest"))
    change = request.get("change")
    for _seq, event in ledger.events(project, "install.recorded"):
        if (_normalized_manifest_rel(event.payload.get("manifest")) == manifest
                and event.payload.get("change") == change):
            return True
    return False


def _pending_requests_fulfilled(ledger: Ledger, project: str,
                                pending: list[object]) -> bool:
    """True iff EVERY request a station once asked the owner for has
    since been fulfilled. Any kind in engine.owner.PENDING_KIND_WHITELIST
    (install/scripts_change/test_config_change -- the kinds a station's
    own owner-request can actually be approved under) can be verified;
    an empty list (no request was ever recorded, e.g. a progress event
    from before this field existed) is vacuously fulfilled, and any
    OTHER/unknown kind fails closed -- never silently treated as done
    just because this module does not know how to check it."""
    for req in pending:
        if not isinstance(req, dict) or req.get("kind") not in PENDING_KIND_WHITELIST:
            return False
        if not _install_request_fulfilled(ledger, project, req):
            return False
    return True


def _station_outcome_to_full(outcome: orbit.JobOutcome, station: str) -> FullOutcome | None:
    """None iff `outcome` is "done" (the caller should keep going).
    Otherwise the terminal/needs_owner FullOutcome to return right
    now."""
    if outcome.status == "done":
        return None
    if outcome.pending_requests or outcome.status == "needs_owner":
        return FullOutcome("needs_owner", "owner_request", outcome.attempt_id, None)
    return FullOutcome("failed", f"{station}:{outcome.reason}", outcome.attempt_id, None)


def _close_attempt(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                   attempt_id: str, job_name: str, on_progress: ProgressCallback | None,
                   processor_factory: orbit.ProcessorFactory | None) -> orbit.JobOutcome:
    """report -> close for an attempt that made no gated commit of its
    own (the facts/planner/designer attempt -- every file it touched
    was already committed directly via _commit_job_trail, so close's
    own archive/reset has nothing left to archive or reset)."""
    return orbit.continue_job(config, ledger, _fakes_for(fakes, attempt_id), project, attempt_id,
                              orbit.release_followup_stages(job_name),
                              processor_factory=processor_factory, on_progress=on_progress)


def _task_build_owns(task: Task, plan: Plan, job_name: str) -> list[str]:
    """This task's own write prefixes (task.owns) plus, only when
    plan.ui, the engine's own extra screens/ allowance (see
    _config_for_build_task) -- never part of the plan's own owns, so
    the planner never has to know or ask for it. Shared by the
    run_station payload (_builder_run_station_payload) and the merge
    payload (_run_task_attempt/the STAGE_TASK_OWNER worker_stuck
    resume) so merge's own re-check (engine.steps.merge._owner_prefixes)
    is narrowed to exactly the same set run_station's own gate already
    enforced while the worker was running -- never the broader,
    unnarrowed station prefix (a per-task merge used to
    accept a write anywhere under the WHOLE build station's own
    prefixes, not just this task's own owns)."""
    owns = list(task.owns)
    if plan.ui:
        owns.append(f".gongbang/{job_name}/screens/")
    return owns


def _builder_run_station_payload(task: Task, task_text: str, plan: Plan,
                                 config: EngineConfig, job_name: str) -> dict[str, object]:
    usable_skills, _needs_install = classify_skill_tools(plan, config)
    return {"station": "build", "role": BUILDER_ROLE, "task": task_text,
            "owns": _task_build_owns(task, plan, job_name),
            "tools": tools_for_station(plan, "build", usable_skills=usable_skills)}


def _review_payload(config: EngineConfig, ledger: Ledger, project: str, plan: Plan,
                    task: str) -> dict[str, object]:
    """The review stage judges against the task itself (without the
    build station's injected skills) and gets its own station context:
    the review base skills plus the skills the planner picked for
    review. Also carries the planner's own `tools_for_station(plan,
    "review")` table -- without it, engine.steps.review's own mcp
    narrowing (_payload_mcp_names) always saw an empty "tools" key,
    so neither the codex reviewer's own "reviewer_mcp_unsupported"
    guard (which only fires when the payload actually asks for mcp)
    nor a claude reviewer's actual mcp access ever reflected what the
    plan asked for."""
    review_context = _prompt_with_context(config, ledger, project, None, "review",
                                          task, "", _planned_skills(config, plan, "review"))
    usable_skills, _needs_install = classify_skill_tools(plan, config)
    payload: dict[str, object] = {
        "role": REVIEWER_ROLE, "task": task,
        "tools": tools_for_station(plan, "review", usable_skills=usable_skills),
    }
    if review_context.strip():
        payload["context"] = review_context.strip()
    return payload


def _replace_review_stage(stages: list[orbit.Stage], config: EngineConfig, ledger: Ledger,
                          project: str, plan: Plan, task: str) -> None:
    """Replaces the one "review" stage in `stages` (orbit.
    stages_after_install's own plain {"role", "task"} payload, built
    with no awareness of the plan at all) with full_orbit's own
    _review_payload -- the plan's injected context AND its planned mcp
    tools (an owner-resume review, e.g. after an install
    approval, used to run with neither, unlike every OTHER review this
    module submits)."""
    for index, stage in enumerate(stages):
        if stage.kind == "review":
            stages[index] = orbit.Stage(
                "review", _review_payload(config, ledger, project, plan, task))
            return


def _install_followup_task_text(task_text: str, build_copy: Path) -> str:
    """`task_text` (the builder's own task, with its skill-context
    appendix already attached) plus a note that the install it once
    asked for has since completed -- never start over, pick up from
    where it stopped. `build_copy`'s own dirty_paths (the copy's real
    git status, never the worker's self-report) lists what it had
    already changed before it asked for the install, same evidence
    engine.orbit._continuation_payload shows a stalled worker."""
    changed = dirty_paths(build_copy, ())
    listed = "\n".join(f"- {p}" for p in changed) or "- (아직 없음)"
    note = (
        "\n\n[의존성 설치 완료] 의존성이 설치됐다. 처음부터 다시 하지 말고, 멈춘 곳부터 "
        "이어서 끝내라. 이미 바뀐 아래 파일을 먼저 읽는다.\n"
        f"{listed}"
    )
    return f"{task_text}{note}"


def _rerun_build_task_stages(ledger: Ledger, project: str, run_config: EngineConfig,
                             attempt_id: str, task_obj: Task, plan: Plan, job_name: str,
                             run_station_task_text: str, review_task_text: str) -> list[orbit.Stage]:
    """run_station(build, task_obj, run_station_task_text) -> merge
    (narrowed to task_obj's own owns, see _task_build_owns) -> engine.
    orbit.stages_after_install's own review/bench/commit/report/close
    tail -- shared by both STAGE_TASK_OWNER resume paths: a stalled
    builder rerun from scratch (continue_full_job's "worker_stuck"
    branch), and a builder whose asked-for install has since completed
    (the "else" branch, `run_station_task_text` built by
    _install_followup_task_text).

    The merge stage's own payload carries `merge_retry_n` (how many
    merge.recorded events this attempt already has) whenever this is
    NOT this attempt's first build merge -- same anti-collision field
    _run_and_merge already adds for a probe/plan retry on a shared
    attempt. Without it, a SECOND merge of the SAME role/station/path/
    owns on the SAME attempt_id submits with the exact same payload as
    the FIRST one did, so engine.processor.submit's own idempotent
    request_id (a hash of project+attempt_id+kind+payload) collides
    with that already-"done" first merge and this second one is never
    actually run at all -- the builder's own second, finishing edit
    (run_station's own task text WAS made different by the
    continuation note, so IT reruns for real) would then silently
    never land."""
    build_config = _config_for_build_task(run_config, job_name, plan)
    build_copy = str(copy_root(build_config, attempt_id, BUILDER_ROLE))
    task_owns = _task_build_owns(task_obj, plan, job_name)
    merge_retry_n = sum(1 for _seq, event in ledger.events(project, "merge.recorded")
                        if event.attempt_id == attempt_id)
    merge_payload: dict[str, object] = {"copies": [
        {"role": BUILDER_ROLE, "station": "build", "path": build_copy, "owns": task_owns},
    ]}
    if merge_retry_n:
        merge_payload["merge_retry_n"] = merge_retry_n
    return [
        orbit.Stage("run_station", _builder_run_station_payload(
            task_obj, run_station_task_text, plan, build_config, job_name)),
        orbit.Stage("merge", merge_payload),
        *orbit.stages_after_install(build_config, attempt_id, BUILDER_ROLE,
                                    REVIEWER_ROLE, job_name, review_task_text),
    ]


def _run_task_attempt(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, task: Task, task_text: str, plan: Plan,
                      on_progress: ProgressCallback | None,
                      processor_factory: orbit.ProcessorFactory | None,
                      base_task_text: str | None = None, *,
                      fixing: bool = False, remaining_task_ids: tuple[str, ...] = (),
                      max_fix_rounds: int = 2) -> orbit.JobOutcome:
    """Open a FRESH attempt for this one task (see module docstring for
    why) and run its full run_station(build) -> merge -> review ->
    bench -> commit -> report -> close sequence -- exactly
    engine.orbit.default_stages()'s own seven stages, just built
    directly here so the run_station payload can carry this task's own
    `owns`, which default_stages() has no parameter for.

    `fixing`/`remaining_task_ids`/`max_fix_rounds` carry nothing this
    function itself needs -- they are recorded on the "task" cursor
    (record_cursor) so a later crash-resume (continue_full_job's own
    cursor fallback, see _resume_from_cursor) knows what this task IS
    (an acceptance-fix round or a plain plan.tasks entry) and what is
    still queued behind it, without having to recompute either from
    scratch."""
    attempt_id = open_job(config, ledger, project, job_name, "full", (BUILDER_ROLE,))
    record_cursor(ledger, project, job_name, stage="task", next_action="run_station",
                  attempt_id=attempt_id, task_id=task.id, task_text=task_text,
                  base_task_text=base_task_text or task_text, fixing=fixing,
                  remaining_task_ids=list(remaining_task_ids), max_fix_rounds=max_fix_rounds)
    build_copy = str(copy_root(config, attempt_id, BUILDER_ROLE))
    # Never the project's own `release` for a per-task attempt's commit
    # stage -- see _config_without_release's own docstring.
    build_config = _config_without_release(
        _config_for_build_task(config, job_name, plan), project)
    task_owns = _task_build_owns(task, plan, job_name)
    stages = [
        orbit.Stage("run_station", _builder_run_station_payload(task, task_text, plan,
                                                                 build_config, job_name)),
        orbit.Stage("merge", {"copies": [
            {"role": BUILDER_ROLE, "station": "build", "path": build_copy, "owns": task_owns},
        ]}),
        orbit.Stage("review", _review_payload(config, ledger, project, plan,
                                              base_task_text or task_text)),
        orbit.Stage("bench", {}),
        orbit.Stage("commit", {"message": f"gongbang: {job_name} - {task.id}"}),
        orbit.Stage("report", {"summary": f"gongbang: {job_name} - {task.id}"}),
        orbit.Stage("close", {}),
    ]
    return orbit.continue_job(build_config, ledger, _fakes_for(fakes, attempt_id), project, attempt_id,
                              stages, processor_factory=processor_factory, on_progress=on_progress)


def _run_tasks(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
              job_name: str, plan: Plan, tasks: list[Task],
              on_progress: ProgressCallback | None,
              processor_factory: orbit.ProcessorFactory | None,
              last_attempt_id: str | None = None, max_fix_rounds: int = 2,
              ) -> tuple[FullOutcome | None, str | None]:
    """Run every task in `tasks` (already in dependency order), each in
    its own fresh attempt, to completion. (None, last_attempt_id) iff
    all of them finished "done" -- otherwise (the needs_owner/failed
    FullOutcome to return right now, its own attempt_id), having
    already journaled enough progress (remaining task ids) for
    continue_full_job to pick up at the task that stopped.

    `last_attempt_id`, when given, is the attempt that already covers
    whatever `tasks` was narrowed down FROM (e.g. a STAGE_TASK_OWNER
    resume's own already-finished attempt, once the task that stopped
    is dropped off the front of `remaining_task_ids`) -- if `tasks` is
    now empty (that dropped task was the LAST one in the plan), the
    loop below never runs, and this value is returned UNCHANGED rather
    than silently falling back to None: the caller still needs a real
    attempt_id to pass acceptance (a None attempt_id used to crash
    engine.acceptance.run_acceptance on exactly this resume path)."""
    for index, task in enumerate(tasks):
        base_task_text = builder_task_text(task, plan.goal, job_name, plan.ui)
        remaining_now = tuple(t.id for t in tasks[index:])
        build_sc = _build_station_context(config, ledger, project, None, "build",
                                          base_task_text, _planned_skills(config, plan, "build"))
        if build_sc.unmet_required():
            return _stop_build_task_for_unmet_context(
                config, ledger, project, job_name, task, base_task_text, base_task_text,
                build_sc, fixing=False, remaining_task_ids=remaining_now,
                max_fix_rounds=max_fix_rounds,
            ), last_attempt_id
        task_text = (f"{base_task_text}\n\n{build_sc.text}" if build_sc.text else base_task_text)
        outcome = _run_task_attempt(config, ledger, fakes, project, job_name, task,
                                    task_text, plan, on_progress, processor_factory,
                                    base_task_text, remaining_task_ids=remaining_now,
                                    max_fix_rounds=max_fix_rounds)
        last_attempt_id = outcome.attempt_id
        if outcome.status != "done" or outcome.pending_requests:
            full = _station_outcome_to_full(outcome, "build")
            if outcome.status == "needs_owner" or outcome.pending_requests:
                remaining = [t.id for t in tasks[index:]]
                _record_progress(
                    ledger, project, outcome.attempt_id, job_name,
                    stage=STAGE_TASK_OWNER, resume_at=STAGE_TASK_OWNER,
                    remaining_task_ids=remaining,
                    current_task_id=task.id, current_task_text=task_text,
                    current_base_task_text=base_task_text, stop_reason=outcome.reason,
                    # The station's own raw proposals (e.g. {"kind":
                    # "install", ...}) -- lost before this fix, since
                    # outcome.reason is None for a pending-requests stop
                    # and nothing else here carried them. continue_full_job
                    # verifies these are fulfilled before resuming (see
                    # _pending_requests_fulfilled) rather than blindly
                    # jumping to review/bench.
                    pending_requests=list(outcome.pending_requests),
                )
            if full is not None:
                return full, last_attempt_id
    return None, last_attempt_id


def _run_acceptance_and_handoff(config: EngineConfig, ledger: Ledger, fakes: Fakes,
                                project: str, job_name: str, plan: Plan,
                                max_fix_rounds: int, on_progress: ProgressCallback | None,
                                processor_factory: orbit.ProcessorFactory | None,
                                last_attempt_id: str | None) -> FullOutcome:
    from engine import acceptance as acceptance_mod  # worker C's module; lazy import
    from engine import handoff as handoff_mod  # worker C's module; lazy import

    record_cursor(ledger, project, job_name, stage="post_tasks", next_action="acceptance",
                  attempt_id=last_attempt_id, max_fix_rounds=max_fix_rounds)
    acc = acceptance_mod.run_acceptance(config, ledger, project, last_attempt_id, plan)
    rounds = 0
    while not acc.passed and rounds < max_fix_rounds:
        failing = tuple(s.id for s in acc.scenes if not s.passed)
        all_owns = tuple(sorted({entry for t in plan.tasks for entry in t.owns}))
        fix_task = Task(id=f"fix-{rounds + 1}", title="수락 실패 수정", detail="",
                        owns=all_owns, depends_on=())
        # getattr with a default: these two AcceptanceResult fields (an earlier round
        # contract item 2) may not exist yet in this worktree's own
        # engine.acceptance -- fix_task_text falls back to its own
        # "no failing scene" wording either way.
        base_task_text = fix_task_text(
            fix_task, plan.goal, failing, job_name, plan.ui,
            full_suite_returncode=getattr(acc, "full_suite_returncode", None),
            full_suite_tail=getattr(acc, "full_suite_tail", ""),
        )
        fix_sc = _build_station_context(config, ledger, project, None, "build", base_task_text,
                                        _planned_skills(config, plan, "build"))
        if fix_sc.unmet_required():
            return _stop_build_task_for_unmet_context(
                config, ledger, project, job_name, fix_task, base_task_text, base_task_text,
                fix_sc, fixing=True, remaining_task_ids=(), max_fix_rounds=max_fix_rounds,
            )
        task_text = (f"{base_task_text}\n\n{fix_sc.text}" if fix_sc.text else base_task_text)
        outcome = _run_task_attempt(config, ledger, fakes, project, job_name, fix_task,
                                    task_text, plan, on_progress, processor_factory,
                                    base_task_text, fixing=True,
                                    max_fix_rounds=max_fix_rounds)
        last_attempt_id = outcome.attempt_id
        if outcome.status != "done" or outcome.pending_requests:
            full = _station_outcome_to_full(outcome, "build")
            if outcome.status == "needs_owner" or outcome.pending_requests:
                _record_progress(
                    ledger, project, outcome.attempt_id, job_name,
                    stage=STAGE_TASK_OWNER, resume_at=STAGE_TASK_OWNER, remaining_task_ids=[],
                    current_task_id=fix_task.id, current_task_text=task_text,
                    current_base_task_text=base_task_text, stop_reason=outcome.reason,
                    fix_round=rounds, fixing=True, max_fix_rounds=max_fix_rounds,
                    pending_requests=list(outcome.pending_requests),
                )
            if full is not None:
                return full
        rounds += 1
        record_cursor(ledger, project, job_name, stage="post_tasks", next_action="acceptance",
                      attempt_id=last_attempt_id, max_fix_rounds=max_fix_rounds)
        acc = acceptance_mod.run_acceptance(config, ledger, project, last_attempt_id, plan)

    if not acc.passed:
        return FullOutcome("failed", "max_fix_rounds_exceeded", last_attempt_id, None)

    uncovered = _uncovered_requirement_ids(plan, acc)
    if uncovered:
        reason = f"requirements_uncovered:{','.join(uncovered)}"
        _record_progress(ledger, project, last_attempt_id, job_name,
                         stage=STAGE_REQUIREMENTS_REVIEW, resume_at=STAGE_REQUIREMENTS_REVIEW,
                         stop_reason=reason, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, last_attempt_id, None)

    record_cursor(ledger, project, job_name, stage="post_tasks", next_action="handoff",
                  attempt_id=last_attempt_id, max_fix_rounds=max_fix_rounds)
    report_path = handoff_mod.write_handoff(config, ledger, project, last_attempt_id,
                                            job_name, plan, acc)
    _commit_job_trail(config, ledger, project, last_attempt_id, job_name,
                      f"gongbang: {job_name} - handoff")
    return _finish_after_handoff(config, ledger, project, job_name, plan, last_attempt_id,
                                 report_path, max_fix_rounds)


def _finish_after_handoff(config: EngineConfig, ledger: Ledger, project: str, job_name: str,
                          plan: Plan, attempt_id: str, report_path: Path,
                          max_fix_rounds: int) -> FullOutcome:
    """Everything after the handoff report is written and committed:
    the app-wide security check  and, only once that passes, the
    job's single release request (_maybe_release_or_done)."""
    from engine import app_security  # worker B's module; lazy import

    record_cursor(ledger, project, job_name, stage="post_tasks", next_action="security",
                  attempt_id=attempt_id, max_fix_rounds=max_fix_rounds)
    security = app_security.check(config, project, attempt_id, plan)
    # The result (findings carry kind:path:line only, never values) is
    # recorded for the release gate and for the person fixing it.
    app_security.record(ledger, project, attempt_id, security)
    if not security.passed:
        findings = "; ".join(list(security.findings)[:5])
        reason = f"security_failed:{findings}"[:300] if findings else "security_failed"
        # The repo's own content fingerprint (engine.evidence.
        # fingerprint, a tree hash covering the working tree -- tracked
        # AND dirty paths alike, same value engine.acceptance's own
        # before/after check uses) acceptance/handoff actually
        # certified -- read back on resume (STAGE_SECURITY_FIX) to tell
        # a plain security retry from one where the owner's own fix has
        # since changed the source, which must rerun acceptance/
        # coverage/handoff against the new source before security runs
        # again, not just security itself. A plain `git rev-parse HEAD`
        # (this field's own earlier implementation) missed exactly the
        # case this exists for: an owner fix that EDITS the source
        # WITHOUT committing (`gb2 fix` always commits on a fresh
        # attempt and so moves HEAD, but a direct owner edit to the
        # working tree does not) left HEAD unchanged, so that comparison
        # silently skipped rerunning acceptance/coverage/handoff and let
        # a build-only job report "done" against source acceptance never
        # actually certified.
        from engine import evidence  # worker C's module; lazy import

        excludes = config.projects[project].excludes
        certified_fingerprint = evidence.fingerprint(config.projects[project].repo, excludes)
        _record_progress(ledger, project, attempt_id, job_name,
                         stage=STAGE_SECURITY_FIX, resume_at=STAGE_SECURITY_FIX,
                         stop_reason=reason, report_path=str(report_path),
                         max_fix_rounds=max_fix_rounds,
                         certified_fingerprint=certified_fingerprint)
        return FullOutcome("needs_owner", reason, attempt_id, None)
    return _maybe_release_or_done(config, ledger, project, job_name, attempt_id,
                                  report_path, max_fix_rounds)


def _finish_from_tasks(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                       job_name: str, plan: Plan, tasks: list[Task], max_fix_rounds: int,
                       on_progress: ProgressCallback | None,
                       processor_factory: orbit.ProcessorFactory | None,
                       last_attempt_id: str | None = None) -> FullOutcome:
    stopped, last_attempt_id = _run_tasks(config, ledger, fakes, project, job_name, plan,
                                         tasks, on_progress, processor_factory,
                                         last_attempt_id, max_fix_rounds)
    if stopped is not None:
        return stopped
    return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name, plan,
                                       max_fix_rounds, on_progress, processor_factory,
                                       last_attempt_id)


def _append_owner_request_resume_note(config: EngineConfig, attempt_id: str, role: str,
                                      task_text: str, needs_resume_note: bool) -> str:
    """`task_text` unchanged for a fresh run OR a "worker_stuck" resume
    (submit()'s own with_retry already busts a FAILED request's
    payload automatically, so an unchanged prompt still reruns for
    real -- and orbit never records "worker_stuck" WITH any pending
    requests at all, so `needs_resume_note` is always False there) --
    but with a continuation note appended for an owner-request resume
    (`needs_resume_note` True: this STAGE_EARLY_STATION stop had a
    non-empty, now-fulfilled `pending_requests`): that station's own
    run_station request ended "done", so resubmitting this SAME,
    unchanged prompt would just reconcile onto that already-"done"
    request -- and its already-recorded pending install -- forever,
    rather than ever actually giving the station a chance to finish
    (the exact infinite-stall bug a test caught). Reuses the same note
    engine.full_orbit's own build-task resume gives a builder after an
    install (_install_followup_task_text)."""
    if not needs_resume_note:
        return task_text
    return _install_followup_task_text(task_text, copy_root(config, attempt_id, role))


def _run_probe(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
              job_name: str, attempt_id: str, request: str,
              on_progress: ProgressCallback | None,
              processor_factory: orbit.ProcessorFactory | None,
              max_fix_rounds: int, needs_resume_note: bool = False,
              ) -> tuple[FullOutcome | None, str]:
    """Runs the probe station from scratch (a fresh run_station call --
    used for a job's very first probe attempt, a STAGE_PROBE_RETRY
    resume, AND a STAGE_EARLY_STATION owner-request resume -- see
    _probe_plan_design_close/_append_owner_request_resume_note).
    Returns (None, probe_text) to keep going, or (the needs_owner/
    failed FullOutcome to return right now, "")."""
    record_cursor(ledger, project, job_name, stage="probe", next_action="run_station",
                  attempt_id=attempt_id, request=request, max_fix_rounds=max_fix_rounds)
    repo = config.projects[project].repo
    try:
        targets = check_probe_targets(repo, job_name)
    except ProbeTargetsError as exc:
        # A malformed 외부 대상 line is never silently skipped: a person
        # fixes facts.md, then continue reruns this station.
        reason = f"probe_targets_invalid:{exc}"[:200]
        _record_progress(ledger, project, attempt_id, job_name,
                         stage=STAGE_PROBE_RETRY, resume_at=STAGE_PROBE_RETRY,
                         stop_reason=reason, request=request, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, attempt_id, None), ""
    if not targets:
        ledger.append(Event(
            project=project, kind="probe.skipped", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id, payload={"job": job_name},
        ))
        return None, ""
    base_probe_text = prober_prompt(request, job_name, targets)
    # A STAGE_PROBE_RETRY resume reruns this on the SAME (never-closed)
    # attempt -- engine.ids/submit's own request_id is deterministic from
    # this payload, so an unchanged prompt would collide with the FIRST
    # run's already-"done" request and never actually run again (the
    # RunStationStub-shaped bug this fixed). The previous attempt's own
    # already-committed logs (if any -- "" on a job's first probe call)
    # make the retry's payload genuinely different, and give the worker
    # real context on what failed last time.
    previous_logs = _probe_logs_text(repo, job_name, targets)
    probe_prompt_text = (f"{base_probe_text}\n\n이전 시도 로그(다시 시도한다):\n{previous_logs}"
                         if previous_logs else base_probe_text)
    probe_sc = _build_station_context(config, ledger, project, attempt_id, "probe", request)
    if probe_sc.unmet_required():
        return _stop_early_station_for_unmet_context(
            ledger, project, attempt_id, job_name, "probe", request, max_fix_rounds, probe_sc,
        ), ""
    probe_task_text = (f"{probe_prompt_text}\n\n{probe_sc.text}" if probe_sc.text
                       else probe_prompt_text)
    probe_task_text = _append_owner_request_resume_note(
        config, attempt_id, PROBER_ROLE, probe_task_text, needs_resume_note)
    outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "probe",
                             PROBER_ROLE, probe_task_text, on_progress,
                             processor_factory)
    full = _station_outcome_to_full(outcome, "probe")
    if full is not None:
        _record_early_station_stop(ledger, project, attempt_id, job_name, "probe", outcome,
                                   request=request, max_fix_rounds=max_fix_rounds)
        return full, ""
    _commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - probe")
    # Pass/fail comes from the engine's own run of each probe script
    # (probe.executed: script, return code, output); the worker's log
    # text ("PROBE OK") is supporting context only.
    from engine import acceptance as acceptance_mod

    evidence = acceptance_mod.run_probe_scripts(config, ledger, project, attempt_id,
                                                job_name, targets)
    failed = [e.name for e in evidence if not e.ok]
    if failed:
        failed_names = ",".join(failed)
        reason = f"probe_failed:{failed_names}"
        _record_progress(ledger, project, attempt_id, job_name,
                         stage=STAGE_PROBE_RETRY, resume_at=STAGE_PROBE_RETRY,
                         stop_reason=reason, request=request, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, attempt_id, None), ""
    return None, _probe_logs_text(repo, job_name, targets)


def _run_plan_and_review(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                         job_name: str, attempt_id: str, request: str, probe_text: str,
                         on_progress: ProgressCallback | None,
                         processor_factory: orbit.ProcessorFactory | None,
                         max_fix_rounds: int, needs_resume_note: bool = False,
                         ) -> tuple[FullOutcome | None, Plan | None]:
    """Runs the plan station, then reviews that same change, from
    scratch -- used for a job's first plan attempt, a STAGE_PLAN_REVIEW
    resume, AND a STAGE_EARLY_STATION owner-request resume. Returns
    (None, plan) to keep going, or (the needs_owner/failed FullOutcome
    to return right now, None)."""
    record_cursor(ledger, project, job_name, stage="plan", next_action="run_station",
                  attempt_id=attempt_id, request=request, probe_text=probe_text,
                  max_fix_rounds=max_fix_rounds)
    repo = config.projects[project].repo
    facts_md = _facts_md_text(repo, job_name)
    base_plan_prompt = planner_prompt(request, job_name, facts_md, skill_catalog_text(config),
                                      probe_text)
    # A STAGE_PLAN_REVIEW resume reruns this on the SAME (never-closed)
    # attempt -- same idempotent-request_id reasoning as _run_probe's own
    # retry note: an unchanged prompt would collide with the first
    # attempt's own already-"done" run_station request. The previous
    # (review-rejected, never committed) plan.json, still sitting in the
    # working tree from that first attempt's own merge, makes the retry's
    # payload different and shows the worker what it wrote last time.
    try:
        previous_plan_json = plan_path(repo, job_name).read_text(encoding="utf-8")
    except OSError:
        previous_plan_json = ""
    plan_prompt_text = (
        f"{base_plan_prompt}\n\n이전 시도의 plan.json(검토를 통과하지 못했다. 고쳐 다시 "
        f"쓴다):\n{previous_plan_json}" if previous_plan_json else base_plan_prompt
    )
    plan_sc = _build_station_context(config, ledger, project, attempt_id, "plan", request)
    if plan_sc.unmet_required():
        return _stop_early_station_for_unmet_context(
            ledger, project, attempt_id, job_name, "plan", request, max_fix_rounds, plan_sc,
            probe_text=probe_text,
        ), None
    plan_task_text = (f"{plan_prompt_text}\n\n{plan_sc.text}" if plan_sc.text
                      else plan_prompt_text)
    plan_task_text = _append_owner_request_resume_note(
        config, attempt_id, PLANNER_ROLE, plan_task_text, needs_resume_note)
    outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "plan",
                             PLANNER_ROLE, plan_task_text,
                             on_progress, processor_factory)
    full = _station_outcome_to_full(outcome, "plan")
    if full is not None:
        _record_early_station_stop(ledger, project, attempt_id, job_name, "plan", outcome,
                                   request=request, max_fix_rounds=max_fix_rounds,
                                   probe_text=probe_text)
        return full, None
    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None), None
    problems = validate_plan(plan, repo, _build_prefixes(config))
    problems += validate_tools(plan, config)
    if problems:
        return FullOutcome("failed", "plan:" + "; ".join(problems), attempt_id, None), None

    # Review the planner's own merged-but-uncommitted change (plan.json +
    # tests/scenes/) BEFORE it is committed as the orchestrator's own plan
    # trail -- otherwise it becomes every later reviewer's base_head
    # (nothing to diff against) and is never judged by anyone (Codex
    # finding). One plain review Stage on this same attempt; the diff it
    # sees is exactly base_head..current (this merge's own files, nothing
    # else has touched the tree yet).
    plan_review_outcome = orbit.continue_job(
        config, ledger, _fakes_for(fakes, attempt_id), project, attempt_id,
        [orbit.Stage("review", _review_payload(config, ledger, project, plan, request))],
        processor_factory=processor_factory, on_progress=on_progress,
    )
    if plan_review_outcome.status != "done":
        _record_progress(ledger, project, attempt_id, job_name,
                         stage=STAGE_PLAN_REVIEW, resume_at=STAGE_PLAN_REVIEW,
                         stop_reason="plan_review_failed", request=request,
                         probe_text=probe_text, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", "plan_review_failed", attempt_id, None), None

    _commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - plan", station="plan", role=PLANNER_ROLE)
    _record_plan_reviewed(ledger, project, attempt_id, job_name, repo)
    return None, plan


def _record_plan_reviewed(ledger: Ledger, project: str, attempt_id: str,
                          job_name: str, repo: Path) -> None:
    """The one durable record of which plan.json + scene files a
    reviewer last approved (engine.plan.record_plan_reviewed), read
    back by _maybe_rerun_plan_review on a later resume."""
    from engine import plan as plan_mod

    plan_mod.record_plan_reviewed(ledger, project, attempt_id, repo, job_name)


def _maybe_rerun_plan_review(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                             job_name: str, attempt_id: str, plan: Plan,
                             on_progress: ProgressCallback | None,
                             processor_factory: orbit.ProcessorFactory | None,
                             ) -> FullOutcome | None:
    """None to keep going -- either no "plan.reviewed" event was ever
    recorded for this job, or plan.json's current digest still matches the one
    last reviewed. Otherwise the needs_owner FullOutcome to return
    right now: the owner edited plan.json (e.g. fixing a scene the
    planner got wrong) between a needs_owner stop and this `gb2
    continue` -- that edit was never looked at by a reviewer, so this
    reruns the SAME review stage _run_plan_and_review's own first pass
    already runs, on `attempt_id` (still the shared early attempt --
    only ever called from the STAGE_DESIGN_APPROVAL resume, before any
    task attempt opens), and records a fresh "plan.reviewed" once it
    passes."""
    from engine.plan import plan_digest

    repo = config.projects[project].repo
    current_digest = plan_digest(plan_path(repo, job_name))
    reviewed_digest: object = None
    for _seq, event in ledger.events(project, "plan.reviewed"):
        if event.payload.get("job") == job_name:
            reviewed_digest = event.payload.get("digest")
    if reviewed_digest is None or reviewed_digest == current_digest:
        return None
    plan_review_outcome = orbit.continue_job(
        config, ledger, _fakes_for(fakes, attempt_id), project, attempt_id,
        [orbit.Stage("review", _review_payload(config, ledger, project, plan, plan.goal))],
        processor_factory=processor_factory, on_progress=on_progress,
    )
    if plan_review_outcome.status != "done":
        return FullOutcome("needs_owner", "plan_review_failed", attempt_id, None)
    # Commit the re-reviewed plan.json/scene bytes to the job trail
    # BEFORE recording plan.reviewed -- same ordering _run_plan_and_
    # review's own first pass uses (commit, then record). Without
    # this, the owner's edit that triggered this re-review was never
    # committed anywhere: it only exists as an uncommitted change in
    # the real repo, which engine.orbit's own "close" stage (git
    # checkout/clean to HEAD) wipes right back out the moment this
    # attempt closes -- so a later build/merge/commit would run
    # against the OLD, pre-edit plan.json even though plan.reviewed
    # now names the new digest as approved.
    _commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - plan", station="plan", role=PLANNER_ROLE)
    _record_plan_reviewed(ledger, project, attempt_id, job_name, repo)
    return None


def _probe_plan_design_close(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                             job_name: str, attempt_id: str, request: str,
                             max_fix_rounds: int, on_progress: ProgressCallback | None,
                             processor_factory: orbit.ProcessorFactory | None, *,
                             start: str = "probe", probe_text: str = "",
                             needs_resume_note: bool = False) -> FullOutcome:
    """probe -> plan+review -> (design, if plan.ui) -> close -> build
    loop. `start` is "probe" (a fresh job, or a STAGE_PROBE_RETRY
    resume), "plan" (a STAGE_PLAN_REVIEW resume -- probe already
    succeeded and stays as-is, `probe_text` is its already-committed
    logs, read by the resume caller), or "design" (a STAGE_EARLY_
    STATION resume for station=="design" -- plan.json is already
    committed by then, so the plan is reloaded from disk rather than
    rerunning the planner at all; `probe_text` is unused). `needs_
    resume_note`, only ever True for a STAGE_EARLY_STATION owner-
    request resume, is given ONLY to whichever station `start` names --
    the one THIS resume is actually for -- never to a later station in
    the same call, which runs fresh regardless."""
    if start == "probe":
        full, probe_text = _run_probe(config, ledger, fakes, project, job_name, attempt_id,
                                      request, on_progress, processor_factory, max_fix_rounds,
                                      needs_resume_note=needs_resume_note)
        if full is not None:
            return full

    if start == "design":
        repo = config.projects[project].repo
        try:
            plan = load_plan(repo, job_name)
        except PlanError as exc:
            return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None)
    else:
        full, plan = _run_plan_and_review(
            config, ledger, fakes, project, job_name, attempt_id, request, probe_text,
            on_progress, processor_factory, max_fix_rounds,
            needs_resume_note=(needs_resume_note if start == "plan" else False))
        if full is not None:
            return full
        assert plan is not None

    if plan.ui:
        record_cursor(ledger, project, job_name, stage="design", next_action="run_station",
                      attempt_id=attempt_id, request=request, max_fix_rounds=max_fix_rounds)
        usable_skills, _needs_install = classify_skill_tools(plan, config)
        design_prompt_text = designer_prompt(request, job_name, plan.goal)
        design_sc = _build_station_context(config, ledger, project, attempt_id, "design",
                                           request, _planned_skills(config, plan, "design"))
        if design_sc.unmet_required():
            return _stop_early_station_for_unmet_context(
                ledger, project, attempt_id, job_name, "design", request, max_fix_rounds,
                design_sc,
            )
        design_task_text = (f"{design_prompt_text}\n\n{design_sc.text}" if design_sc.text
                            else design_prompt_text)
        design_task_text = _append_owner_request_resume_note(
            config, attempt_id, DESIGNER_ROLE, design_task_text,
            needs_resume_note if start == "design" else False)
        outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "design",
                                 DESIGNER_ROLE, design_task_text,
                                 on_progress, processor_factory,
                                 extra_payload={"tools": tools_for_station(
                                     plan, "design", usable_skills=usable_skills)})
        full = _station_outcome_to_full(outcome, "design")
        if full is not None:
            _record_early_station_stop(ledger, project, attempt_id, job_name, "design", outcome,
                                       request=request, max_fix_rounds=max_fix_rounds)
            return full
        _commit_job_trail(config, ledger, project, attempt_id, job_name,
                          f"gongbang: {job_name} - design")
        if not check_design_approved(ledger, project, job_name):
            _record_progress(ledger, project, attempt_id, job_name,
                             stage=STAGE_DESIGN_APPROVAL, resume_at=STAGE_DESIGN_APPROVAL,
                             stop_reason="design_approval", max_fix_rounds=max_fix_rounds)
            return FullOutcome("needs_owner", "design_approval", attempt_id, None)

    close_outcome = _close_attempt(config, ledger, fakes, project, attempt_id, job_name,
                                   on_progress, processor_factory)
    if close_outcome.status != "done":
        return FullOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None)

    return _finish_from_tasks(config, ledger, fakes, project, job_name, plan,
                              ordered_tasks(plan), max_fix_rounds, on_progress,
                              processor_factory)


def _run_facts(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
              job_name: str, attempt_id: str, request: str,
              on_progress: ProgressCallback | None,
              processor_factory: orbit.ProcessorFactory | None,
              max_fix_rounds: int, needs_resume_note: bool = False,
              ) -> FullOutcome | None:
    """Runs the facts station from scratch -- used both for a job's
    very first call (run_full_job) and a STAGE_EARLY_STATION resume.
    Returns None to keep going (facts merged and committed -- check_
    facts' own content validation is the caller's job, same as
    before), or the needs_owner/failed FullOutcome to return right
    now."""
    record_cursor(ledger, project, job_name, stage="facts", next_action="run_station",
                  attempt_id=attempt_id, request=request, max_fix_rounds=max_fix_rounds)
    facts_sc = _build_station_context(config, ledger, project, attempt_id, "facts", request)
    if facts_sc.unmet_required():
        return _stop_early_station_for_unmet_context(
            ledger, project, attempt_id, job_name, "facts", request, max_fix_rounds, facts_sc)
    base_facts_prompt = facts_prompt(request, job_name)
    facts_task_text = (f"{base_facts_prompt}\n\n{facts_sc.text}" if facts_sc.text
                       else base_facts_prompt)
    facts_task_text = _append_owner_request_resume_note(
        config, attempt_id, FACTS_ROLE, facts_task_text, needs_resume_note)
    outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "facts",
                             FACTS_ROLE, facts_task_text, on_progress,
                             processor_factory)
    full = _station_outcome_to_full(outcome, "facts")
    if full is not None:
        _record_early_station_stop(ledger, project, attempt_id, job_name, "facts", outcome,
                                   request=request, max_fix_rounds=max_fix_rounds)
        return full
    _commit_job_trail(config, ledger, project, attempt_id, job_name, f"gongbang: {job_name} - facts")
    return None


def run_full_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                 job_name: str, request: str, *, max_fix_rounds: int = 2,
                 on_progress: ProgressCallback | None = None,
                 processor_factory: orbit.ProcessorFactory | None = None) -> FullOutcome:
    repo = config.projects[project].repo
    attempt_id = open_job(config, ledger, project, job_name, "full",
                          (FACTS_ROLE, PROBER_ROLE, PLANNER_ROLE, DESIGNER_ROLE))

    full = _run_facts(config, ledger, fakes, project, job_name, attempt_id, request,
                      on_progress, processor_factory, max_fix_rounds)
    if full is not None:
        return full
    problems = check_facts(repo, job_name)
    if problems:
        return FullOutcome("failed", "facts:" + "; ".join(problems), attempt_id, None)

    return _probe_plan_design_close(config, ledger, fakes, project, job_name, attempt_id,
                                    request, max_fix_rounds, on_progress, processor_factory,
                                    start="probe")


def _task_attempt_committed(ledger: Ledger, project: str, attempt_id: str) -> bool:
    """True iff `attempt_id`'s own "commit" request reached "done" --
    the one durable signal that a build task actually finished. Needed
    because attempt.status alone cannot tell a GENUINELY finished task
    apart from one fixup.close_failed_attempt closed for a repair (the
    SAME "closed" status either way): a task whose review/bench failed
    never reaches the commit stage at all before fixup closes it to
    archive the work and open a replacement attempt."""
    for _seq, event in ledger.events(project, "request.submitted"):
        if event.attempt_id != attempt_id or event.payload.get("kind") != "commit":
            continue
        request_id = event.payload.get("request_id")
        if isinstance(request_id, str) and ledger.request(request_id).status == "done":
            return True
    return False


def _rerun_closed_task_from_scratch(config: EngineConfig, ledger: Ledger, fakes: Fakes,
                                    project: str, job_name: str, plan: Plan,
                                    cursor: dict[str, object], max_fix_rounds: int,
                                    on_progress: ProgressCallback | None,
                                    processor_factory: orbit.ProcessorFactory | None,
                                    ) -> FullOutcome:
    """A "task" cursor's own attempt is closed/abandoned WITHOUT ever
    committing (_task_attempt_committed is False) -- it never actually
    finished (an abandoned attempt, or fixup.close_failed_attempt
    closing a failed task's attempt for a repair that then crashed
    before its replacement ever opened). Reruns this exact task from
    scratch on a BRAND NEW attempt (never resubmits onto the closed
    one) -- the same run_station -> ... -> close sequence a fresh task
    build always runs, sharing _run_tasks' own loop-body handling of
    whatever outcome that produces (needs_owner records a fresh
    STAGE_TASK_OWNER progress on the new attempt; done continues into
    whatever plan.tasks or the acceptance-fix loop is still queued
    behind it)."""
    task_id = str(cursor.get("task_id", ""))
    fixing = bool(cursor.get("fixing"))
    if fixing:
        all_owns = tuple(sorted({entry for t in plan.tasks for entry in t.owns}))
        task_obj: Task | None = Task(id=task_id, title="수락 실패 수정", detail="",
                                     owns=all_owns, depends_on=())
    else:
        by_id = {t.id: t for t in plan.tasks}
        task_obj = by_id.get(task_id)
        if task_obj is None:
            raise FullOrbitError(f"unknown task id for resume: {task_id!r}")
    task_text = str(cursor.get("task_text", ""))
    base_task_text = str(cursor.get("base_task_text", task_text))
    remaining_ids = [str(i) for i in cursor.get("remaining_task_ids", [])]
    outcome = _run_task_attempt(config, ledger, fakes, project, job_name, task_obj, task_text,
                                plan, on_progress, processor_factory, base_task_text,
                                fixing=fixing, remaining_task_ids=tuple(remaining_ids),
                                max_fix_rounds=max_fix_rounds)
    new_attempt_id = outcome.attempt_id
    if outcome.status != "done" or outcome.pending_requests:
        full = _station_outcome_to_full(outcome, "build")
        if outcome.status == "needs_owner" or outcome.pending_requests:
            _record_progress(
                ledger, project, new_attempt_id, job_name,
                stage=STAGE_TASK_OWNER, resume_at=STAGE_TASK_OWNER,
                remaining_task_ids=remaining_ids, current_task_id=task_id,
                current_task_text=task_text, current_base_task_text=base_task_text,
                stop_reason=outcome.reason, fixing=fixing, max_fix_rounds=max_fix_rounds,
                pending_requests=list(outcome.pending_requests),
            )
        if full is not None:
            return full
    if fixing:
        return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name, plan,
                                           max_fix_rounds, on_progress, processor_factory,
                                           new_attempt_id)
    by_id = {t.id: t for t in plan.tasks}
    remaining_tasks = [by_id[i] for i in remaining_ids[1:] if i in by_id]
    return _finish_from_tasks(config, ledger, fakes, project, job_name, plan, remaining_tasks,
                              max_fix_rounds, on_progress, processor_factory, new_attempt_id)


def _abandon_orphaned_requests(ledger: Ledger, project: str, attempt_id: str) -> None:
    """Reconciles every non-terminal request left over for `attempt_id`
    before this stale attempt is discarded (see _reopen_for_retry's own
    docstring) -- a genuine crash can leave one "queued" (submitted but
    never dispatched) or "running" (Processor.run_once's own
    `_set_status(..., "running", {})` runs BEFORE the step that
    actually does the work, so a process death mid-dispatch never gets
    a chance to set a terminal status at all).

    "queued" is always safe to cancel outright (engine.processor.
    cancel_request -- it never dispatched, so there is no external
    effect to reconcile). "running" is NOT: a request only reaches
    "running" once some processor's run_once claimed it, and that
    processor may still be alive and mid-step right now -- marking it
    "failed" out from under a LIVE processor would let two processors
    act on the same request's external effect at once. This function
    therefore only ever judges a "running" request orphaned while it
    itself holds this project's processor lock (the same lock run_once/
    run_request take) -- proof no other processor can be running
    anything for this project right this moment. If the lock is already
    held by another live processor (acquire_lock returns False), every
    "running" request is left untouched -- the caller, _reopen_for_
    retry, has nothing useful to do with that either way: trying again
    later is always safe, since this function runs again on the next
    attempt-retry."""
    from engine.processor import cancel_request

    holder = f"full-orbit-orphan-{attempt_id}"
    have_lock = ledger.acquire_lock(project, holder)
    try:
        for _seq, event in ledger.events(project, "request.submitted"):
            if event.attempt_id != attempt_id:
                continue
            request_id = event.payload.get("request_id")
            if not isinstance(request_id, str):
                continue
            request = ledger.request(request_id)
            if request.status == "queued":
                cancel_request(ledger, project, request_id, "crash_resume_abandoned")
            elif request.status == "running" and have_lock:
                ledger.append(Event(
                    project=project, kind="request.status", actor_kind="processor",
                    actor_id="processor", attempt_id=attempt_id,
                    payload={"request_id": request_id, "status": "failed",
                             "result": {"reason": "crash_resume_abandoned"}},
                ))
    finally:
        if have_lock:
            ledger.release_lock(project, holder)


# Marker files close's own archive_tmp writes alongside the archived
# paths themselves (never part of the real diff) -- same constant
# engine.fixup._ARCHIVE_MARKER_FILES defines, duplicated here rather
# than imported: this module does not depend on fixup.py's internals,
# same reasoning fixup.py's own module docstring gives for duplicating
# engine.steps.open_job's equivalent helper instead of reusing it.
_ARCHIVE_MARKER_FILES = {".deleted", ".modes"}


def _archive_dir_for_attempt(ledger: Ledger, project: str, attempt_id: str) -> Path | None:
    """The archive directory close already created for this attempt (its
    "archive.done" event), or None if it was never closed yet. Mirrors
    engine.fixup._archive_dir_for_attempt."""
    path: Path | None = None
    for _seq, event in ledger.events(project, "archive.done"):
        if event.attempt_id == attempt_id:
            path = Path(str(event.payload["path"]))
    return path


def _archive_has_content(archive_dir: Path) -> bool:
    """True iff `archive_dir` actually holds a change -- mirrors
    engine.fixup._archive_has_content (see that function's own
    docstring for why an archive that exists but is empty, e.g. an
    attempt that never merged anything into the real repo, must never
    be mistaken for "owner work to restore")."""
    if not archive_dir.exists():
        return False
    deleted_file = archive_dir / ".deleted"
    if deleted_file.exists() and deleted_file.read_text(encoding="utf-8").strip():
        return True
    for path in archive_dir.rglob("*"):
        if path.is_file() and str(path.relative_to(archive_dir)) not in _ARCHIVE_MARKER_FILES:
            return True
    return False


def _overlay_archive_into_copy(config: EngineConfig, attempt_id: str, role: str,
                               archive_dir: Path) -> None:
    """Copies every archived file (an owner's own uncommitted edit to a
    facts/probe/plan/design station's files, made while the job sat
    needs_owner at STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW/STAGE_EARLY_
    STATION/a plain crash) into the FRESH attempt's own `role` copy, as
    plain filesystem writes -- mirrors engine.fixup.
    _overlay_archive_into_builder_copy exactly (see that function's own
    docstring), generalized to any role's copy, not just the builder's.
    Without this, _reopen_for_retry's own close (archive, THEN reset_to_
    head) preserved the owner's edit only in cold storage -- the real
    repo was wiped back to HEAD and the new attempt's copy was archived
    fresh from that same HEAD, so the edit was never actually seen
    again by anything that runs after this."""
    import shutil

    dest_root = copy_root(config, attempt_id, role)

    modes: dict[str, object] = {}
    modes_file = archive_dir / ".modes"
    if modes_file.exists():
        try:
            modes = json.loads(modes_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            modes = {}

    deleted: list[str] = []
    deleted_file = archive_dir / ".deleted"
    if deleted_file.exists():
        deleted = [
            line for line in deleted_file.read_text(encoding="utf-8").splitlines() if line
        ]

    for src in sorted(archive_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(archive_dir)
        if str(rel) in _ARCHIVE_MARKER_FILES:
            continue
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if modes.get(str(rel)) == "100755":
            dest.chmod(dest.stat().st_mode | 0o111)

    for rel in deleted:
        target = dest_root / rel
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)


_RETRY_STAGE_ROLE = {
    "facts": FACTS_ROLE, "probe": PROBER_ROLE, "plan": PLANNER_ROLE, "design": DESIGNER_ROLE,
}


def _reopen_for_retry(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, stale_attempt_id: str,
                      on_progress: ProgressCallback | None,
                      processor_factory: orbit.ProcessorFactory | None,
                      *, retry_stage: str | None = None) -> tuple[FullOutcome | None, str]:
    """Closes `stale_attempt_id` (report -> close, same as the design-
    approval resume already does on this shared facts/probe/plan/design
    attempt) and opens a brand-new one for the SAME job. Needed before
    a STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW resume reruns a station whose
    own merge already landed and was committed once on `stale_attempt_id`
    -- engine.steps.merge's own base_head check (_verify_destinations_
    unchanged) refuses a SECOND merge of the same path on the SAME
    attempt, because the repo's HEAD (carrying that first, already-
    committed probe/plan trail) no longer matches that attempt's fixed
    base_head: "someone else's committed change landed there since this
    copy's base" -- true here even though the "someone else" is this
    very job's own earlier station. A fresh attempt's own base_head is
    the CURRENT repo HEAD (already including that earlier commit), so
    the retry's own new merge sees no conflict. Returns (None, the new
    attempt_id) to keep going, or (the failed FullOutcome to return
    right now, "").

    `retry_stage`, when given ("facts"/"probe"/"plan"/"design"), is
    this retry's OWN station -- close's own archive_tmp step already
    copied out every path THAT station's merge had landed uncommitted
    in the real repo (including, e.g., a probe log an owner hand-edited
    while the job sat STAGE_PROBE_RETRY needs_owner) before reset_to_
    head wiped the real repo back to HEAD; _overlay_archive_into_copy
    restores that same content into the fresh attempt's own role copy
    (_RETRY_STAGE_ROLE), so the owner's edit is still there for the
    retry to see. Without this, close's own archive preserved it only
    in cold storage -- the owner's edit never actually reached the new
    attempt at all."""
    # A genuine crash (as opposed to the two pre-existing callers' own
    # terminal "failed"/"done" stop) can leave ONE request for
    # `stale_attempt_id` still "running" -- engine.processor.Processor.
    # run_once treats "running" exactly like "queued" (something still
    # to dispatch), so _close_attempt's own report/close requests,
    # submitted right below, would have run_once sweep that orphaned
    # request up and actually run it FIRST -- silently reconciling (and
    # wasting) a worker call for an attempt this resume is about to
    # discard entirely. A no-op for the two pre-existing callers (their
    # stale attempt's own request is already terminal by the time they
    # call this).
    _abandon_orphaned_requests(ledger, project, stale_attempt_id)
    close_outcome = _close_attempt(config, ledger, fakes, project, stale_attempt_id, job_name,
                                   on_progress, processor_factory)
    if close_outcome.status != "done":
        return FullOutcome("failed", f"close:{close_outcome.reason}", stale_attempt_id, None), ""
    # Read only AFTER close has run -- close's own archive_tmp/archive_
    # commit steps are what actually create the "archive.done" event
    # this looks up; nothing exists to find before that.
    archive_dir = (_archive_dir_for_attempt(ledger, project, stale_attempt_id)
                   if retry_stage is not None else None)
    new_attempt_id = open_job(config, ledger, project, job_name, "full",
                              (FACTS_ROLE, PROBER_ROLE, PLANNER_ROLE, DESIGNER_ROLE))
    if (retry_stage is not None and archive_dir is not None
            and _archive_has_content(archive_dir)):
        _overlay_archive_into_copy(config, new_attempt_id, _RETRY_STAGE_ROLE[retry_stage],
                                   archive_dir)
    return None, new_attempt_id


def _resume_from_cursor(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                        job_name: str, cursor: dict[str, object],
                        on_progress: ProgressCallback | None,
                        processor_factory: orbit.ProcessorFactory | None) -> FullOutcome:
    """continue_full_job's fallback once there is no `full_orbit.progress`
    needs_owner stop at all for this job -- an ordinary "failed"
    FullOutcome (plan validation, max_fix_rounds_exceeded, a close
    failure, ...) or a genuine crash (the process died before anything
    recorded a stop), both of which leave `cursor` (record_cursor,
    written before every stage/task transition -- see its own
    docstring) as the only record of where this job actually was.

    "facts"/"probe"/"plan"/"design" -- NEVER trusted to still have a
    reusable copy for every role _run_facts/_probe_plan_design_close
    might need: a cursor at one of these stages can belong to a SMALL
    job (engine.small_orbit), whose attempt was opened with only the
    PLANNER_ROLE copy (its one combined facts+plan call) -- resuming
    "facts" there against the recorded attempt_id would reach for a
    FACTS_ROLE copy that was simply never made (open_job's own
    _make_copies only ever makes copies for the roles it was opened
    with), failing closed at the first dirty_paths/git-identity check
    rather than ever running. This is also engine.small_orbit's own
    documented recovery story (see that module's docstring): a crashed
    small job resumes as an ordinary FULL job from the facts station
    onward, never by retrying the combined small-mode call again. So
    `_reopen_for_retry` (close the stale attempt -- a trivial no-op
    reset if nothing was ever merged into the real repo yet, same as
    every other case that function already handles -- then open a
    FRESH attempt with every early-station role) runs first, exactly
    the same as continue_full_job's own STAGE_PROBE_RETRY/STAGE_PLAN_
    REVIEW dispatch already does; only then do _run_facts/
    _probe_plan_design_close's own idempotent run_station/merge
    submissions take over (a GENUINE full-job crash reconciles whatever
    already finished on that fresh attempt's first submission, same
    idempotent-request-id story as an owner-request STAGE_EARLY_
    STATION resume already relies on -- the fresh attempt's base_head
    is simply the same repo HEAD the stale one already had, nothing
    is lost).
    "task" -- same idempotent-rerun story, via _resume_task_owner with
    a synthesized stop_reason="crash_resume" (see that function's own
    docstring for why that is equivalent to "worker_stuck" here).
    "post_tasks" -- acceptance -> handoff -> security -> release, all
    of which _run_acceptance_and_handoff already reruns safely from
    scratch on every STAGE_REQUIREMENTS_REVIEW/STAGE_SECURITY_FIX
    resume today."""
    stage = cursor.get("stage")
    attempt_id = str(cursor.get("attempt_id") or "")
    max_fix_rounds = int(cursor.get("max_fix_rounds", 2))

    if stage in ("facts", "probe", "plan", "design"):
        request = str(cursor.get("request", ""))
        probe_text = str(cursor.get("probe_text", ""))
        repo = config.projects[project].repo
        failed, fresh_attempt_id = _reopen_for_retry(config, ledger, fakes, project, job_name,
                                                      attempt_id, on_progress, processor_factory,
                                                      retry_stage=str(stage))
        if failed is not None:
            return failed
        attempt_id = fresh_attempt_id
        if stage == "facts":
            full = _run_facts(config, ledger, fakes, project, job_name, attempt_id, request,
                              on_progress, processor_factory, max_fix_rounds)
            if full is not None:
                return full
            problems = check_facts(repo, job_name)
            if problems:
                return FullOutcome("failed", "facts:" + "; ".join(problems), attempt_id, None)
            return _probe_plan_design_close(config, ledger, fakes, project, job_name, attempt_id,
                                            request, max_fix_rounds, on_progress, processor_factory,
                                            start="probe")
        return _probe_plan_design_close(config, ledger, fakes, project, job_name, attempt_id,
                                        request, max_fix_rounds, on_progress, processor_factory,
                                        start=stage, probe_text=probe_text)

    if stage == "small_plan":
        # engine.small_orbit's own combined facts+plan call (one station
        # covering what _run_facts/_run_plan_and_review split into two
        # here) -- never load_plan below: a crash/failed-review resume
        # at this stage can land before plan.json was ever committed
        # (or validated) at all, which load_plan would raise on.
        from engine import small_orbit

        return small_orbit.resume_small_plan(
            config, ledger, fakes, project, job_name, cursor,
            on_progress=on_progress, processor_factory=processor_factory)

    repo = config.projects[project].repo
    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None)
    stop = _stop_for_resumed_plan_problems(ledger, project, job_name, attempt_id, repo)
    if stop is not None:
        return stop

    if stage == "task":
        if attempt_id and ledger.attempt(attempt_id).status in ("closed", "abandoned"):
            # This cursor's own attempt is no longer active -- but
            # "closed" alone does NOT mean the task it names finished:
            # fixup.close_failed_attempt closes a FAILED task's attempt
            # (to archive its work before opening a replacement) with
            # the exact same status a genuinely successful close leaves
            # behind. Resubmitting onto this attempt would raise
            # 'attempt_not_active' outright either way, so the real
            # question is whether to advance past this task (it truly
            # finished) or rerun it from scratch (it did not --
            # including the crash-mid-repair case: fixup closed the old
            # attempt, then crashed before ever opening its
            # replacement, so there is nothing else left to resume).
            if _task_attempt_committed(ledger, project, attempt_id):
                # Genuinely done -- advance the way resume_full_job_
                # after_task_fix does once its own fixed task's outcome
                # is "done": drop it off the front of remaining_task_ids
                # and continue with whatever plan.tasks (or the
                # acceptance-fix loop) are still queued behind it.
                if cursor.get("fixing"):
                    return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name,
                                                       plan, max_fix_rounds, on_progress,
                                                       processor_factory, attempt_id)
                remaining_ids = [str(i) for i in cursor.get("remaining_task_ids", [])]
                by_id = {t.id: t for t in plan.tasks}
                remaining_tasks = [by_id[i] for i in remaining_ids[1:] if i in by_id]
                return _finish_from_tasks(config, ledger, fakes, project, job_name, plan,
                                          remaining_tasks, max_fix_rounds, on_progress,
                                          processor_factory, attempt_id)
            return _rerun_closed_task_from_scratch(config, ledger, fakes, project, job_name,
                                                   plan, cursor, max_fix_rounds, on_progress,
                                                   processor_factory)
        synthetic_progress: dict[str, object] = {
            "current_task_id": cursor.get("task_id"),
            "current_task_text": cursor.get("task_text"),
            "current_base_task_text": cursor.get("base_task_text"),
            "remaining_task_ids": cursor.get("remaining_task_ids", []),
            "fixing": cursor.get("fixing", False),
            "stop_reason": "crash_resume",
            "pending_requests": [],
        }
        return _resume_task_owner(config, ledger, fakes, project, job_name, plan, attempt_id,
                                  synthetic_progress, max_fix_rounds, on_progress,
                                  processor_factory)

    if stage == "post_tasks":
        return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name, plan,
                                           max_fix_rounds, on_progress, processor_factory,
                                           attempt_id)

    raise FullOrbitError(f"unknown cursor stage: {stage!r}")


def resume_full_job_after_task_fix(config: EngineConfig, ledger: Ledger, fakes: Fakes,
                                   project: str, job_name: str, outcome: orbit.JobOutcome,
                                   new_attempt_id: str,
                                   on_progress: ProgressCallback | None = None,
                                   processor_factory: orbit.ProcessorFactory | None = None,
                                   ) -> FullOutcome | None:
    """None iff `job_name` was never run through the full orbit at all
    (no "task" cursor ever recorded for it -- an ordinary single-task
    `gb2 run`/`gb2 fix` job, whose caller's own existing behavior is
    unchanged). Otherwise the ONE place `gb2 fix` hands a full job's
    just-fixed task back to the full-job coordinator: `engine.fixup.
    fix()` only ever repairs the one task its own find_fix_target
    selected (closing the old failed attempt, opening a fresh one,
    restoring the archived work, rerunning run_station -> merge ->
    review -> bench -> commit -> report -> close for THAT task alone)
    -- it has no notion of "the rest of this job's own plan.tasks".
    Once that fixed task's own `outcome` is "done", this reads the
    LATEST "task" cursor `_run_tasks`/`_run_acceptance_and_handoff`
    recorded for it (remaining_task_ids, fixing, max_fix_rounds -- the
    same fields a STAGE_TASK_OWNER resume would use) and continues
    exactly where the full job itself would have: either back into the
    acceptance-fix loop (`fixing`) or into whatever plan.tasks are
    still queued behind the one just fixed, followed by acceptance,
    the requirement-coverage check, the handoff report, the app-wide
    security check, and (only once, at the very end -- never for this
    one task) the job's single release request. If `outcome` itself is
    not "done" (the fix produced ANOTHER failure/needs_owner, e.g. the
    builder asked for another install), that is returned as-is -- but
    ONLY after this records a fresh "task" cursor and (for needs_owner)
    a STAGE_TASK_OWNER progress pointed at `new_attempt_id` -- the
    REPLACEMENT attempt fixup.fix() just opened. Without this, the
    job's own cursor/progress stayed pointed at the OLD attempt fixup
    already closed (fixup.close_failed_attempt): a later `gb2 continue`
    would try to resume building on an attempt engine.processor.submit
    now refuses outright ('attempt_not_active'), the exact failure mode
    this records before ever returning."""
    cursor = _load_cursor(ledger, project, job_name)
    if cursor is None or cursor.get("stage") != "task":
        return None
    if outcome.status != "done":
        if outcome.pending_requests or outcome.status == "needs_owner":
            remaining_task_ids = [str(i) for i in cursor.get("remaining_task_ids", [])]
            task_text = str(cursor.get("task_text", ""))
            base_task_text = str(cursor.get("base_task_text", task_text))
            fixing = bool(cursor.get("fixing"))
            max_fix_rounds = int(cursor.get("max_fix_rounds", 2))
            record_cursor(ledger, project, job_name, stage="task", next_action="run_station",
                         attempt_id=new_attempt_id, task_id=cursor.get("task_id"),
                         task_text=task_text, base_task_text=base_task_text, fixing=fixing,
                         remaining_task_ids=remaining_task_ids, max_fix_rounds=max_fix_rounds)
            _record_progress(
                ledger, project, new_attempt_id, job_name,
                stage=STAGE_TASK_OWNER, resume_at=STAGE_TASK_OWNER,
                remaining_task_ids=remaining_task_ids, current_task_id=cursor.get("task_id"),
                current_task_text=task_text, current_base_task_text=base_task_text,
                stop_reason=outcome.reason, fixing=fixing, max_fix_rounds=max_fix_rounds,
                pending_requests=list(outcome.pending_requests),
            )
        return FullOutcome(outcome.status, outcome.reason, new_attempt_id, None)
    repo = config.projects[project].repo
    stop = _stop_for_resumed_plan_problems(ledger, project, job_name, new_attempt_id, repo)
    if stop is not None:
        return stop
    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", new_attempt_id, None)
    max_fix_rounds = int(cursor.get("max_fix_rounds", 2))
    if cursor.get("fixing"):
        return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name, plan,
                                           max_fix_rounds, on_progress, processor_factory,
                                           new_attempt_id)
    remaining_ids = [str(i) for i in cursor.get("remaining_task_ids", [])]
    by_id = {t.id: t for t in plan.tasks}
    remaining_tasks = [by_id[i] for i in remaining_ids[1:] if i in by_id]
    return _finish_from_tasks(config, ledger, fakes, project, job_name, plan, remaining_tasks,
                              max_fix_rounds, on_progress, processor_factory, new_attempt_id)


def _check_resumed_plan(ledger: Ledger, project: str, job_name: str, repo: Path) -> list[str]:
    """engine.plan.check_resumed_plan(ledger, project, job_name, repo),
    with "plan_never_reviewed" filtered out -- that code can be true
    for a job whose plan was reviewed before the "plan.reviewed" ledger
    event existed at all (an older worktree's own job), so on its own
    it is never a reason to stop a resume. Every OTHER code (plan load/
    validate_plan failures, "plan_changed_since_review", "requirement_
    removed_since_review: X", "task_removed_since_review: X") IS.

    Call sites that already run their own re-review before trusting the
    plan (STAGE_DESIGN_APPROVAL's own _maybe_rerun_plan_review) call
    this AFTER that re-review, never before: checking first would
    wrongly stop a resume on "plan_changed_since_review" for an edit
    the re-review is about to re-approve anyway (see continue_full_job's own
    STAGE_DESIGN_APPROVAL branch)."""
    from engine import plan as plan_mod

    return [p for p in plan_mod.check_resumed_plan(ledger, project, job_name, repo)
            if p != "plan_never_reviewed"]


def _stop_for_resumed_plan_problems(ledger: Ledger, project: str, job_name: str,
                                    attempt_id: str, repo: Path) -> FullOutcome | None:
    """None iff _check_resumed_plan finds nothing wrong -- the caller
    keeps going. Otherwise returns the needs_owner FullOutcome to
    return right now, WITHOUT recording any new progress/cursor event
    at all -- a plan.json/scene edit made (or a requirement/task
    silently dropped) while this job sat needs_owner is caught here
    before ANY stage reruns build/merge/commit work against it, but
    whatever progress/cursor ALREADY describes the real stop (e.g. a
    STAGE_TASK_OWNER build-task stop with its own remaining_task_ids,
    or a "task" cursor) must stay the LATEST event for this job/
    attempt. An earlier version of this function overwrote it with a
    generic STAGE_REQUIREMENTS_REVIEW stop instead -- once the plan
    edit was reverted, the next `gb2 continue` saw ONLY that generic
    stop, jumped straight to _run_acceptance_and_handoff, and silently
    skipped the interrupted task and everything still queued behind it
    (codex-diff-review-s18.md's own "Plan guard destroys continuation
    state" finding). Leaving the real progress/cursor untouched means
    the NEXT `gb2 continue`, once the edit is fixed, reloads that SAME
    event and resumes exactly where the job actually was."""
    plan_problems = _check_resumed_plan(ledger, project, job_name, repo)
    if not plan_problems:
        return None
    reason = ("resumed_plan_invalid:" + "; ".join(plan_problems))[:300]
    return FullOutcome("needs_owner", reason, attempt_id, None)


def pending_release_progress(ledger: Ledger, project: str,
                             job_name: str) -> dict[str, object] | None:
    """The STAGE_RELEASE_APPROVAL progress for this job's latest
    attempt, if any -- a thin, PUBLIC read-only wrapper around
    _load_progress (same pattern as pending_owner_requests above), read
    by `gb2 resolve` to find the report_path _maybe_release_or_done
    recorded without reaching into this module's private progress
    format itself."""
    attempts = ledger.attempts_for_job_name(project, job_name)
    if not attempts:
        return None
    attempt_id = attempts[-1].attempt_id
    progress = _load_progress(ledger, project, attempt_id, job_name)
    if progress is None or progress.get("stage") != STAGE_RELEASE_APPROVAL:
        return None
    return progress


def continue_full_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, *, on_progress: ProgressCallback | None = None,
                      processor_factory: orbit.ProcessorFactory | None = None) -> FullOutcome:
    repo = config.projects[project].repo
    attempts = ledger.attempts_for_job_name(project, job_name)
    if not attempts:
        raise FullOrbitError(f"no attempt for job {job_name!r}")
    attempt_id = attempts[-1].attempt_id

    progress = _load_progress(ledger, project, attempt_id, job_name)
    if progress is None:
        # No needs_owner stop was ever journaled for this job's latest
        # attempt -- either it crashed mid-stage (the process died
        # before anything recorded a stop) or it ended at an ordinary
        # "failed" FullOutcome (record_release_decision/_record_progress
        # are never called for those). Fall back to the latest cursor
        # (record_cursor) instead of raising outright -- see
        # _resume_from_cursor's own docstring for exactly what each
        # cursor stage resumes into.
        cursor = _load_cursor(ledger, project, job_name)
        if cursor is None:
            raise FullOrbitError(f"no pending stop recorded for job {job_name!r}")
        return _resume_from_cursor(config, ledger, fakes, project, job_name, cursor,
                                   on_progress, processor_factory)

    max_fix_rounds = int(progress.get("max_fix_rounds", 2))
    stage = progress.get("stage")

    # These two stages stop BEFORE plan.json ever exists (a probe retry)
    # or before it was ever committed (a plan-review retry rerun from
    # scratch) -- load_plan below would raise "plan_missing" for either,
    # so both are dispatched first, never touching `plan` at all.
    if stage == STAGE_PROBE_RETRY:
        request = str(progress.get("request", ""))
        failed, fresh_attempt_id = _reopen_for_retry(config, ledger, fakes, project, job_name,
                                                      attempt_id, on_progress, processor_factory,
                                                      retry_stage="probe")
        if failed is not None:
            return failed
        return _probe_plan_design_close(config, ledger, fakes, project, job_name,
                                        fresh_attempt_id, request, max_fix_rounds, on_progress,
                                        processor_factory, start="probe")

    if stage == STAGE_PLAN_REVIEW:
        request = str(progress.get("request", ""))
        probe_text = str(progress.get("probe_text", ""))
        failed, fresh_attempt_id = _reopen_for_retry(config, ledger, fakes, project, job_name,
                                                      attempt_id, on_progress, processor_factory,
                                                      retry_stage="plan")
        if failed is not None:
            return failed
        return _probe_plan_design_close(config, ledger, fakes, project, job_name,
                                        fresh_attempt_id, request, max_fix_rounds, on_progress,
                                        processor_factory, start="plan", probe_text=probe_text)

    if stage == STAGE_EARLY_STATION:
        # Also dispatched before load_plan: a facts/probe/plan stop can
        # happen before plan.json exists at all (or, for "plan", before
        # it was ever validated) -- only "design" needs it, and
        # _probe_plan_design_close(start="design") loads it itself.
        station = str(progress.get("station", ""))
        request = str(progress.get("request", ""))
        probe_text = str(progress.get("probe_text", ""))
        stop_reason = progress.get("stop_reason")
        stop_reason_str = str(stop_reason) if stop_reason is not None else None
        pending_requests = progress.get("pending_requests") or []
        if (stop_reason_str != "worker_stuck" and isinstance(pending_requests, list)
                and pending_requests and not _pending_requests_fulfilled(
                    ledger, project, pending_requests)):
            # Same fulfillment gate as the build-task STAGE_TASK_OWNER
            # resume -- never silently proceed past an owner request
            # that was never actually granted.
            return FullOutcome("needs_owner", "owner_request", attempt_id, None)
        if station == "facts":
            full = _run_facts(config, ledger, fakes, project, job_name, attempt_id, request,
                              on_progress, processor_factory, max_fix_rounds,
                              needs_resume_note=bool(pending_requests))
            if full is not None:
                return full
            problems = check_facts(repo, job_name)
            if problems:
                return FullOutcome("failed", "facts:" + "; ".join(problems), attempt_id, None)
            return _probe_plan_design_close(config, ledger, fakes, project, job_name, attempt_id,
                                            request, max_fix_rounds, on_progress,
                                            processor_factory, start="probe")
        if station not in ("probe", "plan", "design"):
            raise FullOrbitError(f"unknown early station for resume: {station!r}")
        return _probe_plan_design_close(config, ledger, fakes, project, job_name, attempt_id,
                                        request, max_fix_rounds, on_progress, processor_factory,
                                        start=station, probe_text=probe_text,
                                        needs_resume_note=bool(pending_requests))

    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None)

    if stage != STAGE_DESIGN_APPROVAL:
        # STAGE_DESIGN_APPROVAL checks this itself, AFTER its own
        # _maybe_rerun_plan_review -- see that branch below for why the
        # order matters (re-review already re-approves a plan edit that
        # triggered it; checking BEFORE that re-review would wrongly
        # stop on "plan_changed_since_review" for an edit the re-review
        # is about to re-approve anyway).
        stop = _stop_for_resumed_plan_problems(ledger, project, job_name, attempt_id, repo)
        if stop is not None:
            return stop

    if stage == STAGE_DESIGN_APPROVAL:
        stale = _maybe_rerun_plan_review(config, ledger, fakes, project, job_name, attempt_id,
                                         plan, on_progress, processor_factory)
        if stale is not None:
            return stale
        stop = _stop_for_resumed_plan_problems(ledger, project, job_name, attempt_id, repo)
        if stop is not None:
            return stop
        if not check_design_approved(ledger, project, job_name):
            return FullOutcome("needs_owner", "design_approval", attempt_id, None)
        close_outcome = _close_attempt(config, ledger, fakes, project, attempt_id, job_name,
                                       on_progress, processor_factory)
        if close_outcome.status != "done":
            return FullOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None)
        return _finish_from_tasks(config, ledger, fakes, project, job_name, plan,
                                  ordered_tasks(plan), max_fix_rounds, on_progress,
                                  processor_factory)

    if stage == STAGE_REQUIREMENTS_REVIEW:
        # The owner has (outside this call) presumably fixed the plan's
        # own scenes/tasks `covers` or the app itself -- re-run
        # acceptance and the coverage check fresh, rather than trusting
        # the stale AcceptanceResult that caused this stop.
        return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name,
                                           plan, max_fix_rounds, on_progress,
                                           processor_factory, attempt_id)

    if stage == STAGE_SECURITY_FIX:
        report_path = Path(str(progress.get("report_path", "")))
        certified_fingerprint = progress.get("certified_fingerprint")
        from engine import evidence  # worker C's module; lazy import

        current_fingerprint = evidence.fingerprint(repo, config.projects[project].excludes)
        if certified_fingerprint and current_fingerprint != certified_fingerprint:
            # The source acceptance/handoff certified has changed since
            # then -- an owner's own security fix, committed or not (a
            # content fingerprint, unlike a plain HEAD compare, also
            # catches an uncommitted working-tree edit) -- so that stale
            # AcceptanceResult and handoff report can no longer be
            # trusted. Rerun acceptance, requirement coverage and the
            # handoff report fresh against the current source before
            # security runs again, exactly like STAGE_REQUIREMENTS_
            # REVIEW's own resume.
            return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name,
                                               plan, max_fix_rounds, on_progress,
                                               processor_factory, attempt_id)
        return _finish_after_handoff(config, ledger, project, job_name, plan, attempt_id,
                                     report_path, max_fix_rounds)

    if stage == STAGE_RELEASE_APPROVAL:
        # finalize_release itself no-ops (no mutation at all) while the
        # publish request is still queued/running -- a crash exactly
        # HERE (after the request reached a terminal status but before
        # report/close/record_release_decision ever ran) is the gap
        # this resume exists to close; see that function's own
        # docstring.
        request_id = str(progress.get("request_id", ""))
        report_path = str(progress.get("report_path", ""))
        return finalize_release(config, ledger, fakes, project, attempt_id, job_name,
                                request_id, report_path, on_progress, processor_factory)

    if stage == STAGE_TASK_OWNER:
        return _resume_task_owner(config, ledger, fakes, project, job_name, plan, attempt_id,
                                  progress, max_fix_rounds, on_progress, processor_factory)

    raise FullOrbitError(f"unknown progress stage: {stage!r}")


def _resume_task_owner(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                       job_name: str, plan: Plan, attempt_id: str,
                       progress: dict[str, object], max_fix_rounds: int,
                       on_progress: ProgressCallback | None,
                       processor_factory: orbit.ProcessorFactory | None) -> FullOutcome:
    """The STAGE_TASK_OWNER resume body -- shared by continue_full_job's
    own dispatch (a build task that stopped needs_owner, `progress`
    read straight off a `full_orbit.progress` event) and
    _resume_from_cursor's "task" branch (a build task that crashed or
    ended at an ordinary, non-needs_owner "failed" -- never journaled
    as `full_orbit.progress` at all, see record_cursor's own
    docstring), which synthesizes an equivalent `progress` dict with
    `stop_reason="crash_resume"` and `pending_requests=[]` instead.
    `progress["stop_reason"]` being "worker_stuck" OR "crash_resume"
    both mean the same thing here: there is nothing merged yet (or
    nothing NEW merged since the crash) worth a continuation note --
    rerun this task's build from scratch on the SAME attempt/copy,
    relying entirely on run_station/merge/review/bench/commit's own
    idempotent request ids (engine.processor.submit) to skip whatever
    already finished before the crash/stop and actually run whatever
    did not."""
    # The reviewer is always shown the plain base task text (no build-
    # station skill appendix) -- current_base_task_text falls back to
    # current_task_text only for a progress event recorded before this
    # field existed.
    base_task_text = str(progress.get("current_base_task_text",
                                      progress.get("current_task_text", "")))
    current_task_id = str(progress.get("current_task_id", ""))
    fixing = bool(progress.get("fixing"))
    context_gap = bool(progress.get("context_gap"))
    pending_requests = progress.get("pending_requests") or []
    if (not context_gap and progress.get("stop_reason") not in ("worker_stuck", "crash_resume")
            and isinstance(pending_requests, list) and pending_requests
            and not _pending_requests_fulfilled(ledger, project, pending_requests)):
        # The station asked the owner for something (e.g. an install)
        # and merge already landed its edits (see stages_after_install's
        # own docstring) -- but nothing here ever checked that whatever
        # was asked for actually happened before jumping straight to
        # review/bench on a tree that may still be missing it. Stay
        # needs_owner, with the SAME request(s) still pending, rather
        # than silently reviewing/benching against unresolved state.
        return FullOutcome("needs_owner", "owner_request", attempt_id, None)
    if fixing:
        all_owns = tuple(sorted({entry for t in plan.tasks for entry in t.owns}))
        task_obj: Task | None = Task(id=current_task_id, title="수락 실패 수정", detail="",
                                     owns=all_owns, depends_on=())
    else:
        by_id = {t.id: t for t in plan.tasks}
        task_obj = by_id.get(current_task_id)
        if task_obj is None:
            raise FullOrbitError(f"unknown task id for resume: {current_task_id!r}")

    # Never the project's own `release` for this per-task attempt's
    # commit stage either (see _config_without_release) -- a task-
    # attempt resume must not trigger engine.orbit's own per-commit
    # release stop any more than a fresh task attempt does.
    run_config = _config_without_release(config, project)
    task_text = str(progress.get("current_task_text", base_task_text))
    if context_gap:
        # The engine itself, not the worker, stopped this task
        # (unmet_required() -- a required skill/company-library lookup
        # never loaded) -- rebuild the station context FRESH and
        # recheck it before ever dispatching a worker: the owner may
        # have fixed the underlying config/skill root since, or may
        # not have yet. NEVER apply the install-followup note (nothing
        # here was ever installed), and never trust the stale
        # `current_task_text` this progress recorded (it was built
        # with no context appended at all, by construction).
        gap_sc = _build_station_context(config, ledger, project, attempt_id, "build",
                                        base_task_text, _planned_skills(config, plan, "build"))
        if gap_sc.unmet_required():
            reason = _unmet_context_reason(gap_sc)
            _record_progress(
                ledger, project, attempt_id, job_name,
                stage=STAGE_TASK_OWNER, resume_at=STAGE_TASK_OWNER,
                remaining_task_ids=progress.get("remaining_task_ids", []),
                current_task_id=current_task_id, current_task_text=task_text,
                current_base_task_text=base_task_text, stop_reason=reason, fixing=fixing,
                max_fix_rounds=max_fix_rounds, pending_requests=[], context_gap=True,
            )
            return FullOutcome("needs_owner", reason, attempt_id, None)
        task_text = f"{base_task_text}\n\n{gap_sc.text}" if gap_sc.text else base_task_text
        resume_stages = _rerun_build_task_stages(
            ledger, project, run_config, attempt_id, task_obj, plan, job_name,
            task_text, base_task_text)
    elif progress.get("stop_reason") in ("worker_stuck", "crash_resume"):
        # run_station itself stopped (engine.orbit's own continuation
        # limit, see CONTINUABLE_REASONS/MAX_STATION_CONTINUES) BEFORE
        # the merge stage after it ever ran -- there is nothing merged
        # yet to resume reviewing. Rerun this task's build from
        # scratch -- continue_n reset, since this is a fresh
        # run_station request, not a continuation of the exhausted
        # one -- on the SAME attempt/copy, then merge, then review
        # onward.
        resume_stages = _rerun_build_task_stages(
            ledger, project, run_config, attempt_id, task_obj, plan, job_name,
            task_text, base_task_text)
    else:
        # The station asked for something (e.g. an install) and got
        # it -- the fulfillment check above already confirmed this.
        # merge already landed whatever the builder had written
        # BEFORE it asked (engine.orbit.stages_after_install's own
        # docstring), but the task itself was never actually
        # finished. Rerun run_station on the SAME attempt/copy with a
        # continuation note saying the install is done and to pick
        # up from where it stopped -- never from scratch -- then
        # merge (narrowed to this task's own owns) and review onward,
        # exactly the same shape as the worker_stuck branch above.
        build_copy_path = copy_root(
            _config_for_build_task(run_config, job_name, plan), attempt_id, BUILDER_ROLE)
        followup_task_text = _install_followup_task_text(task_text, build_copy_path)
        resume_stages = _rerun_build_task_stages(
            ledger, project, run_config, attempt_id, task_obj, plan, job_name,
            followup_task_text, base_task_text)
    _replace_review_stage(resume_stages, config, ledger, project, plan, base_task_text)
    outcome = orbit.continue_job(run_config, ledger, _fakes_for(fakes, attempt_id), project,
                                 attempt_id, resume_stages, processor_factory=processor_factory,
                                 on_progress=on_progress)
    if outcome.status != "done" or outcome.pending_requests:
        full = _station_outcome_to_full(outcome, "build")
        if outcome.status == "needs_owner" or outcome.pending_requests:
            # This SAME resumed task stopped again (e.g. the builder
            # asked for another install) -- the journal's own
            # pending_requests/stop_reason are still whatever the
            # FIRST stop recorded unless overwritten here, which
            # would leave approve-install looking at an already-
            # fulfilled request forever instead of this new one (see
            # the sibling _record_progress calls in _run_tasks/
            # _run_acceptance_and_handoff, which this resume path
            # must match).
            _record_progress(
                ledger, project, outcome.attempt_id, job_name,
                stage=STAGE_TASK_OWNER, resume_at=STAGE_TASK_OWNER,
                remaining_task_ids=progress.get("remaining_task_ids", []),
                current_task_id=current_task_id, current_task_text=task_text,
                current_base_task_text=base_task_text, stop_reason=outcome.reason,
                fixing=fixing, fix_round=progress.get("fix_round"),
                max_fix_rounds=max_fix_rounds,
                pending_requests=list(outcome.pending_requests),
            )
        if full is not None:
            return full
    if progress.get("fixing"):
        return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name,
                                           plan, max_fix_rounds, on_progress,
                                           processor_factory, attempt_id)
    remaining_ids = [str(i) for i in progress.get("remaining_task_ids", [])]
    by_id = {t.id: t for t in plan.tasks}
    remaining_tasks = [by_id[i] for i in remaining_ids[1:] if i in by_id]
    return _finish_from_tasks(config, ledger, fakes, project, job_name, plan,
                              remaining_tasks, max_fix_rounds, on_progress,
                              processor_factory, attempt_id)


__all__ = [
    "FullOutcome", "FullOrbitError", "run_full_job", "continue_full_job",
    "record_release_decision", "finalize_release", "pending_owner_requests",
    "record_cursor", "resume_full_job_after_task_fix", "pending_release_progress",
    "config_without_release", "is_full_orbit_job",
    "FACTS_ROLE", "PROBER_ROLE", "PLANNER_ROLE", "DESIGNER_ROLE", "BUILDER_ROLE",
    "REVIEWER_ROLE",
    "STAGE_DESIGN_APPROVAL", "STAGE_TASK_OWNER", "STAGE_PROBE_RETRY", "STAGE_PLAN_REVIEW",
    "STAGE_REQUIREMENTS_REVIEW", "STAGE_SECURITY_FIX", "STAGE_RELEASE_APPROVAL",
]
