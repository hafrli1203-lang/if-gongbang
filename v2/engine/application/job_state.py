"""Where a job is right now: one ledger event per transition, one reader,
one resume entry point.

Every flow-layer transition (a station starting, a task attempt opening,
a stop for the owner, a fix attempt replacing a failed one) is recorded
as exactly one `job.position` event carrying a whole JobPosition.
`load_position` returns the newest one for a job. Ledgers written before
this event existed carry the same information split across
`full_orbit.cursor`, `full_orbit.progress` and the `fix.*` journal;
`load_position` reads those too and folds them into one JobPosition, so
an old job resumes the same way a new one does.

`resume_job` is the only way a stopped or crashed job continues: it loads
the position, finishes an interrupted fix transition first if there is
one, and otherwise dispatches on `stage` through one table.

`pending_owner_requests` is the only place that decides which owner
requests a job is still waiting on -- including a `kind="edit"` request
(repair_target.EDIT_REQUEST_KIND, OWNS_SCOPE_REQUEST_RULE, 결함 26/28):
unlike install/scripts_change/test_config_change, such a request is
never submitted to any processor at all, so it is fulfilled only by an
owner's `--also-own` grant (repair_target.OWNS_EXTENDED_KIND) covering
its path, recorded strictly after it was proposed -- see
_edit_request_fulfilled.

`FLOW_BOUNDARIES` lists every named crash boundary the flow layer
(engine.application.pipeline, engine.application.repair, engine.application.quick_pipeline, engine.application.steps.close)
passes through right before a ledger write."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from engine.domain import faults
from engine.domain.records import Event

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger
    from engine.application import task_pipeline as orbit
    from engine.config import EngineConfig
    from engine.application.repair import FixResult
    from engine.application.pipeline import FullOutcome
    from engine.application.processor import Fakes

POSITION_KIND = "job.position"
_LEGACY_CURSOR_KIND = "full_orbit.cursor"
_LEGACY_PROGRESS_KIND = "full_orbit.progress"

# Stages a running job passes through.
STAGE_FACTS = "facts"
STAGE_PROBE = "probe"
STAGE_PLAN = "plan"
STAGE_DESIGN = "design"
STAGE_TASK = "task"
STAGE_POST_TASKS = "post_tasks"
STAGE_SMALL_PLAN = "small_plan"
# Planning is finished; the shared facts/probe/plan/design attempt is
# being closed before the first task attempt opens.
STAGE_EARLY_CLOSE = "early_close"
# A plain single-task job (`gb2 run`) that `gb2 fix` repaired: it has a
# position only so the fix attempt's opening is recorded once; there is
# nothing for resume_job to continue.
STAGE_SINGLE = "single_task"

# Stages a job stops at for the owner.
STAGE_DESIGN_APPROVAL = "awaiting_design_approval"
STAGE_TASK_OWNER = "awaiting_task_owner"
STAGE_PROBE_RETRY = "awaiting_probe_retry"
STAGE_PLAN_REVIEW = "awaiting_plan_review"
STAGE_REQUIREMENTS_REVIEW = "awaiting_requirements_review"
STAGE_SECURITY_FIX = "awaiting_security_fix"
STAGE_RELEASE_APPROVAL = "awaiting_release_approval"
# A facts/probe/plan/design station's own run_station stop (an owner
# request, a worker_stuck continuation limit, or a required-context gap).
STAGE_EARLY_STATION = "awaiting_early_station_owner"

STAGES = (
    STAGE_FACTS, STAGE_PROBE, STAGE_PLAN, STAGE_DESIGN, STAGE_TASK, STAGE_POST_TASKS,
    STAGE_SMALL_PLAN, STAGE_EARLY_CLOSE, STAGE_SINGLE, STAGE_DESIGN_APPROVAL, STAGE_TASK_OWNER,
    STAGE_PROBE_RETRY, STAGE_PLAN_REVIEW, STAGE_REQUIREMENTS_REVIEW, STAGE_SECURITY_FIX,
    STAGE_RELEASE_APPROVAL, STAGE_EARLY_STATION,
)

# Stages whose attempt is one build task's own attempt (a fix may replace it).
TASK_STAGES = (STAGE_TASK, STAGE_TASK_OWNER)

FLOW_BOUNDARIES: tuple[str, ...] = (
    *(f"position.record.{stage}" for stage in STAGES),
    "full_orbit.release_decision",
    "full_orbit.probe_skipped",
    "full_orbit.context_injected",
    "full_orbit.context_failed",
    "full_orbit.orphan_abandoned",
    "full_orbit.plan_reviewed",
    "full_orbit.attempt_opened",
    "small_orbit.plan_reviewed",
    "fixup.fix.started",
    "fixup.fix.past_cap",
    "fixup.fix.closed_old",
    "fixup.fix.opening_new",
    "fixup.fix.opening_failed",
    "fixup.fix.opened_new",
    "fixup.fix.restored",
    "fixup.fix.submitted",
    "fixup.fix.finished",
    "fixup.fix.task_stop",
    "close.attempt_finished",
    "close.state_closed",
)


@dataclass(frozen=True)
class FixState:
    """The fix cycle (engine.application.repair.fix) that replaced `old_attempt_id`.
    `new_attempt_id` is None while the replacement is not yet opened;
    `finished` is computed from the fix journal when the position is
    loaded, never stored."""
    old_attempt_id: str
    new_attempt_id: str | None = None
    finished: bool = False


@dataclass(frozen=True)
class JobPosition:
    job: str
    stage: str
    attempt_id: str | None
    task_id: str | None = None
    remaining_task_ids: tuple[str, ...] = ()
    fixing: bool = False
    fix: FixState | None = None
    pending_requests: tuple[dict, ...] = ()
    stop_reason: str | None = None
    extra: dict = field(default_factory=dict)

    def get(self, key: str, default: object = None) -> object:
        return self.extra.get(key, default)

    @property
    def max_fix_rounds(self) -> int:
        value = self.extra.get("max_fix_rounds", 2)
        return value if isinstance(value, int) else 2


def _position_payload(pos: JobPosition) -> dict[str, object]:
    fix = None
    if pos.fix is not None:
        fix = {"old_attempt_id": pos.fix.old_attempt_id,
               "new_attempt_id": pos.fix.new_attempt_id}
    return {
        "job": pos.job, "stage": pos.stage, "attempt_id": pos.attempt_id,
        "task_id": pos.task_id, "remaining_task_ids": list(pos.remaining_task_ids),
        "fixing": pos.fixing, "fix": fix, "pending_requests": list(pos.pending_requests),
        "stop_reason": pos.stop_reason, "extra": dict(pos.extra),
    }


def record_position(ledger: Ledger, project: str, pos: JobPosition) -> None:
    """Appends the one `job.position` event for this transition."""
    faults.flow_boundary(f"position.record.{pos.stage}")
    ledger.append(Event(
        project=project, kind=POSITION_KIND, actor_kind="processor", actor_id="processor",
        attempt_id=pos.attempt_id, payload=_position_payload(pos),
    ))


# ---------------------------------------------------------------------
# reading: new events and the legacy cursor/progress/fix journal
# ---------------------------------------------------------------------

def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict_requests(raw: object) -> tuple[dict, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(r for r in raw if isinstance(r, dict))


def _str_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(str(i) for i in raw)


def _from_position_event(payload: dict) -> JobPosition:
    fix_raw = payload.get("fix")
    fix = None
    if isinstance(fix_raw, dict) and isinstance(fix_raw.get("old_attempt_id"), str):
        fix = FixState(str(fix_raw["old_attempt_id"]), _str_or_none(fix_raw.get("new_attempt_id")))
    extra = payload.get("extra")
    return JobPosition(
        job=str(payload.get("job")), stage=str(payload.get("stage")),
        attempt_id=_str_or_none(payload.get("attempt_id")),
        task_id=_str_or_none(payload.get("task_id")),
        remaining_task_ids=_str_tuple(payload.get("remaining_task_ids")),
        fixing=bool(payload.get("fixing")), fix=fix,
        pending_requests=_dict_requests(payload.get("pending_requests")),
        stop_reason=_str_or_none(payload.get("stop_reason")),
        extra=dict(extra) if isinstance(extra, dict) else {},
    )


_LEGACY_CURSOR_FIELDS = {
    "job", "stage", "attempt_id", "task_id", "remaining_task_ids", "fixing",
    "pending_requests", "stop_reason", "fixing_attempt", "fix_old_attempt_id",
}


def _from_legacy_cursor(payload: dict) -> JobPosition:
    fix = None
    old = _str_or_none(payload.get("fix_old_attempt_id"))
    if payload.get("fixing_attempt") and old is not None:
        fix = FixState(old, _str_or_none(payload.get("attempt_id")))
    return JobPosition(
        job=str(payload.get("job")), stage=str(payload.get("stage")),
        attempt_id=_str_or_none(payload.get("attempt_id")),
        task_id=_str_or_none(payload.get("task_id")),
        remaining_task_ids=_str_tuple(payload.get("remaining_task_ids")),
        fixing=bool(payload.get("fixing")), fix=fix,
        pending_requests=_dict_requests(payload.get("pending_requests")),
        stop_reason=_str_or_none(payload.get("stop_reason")),
        extra={k: v for k, v in payload.items() if k not in _LEGACY_CURSOR_FIELDS},
    )


_LEGACY_PROGRESS_FIELDS = {
    "job", "stage", "attempt_id", "resume_at", "current_task_id", "current_task_text",
    "current_base_task_text", "remaining_task_ids", "fixing", "pending_requests",
    "stop_reason",
}


def _from_legacy_progress(payload: dict) -> JobPosition:
    extra = {k: v for k, v in payload.items() if k not in _LEGACY_PROGRESS_FIELDS}
    if "current_task_text" in payload:
        extra["task_text"] = payload["current_task_text"]
    if "current_base_task_text" in payload:
        extra["base_task_text"] = payload["current_base_task_text"]
    return JobPosition(
        job=str(payload.get("job")), stage=str(payload.get("stage")),
        attempt_id=_str_or_none(payload.get("attempt_id")),
        task_id=_str_or_none(payload.get("current_task_id")),
        remaining_task_ids=_str_tuple(payload.get("remaining_task_ids")),
        fixing=bool(payload.get("fixing")),
        pending_requests=_dict_requests(payload.get("pending_requests")),
        stop_reason=_str_or_none(payload.get("stop_reason")), extra=extra,
    )


_READERS: tuple[tuple[str, Callable[[dict], JobPosition]], ...] = (
    (POSITION_KIND, _from_position_event),
    (_LEGACY_CURSOR_KIND, _from_legacy_cursor),
    (_LEGACY_PROGRESS_KIND, _from_legacy_progress),
)


def _latest_fix_started(ledger: Ledger, project: str,
                        old_attempt_id: str) -> int | None:
    started: int | None = None
    for seq, event in ledger.events(project, "fix.started"):
        if event.payload.get("old_attempt_id") == old_attempt_id:
            started = seq
    return started


def _fix_finished_after(ledger: Ledger, project: str, started_seq: int) -> bool:
    return any(seq > started_seq for seq, _event in ledger.events(project, "fix.finished"))


def fix_opened_attempt(ledger: Ledger, project: str, old_attempt_id: str,
                       after_seq: int) -> str | None:
    """The replacement attempt a fix cycle started at `after_seq` already
    opened for `old_attempt_id`: a `job.position` whose fix names it, or
    (legacy ledgers) a `fix.opened_new` event. None if not opened yet."""
    found: tuple[int, str] | None = None
    for seq, event in ledger.events(project, POSITION_KIND):
        fix_raw = event.payload.get("fix")
        if (seq > after_seq and isinstance(fix_raw, dict)
                and fix_raw.get("old_attempt_id") == old_attempt_id
                and isinstance(fix_raw.get("new_attempt_id"), str)):
            found = (seq, str(fix_raw["new_attempt_id"]))
    for seq, event in ledger.events(project, "fix.opened_new"):
        if seq > after_seq and event.attempt_id == old_attempt_id:
            if found is None or seq > found[0]:
                found = (seq, str(event.payload["new_attempt_id"]))
    return found[1] if found is not None else None


def _with_fix_journal(ledger: Ledger, project: str, pos: JobPosition) -> JobPosition:
    """Folds the fix journal into `pos`. The newest fix cycle that is
    replacing the position's OWN attempt comes first (a later repair of a
    repair is the one in flight); only when there is none does the
    position's recorded fix pointer get its `finished` state. A cycle
    that already opened its replacement moves a task position onto it
    (legacy ledgers recorded that move separately)."""
    if pos.attempt_id is not None:
        started = _latest_fix_started(ledger, project, pos.attempt_id)
        if started is not None:
            finished = _fix_finished_after(ledger, project, started)
            if not finished or pos.fix is None:
                return _fold_cycle(ledger, project, pos, pos.attempt_id, started, finished)
    if pos.fix is None:
        return pos
    started = _latest_fix_started(ledger, project, pos.fix.old_attempt_id)
    if started is None:
        return pos
    return replace(pos, fix=replace(pos.fix,
                                    finished=_fix_finished_after(ledger, project, started)))


def _fold_cycle(ledger: Ledger, project: str, pos: JobPosition, old: str, started: int,
                finished: bool) -> JobPosition:
    new_attempt = fix_opened_attempt(ledger, project, old, started)
    fix = FixState(old, new_attempt, finished)
    if new_attempt is not None and pos.stage in TASK_STAGES:
        return replace(pos, stage=STAGE_TASK, attempt_id=new_attempt, fix=fix,
                       pending_requests=(), stop_reason=None)
    if finished:
        return pos
    return replace(pos, fix=fix)


def early_station_rounds_used(ledger: Ledger, project: str, job_name: str) -> int:
    """How many DISTINCT attempts of this job's shared facts/probe/plan/
    design attempt have already stopped at STAGE_PROBE_RETRY or
    STAGE_PLAN_REVIEW and been reopened
    (engine.application.pipeline_resume._resume_reopened_station) -- the
    early-station counterpart of engine.application.repair_target.
    fix_rounds_used, which counts the build stage's own per-task rounds
    instead. Counted by distinct `attempt_id` (never by raw event
    count): a cap-stop this module's own caller records for an attempt
    that is NOT being reopened again (_early_station_round_stop, same
    attempt_id, no new attempt) must not inflate this total on its
    own. Never reset at a commit (there is none before the plan is
    reviewed, unlike a build task's own fix rounds) -- used to enforce
    the SAME JobPosition.max_fix_rounds cap engine.application.repair.
    fix() already enforces for `gb2 fix`, for `gb2 continue`'s own
    early-station reopen too (a past incident, 반장 지시: appX
    chunkA-planning-loop,, 5 consecutive plan_review_failed
    stops with NO cap of any kind on this path before)."""
    attempt_ids: set[str] = set()
    for _seq, event in ledger.events(project, POSITION_KIND):
        if (event.payload.get("job") == job_name
                and event.payload.get("stage") in (STAGE_PROBE_RETRY, STAGE_PLAN_REVIEW)
                and isinstance(event.attempt_id, str)):
            attempt_ids.add(event.attempt_id)
    return len(attempt_ids)


def acceptance_fix_rounds_used(ledger: Ledger, project: str, job_name: str) -> int:
    """How many acceptance-fix rounds (engine.application.pipeline_tasks.
    run_acceptance_and_handoff's own `fix-{n}` tasks) this job has
    already opened, EVER -- by distinct `attempt_id` among every
    job.position this job has recorded with `fixing` True (set only by
    run_acceptance_and_handoff's own two fixing=True call sites, never
    by anything else). a review finding, a past incident's own additional
    scope): run_acceptance_and_handoff used to start its own in-memory
    `rounds` at 0 on EVERY call, including a resume
    (engine.application.pipeline_resume_tasks.advance_past_task, when a
    fix round's own task attempt itself stopped needs_owner mid-round
    and `gb2 continue` reopened it) -- so a job that had already spent
    max_fix_rounds got max_fix_rounds MORE every time it was resumed,
    forever, the SAME blind spot early_station_rounds_used exists to
    fix for `gb2 continue`'s early-station reopen. Passing this count
    as run_acceptance_and_handoff's OWN starting `rounds` (instead of a
    bare 0) makes the cap persist across any number of resumes."""
    attempt_ids: set[str] = set()
    for _seq, event in ledger.events(project, POSITION_KIND):
        if (event.payload.get("job") == job_name and event.payload.get("fixing") is True
                and isinstance(event.attempt_id, str)):
            attempt_ids.add(event.attempt_id)
    return len(attempt_ids)


def load_position(ledger: Ledger, project: str, job: str) -> JobPosition | None:
    """The newest position recorded for `job` (any of `job.position`,
    legacy `full_orbit.cursor`, legacy `full_orbit.progress`, by seq), with
    the fix journal folded in. None if nothing was ever recorded."""
    latest: tuple[int, JobPosition] | None = None
    for kind, parse in _READERS:
        for seq, event in ledger.events(project, kind):
            if event.payload.get("job") != job:
                continue
            if latest is None or seq > latest[0]:
                latest = (seq, parse(event.payload))
    if latest is None:
        return None
    return _with_fix_journal(ledger, project, latest[1])


def opening_roles(pos: JobPosition) -> tuple[str, ...] | None:
    """The roles an attempt is to be opened with, iff `pos` is an opening
    intent: recorded before open_job, with no attempt id yet."""
    roles = pos.extra.get("roles")
    if pos.attempt_id is not None or not isinstance(roles, list):
        return None
    return tuple(str(r) for r in roles)


def is_full_job(pos: JobPosition | None) -> bool:
    """True iff `pos` belongs to a full-orbit (or small-mode) job, whose
    task attempts never release on their own."""
    return pos is not None and pos.stage != STAGE_SINGLE


def without_release(config: "EngineConfig", project: str) -> "EngineConfig":
    """`config` with this project's own `release` removed: a task attempt
    inside a full job never stops for a per-task release; the job makes one
    publish request at the very end."""
    if config.projects[project].release is None:
        return config
    stripped = replace(config.projects[project], release=None)
    return replace(config, projects={**config.projects, project: stripped})


def stages_spec(stages: "list[orbit.Stage]") -> list[dict]:
    """A task attempt's stage list as recorded on its position: the one
    canonical spec its first run, its fix and every recovery replay."""
    return [{"kind": stage.kind, "payload": stage.payload} for stage in stages]


def stages_from_spec(spec: list[dict], config: "EngineConfig", attempt_id: str, *,
                     task_text: str | None = None,
                     merge_retry_n: int = 0, resume_n: int = 0) -> "list[orbit.Stage]":
    """The recorded stages for `attempt_id`: every payload as recorded,
    with each merge copy pointed at this attempt's own copy of its role.
    `task_text` replaces the run_station task (a fix's composed task, or
    a continuation note), `merge_retry_n` marks a re-merge on the same
    attempt, and `resume_n` (결함 61) is metadata-only -- never shown to
    the worker, it exists purely to make this run_station payload hash
    to a request_id distinct from an earlier owner-request resume on
    this same attempt whose own followup text happened to come out
    byte-identical (same unchanged dirty_paths, same base_task_text):
    without it, processor.submit's own idempotency (same request_id,
    not 'failed' -> return the existing id, no new event) silently
    reuses that earlier, already-finished request and the worker is
    never actually dispatched again. The only deliberate differences
    from the spec."""
    from engine.application import task_pipeline as orbit
    from engine.application.steps.open_job import copy_root

    stages: list[orbit.Stage] = []
    for entry in spec:
        kind = str(entry["kind"])
        payload = dict(entry["payload"])
        if kind == "run_station" and task_text is not None:
            payload["task"] = task_text
            if resume_n:
                payload["resume_n"] = resume_n
        if kind == "merge":
            payload["copies"] = [
                {**copy, "path": str(copy_root(config, attempt_id, str(copy["role"])))}
                for copy in payload.get("copies", [])
            ]
            if merge_retry_n:
                payload["merge_retry_n"] = merge_retry_n
        stages.append(orbit.Stage(kind, payload))
    return stages


def _widen_owns_list(owns: list, extra_owns: tuple[str, ...]) -> list[str]:
    """Deliberately duplicates engine.application.repair's own private
    `_widen_owns` (same bd38249-style small-helper duplication this
    codebase already uses for a handful of module-private lookups --
    see e.g. repair_target._job_name_for_attempt's own docstring):
    importing it here would mean this module depending on
    engine.application.repair, which itself imports THIS module
    (engine.application.job_state) -- a real import cycle, not a
    style choice."""
    widened = list(owns)
    for path in extra_owns:
        if path not in widened:
            widened.append(path)
    return widened


def widen_stage_owns(stages: "list[orbit.Stage]",
                     extra_owns: tuple[str, ...]) -> "list[orbit.Stage]":
    """`stages`, unchanged, iff `extra_owns` is empty; otherwise a new
    list with every run_station payload's own "owns" and every merge
    copy's own "owns" widened by `extra_owns` on top of whatever it
    already lists -- the SAME technique engine.application.repair._fix_spec
    uses for a brand-new fix attempt, reused here so a job resuming a
    STILL-OPEN task attempt (engine.application.pipeline_resume_tasks.
    rerun_task_owner_stop, 결함 28: an owner's `--also-own` grant for a
    pending "edit" request, approved without opening any new attempt at
    all) sees the exact same owner-granted widening the live gate
    (engine.application.steps.worker_gate_log.task_owns) and merge's own
    re-check (engine.application.steps.merge._owner_prefixes) both
    trust. Never touches any OTHER stage kind, and never plan.json
    itself (frozen evidence, same reasoning _fix_spec's own docstring
    gives)."""
    from engine.application import task_pipeline as orbit

    if not extra_owns:
        return stages
    out: list[orbit.Stage] = []
    for stage in stages:
        payload = stage.payload
        if stage.kind == "run_station" and isinstance(payload.get("owns"), list):
            payload = {**payload, "owns": _widen_owns_list(payload["owns"], extra_owns)}
        elif stage.kind == "review" and isinstance(payload.get("task"), str):
            # The reviewer must judge against the same widened owns, or it
            # flags the owner-approved paths as out of scope (결함 32).
            note = ("\n\nPaths the owner added to this task's owns (approved, "
                    "in scope): " + ", ".join(extra_owns))
            if note not in payload["task"]:
                payload = {**payload, "task": payload["task"] + note}
        elif stage.kind == "merge":
            payload = {**payload, "copies": [
                ({**copy, "owns": _widen_owns_list(copy["owns"], extra_owns)}
                 if isinstance(copy.get("owns"), list) else copy)
                for copy in payload.get("copies", [])
            ]}
        out.append(orbit.Stage(stage.kind, payload))
    return out


def fix_opened_position(pos: JobPosition | None, job: str, old_attempt_id: str,
                        new_attempt_id: str, task_text: str,
                        stages: list[dict] | None = None) -> JobPosition:
    """The one position that records a fix cycle's replacement attempt: a
    task position on the replacement when the fix replaced the job's own
    current task attempt, the job's position marked with the fix
    otherwise, or a single-task position for a plain `gb2 run` job."""
    fix = FixState(old_attempt_id, new_attempt_id)
    if not is_full_job(pos):
        return JobPosition(job=job, stage=STAGE_SINGLE, attempt_id=new_attempt_id, fix=fix)
    assert pos is not None
    if pos.stage in TASK_STAGES and pos.attempt_id == old_attempt_id:
        extra = {"task_text": task_text, "base_task_text": task_text,
                 "max_fix_rounds": pos.max_fix_rounds, "next_action": "run_station"}
        if stages is not None:
            extra["stages"] = stages
        return JobPosition(job=job, stage=STAGE_TASK, attempt_id=new_attempt_id,
                           task_id=pos.task_id, remaining_task_ids=pos.remaining_task_ids,
                           fixing=pos.fixing, fix=fix, extra=extra)
    return replace(pos, fix=fix)


def task_stop_position(pos: JobPosition, attempt_id: str | None, reason: str | None,
                       pending: tuple[dict, ...] | list[dict], **extra: object) -> JobPosition:
    """`pos` (a task position) stopped for the owner on `attempt_id`."""
    return replace(pos, stage=STAGE_TASK_OWNER, attempt_id=attempt_id, stop_reason=reason,
                   pending_requests=tuple(pending), extra={**pos.extra, **extra})


# ---------------------------------------------------------------------
# resuming
# ---------------------------------------------------------------------

def resume_job(config: "EngineConfig", ledger: Ledger,
               fakes: "Fakes | Callable[[str], Fakes]", project: str, job: str, *,
               on_progress: "orbit.ProgressCallback | None" = None,
               processor_factory: "orbit.ProcessorFactory | None" = None,
               past_cap: bool = False) -> "FullOutcome":
    """The one entry point that continues a stopped or crashed job:
    `gb2 continue`, `approve-design`, `approve-install` and the
    continuation after `gb2 fix` all come here. Before dispatching it
    finishes a close of this job's attempt left in flight, reconciles an
    opening intent with the attempt it was for, and checks the plan
    against its review (full_orbit.resumed_plan_stop).

    `past_cap` (a past incident, 반장 지시, `gb2 continue --past-cap`):
    forwarded onto the Run this dispatches through, read only by
    _resume_reopened_station's own early-station cap/no-progress check
    -- every other caller (approve-design, approve-install, the
    continuation after `gb2 fix`) leaves it False, since none of those
    ever reach that check."""
    from engine import full_orbit

    pos = load_position(ledger, project, job)
    if pos is None and not ledger.attempts_for_job_name(project, job):
        raise full_orbit.FullOrbitError(f"no attempt for job {job!r}")
    if pos is None or pos.stage == STAGE_SINGLE:
        raise full_orbit.FullOrbitError(f"no pending stop recorded for job {job!r}")
    run = full_orbit.Run(config, ledger, fakes, project, job, on_progress, processor_factory,
                         past_cap=past_cap)
    unfinished_close = full_orbit.finish_inflight_close(run)
    if unfinished_close is not None:
        return unfinished_close
    roles = opening_roles(pos)
    if roles is not None:
        pos = full_orbit.reconcile_opening(run, pos, roles)
    stop = full_orbit.resumed_plan_stop(run, pos)
    if stop is not None:
        return stop
    if pos.fix is not None and not pos.fix.finished:
        return _finish_fix_then_resume(run)
    handler = full_orbit.RESUME_TABLE.get(pos.stage)
    if handler is None:
        raise full_orbit.FullOrbitError(f"unknown position stage: {pos.stage!r}")
    return handler(run, pos)


def resume_job_from(config: "EngineConfig", ledger: Ledger,
                    fakes: "Fakes | Callable[[str], Fakes]", project: str, job: str,
                    from_station: str, *,
                    on_progress: "orbit.ProgressCallback | None" = None,
                    processor_factory: "orbit.ProcessorFactory | None" = None) -> "FullOutcome":
    """`gb2 continue <app> --from <station>`: the escape hatch for a job
    stuck needs_owner/failed at an early station (facts/probe/plan/
    design) because the owner's fix belongs at an EARLIER station than
    the one it is stopped at (결함 24, -- see
    full_orbit.force_reopen_station's own docstring for the real
    incident). Same opening work as resume_job (finish an in-flight
    close, reconcile an opening intent) but dispatches through
    full_orbit.force_reopen_station instead of RESUME_TABLE, and never
    through resumed_plan_stop -- force_reopen_station's own station-
    order check already refuses anything resumed_plan_stop would
    otherwise gate on (the build stage)."""
    from engine import full_orbit

    pos = load_position(ledger, project, job)
    if pos is None and not ledger.attempts_for_job_name(project, job):
        raise full_orbit.FullOrbitError(f"no attempt for job {job!r}")
    if pos is None or pos.stage == STAGE_SINGLE:
        raise full_orbit.FullOrbitError(f"no pending stop recorded for job {job!r}")
    run = full_orbit.Run(config, ledger, fakes, project, job, on_progress, processor_factory)
    unfinished_close = full_orbit.finish_inflight_close(run)
    if unfinished_close is not None:
        return unfinished_close
    roles = opening_roles(pos)
    if roles is not None:
        pos = full_orbit.reconcile_opening(run, pos, roles)
    return full_orbit.force_reopen_station(run, pos, from_station)


def _finish_fix_then_resume(run: "full_orbit.Run") -> "FullOutcome":
    """An interrupted fix transition is finished by engine.application.repair.fix
    itself (it resumes its own journal), then the job continues."""
    from engine import full_orbit
    from engine.application import repair as fixup

    try:
        result = fixup.fix(run.config, run.ledger, run.project, "", run.fakes_for,
                           on_progress=run.on_progress,
                           processor_factory=run.processor_factory)
    except fixup.FixupError as exc:
        return full_orbit.FullOutcome("failed", f"fix:{exc}"[:300], None, None)
    outcome = resume_after_fix(run.config, run.ledger, run.fakes, run.project, result,
                               on_progress=run.on_progress,
                               processor_factory=run.processor_factory)
    if outcome is not None:
        return outcome
    return full_orbit.FullOutcome(result.outcome.status, result.outcome.reason,
                                  result.new_attempt_id, None)


def resume_after_fix(config: "EngineConfig", ledger: Ledger,
                     fakes: "Fakes | Callable[[str], Fakes]", project: str,
                     result: "FixResult", *,
                     on_progress: "orbit.ProgressCallback | None" = None,
                     processor_factory: "orbit.ProcessorFactory | None" = None,
                     ) -> "FullOutcome | None":
    """None for a plain single-task job (the fix outcome is the whole
    job's outcome). For a full job: the repaired task's own stop, as-is,
    when it did not finish (engine.application.repair.fix already recorded it), or
    resume_job once it did."""
    from engine import full_orbit

    if not result.full_job:
        return None
    if result.outcome.status != "done":
        return full_orbit.FullOutcome(result.outcome.status, result.outcome.reason,
                                      result.new_attempt_id, None)
    return resume_job(config, ledger, fakes, project, result.job_name,
                      on_progress=on_progress, processor_factory=processor_factory)


# Re-exported from job_state_requests (split out to keep this module
# under the 800-line architecture cap -- see that module's own
# docstring). Imported here at the BOTTOM, after JobPosition/
# load_position already exist on this module's own namespace: that
# module imports those two names from here at ITS top, so importing it
# any earlier (before they are defined) would be the exact circular
# import engine.application.pipeline_base's own docstring warns about.
from engine.application.job_state_requests import (  # noqa: E402
    hand_off_later_task_requests, outstanding_requests, pending_owner_requests, proposal_id,
    proposal_seq, request_fulfilled as _request_fulfilled,
)

__all__ = [
    "JobPosition", "FixState", "record_position", "load_position", "pending_owner_requests",
    "resume_job", "resume_job_from", "resume_after_fix", "FLOW_BOUNDARIES", "STAGES",
    "fix_opened_position", "fix_opened_attempt", "task_stop_position", "without_release",
    "is_full_job", "stages_spec", "stages_from_spec", "opening_roles", "proposal_id",
    "proposal_seq", "widen_stage_owns", "outstanding_requests", "early_station_rounds_used",
    "acceptance_fix_rounds_used", "hand_off_later_task_requests",
]
