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

import posixpath
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from engine.domain import faults
from engine.domain.policy import owns_within_prefixes
from engine.domain.records import Event, LedgerError
from engine.application.owner_approval import PENDING_KIND_WHITELIST
from engine.domain.policy import NOTE_REQUEST_KIND
from engine.application.repair_target import (
    HANDOFF_KIND, OWNS_EXTENDED_KIND, is_owns_scope_request, owning_later_task, record_request_handoff,
)

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
                     merge_retry_n: int = 0) -> "list[orbit.Stage]":
    """The recorded stages for `attempt_id`: every payload as recorded,
    with each merge copy pointed at this attempt's own copy of its role.
    `task_text` replaces the run_station task (a fix's composed task, or
    a continuation note) and `merge_retry_n` marks a re-merge on the same
    attempt -- the only two deliberate differences from the spec."""
    from engine.application import task_pipeline as orbit
    from engine.application.steps.open_job import copy_root

    stages: list[orbit.Stage] = []
    for entry in spec:
        kind = str(entry["kind"])
        payload = dict(entry["payload"])
        if kind == "run_station" and task_text is not None:
            payload["task"] = task_text
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
# owner requests still outstanding
# ---------------------------------------------------------------------

def _normalized_manifest(raw: object) -> str:
    return posixpath.normpath(str(raw))


def proposal_seq(ledger: Ledger, project: str, attempt_id: str | None, request: dict) -> int:
    """The proposal's identity in the ledger: the seq of the newest
    station.finished on `attempt_id` that proposed exactly `request`
    (0 when none -- a position recorded without one)."""
    seq = 0
    for event_seq, event in ledger.events(project, "station.finished"):
        if event.attempt_id == attempt_id and request in (event.payload.get("requests") or []):
            seq = event_seq
    return seq


def proposal_id(ledger: Ledger, project: str, pos: JobPosition, request: dict) -> str:
    """The identity of `request` as proposed for `pos`: its attempt and
    the seq of the station.finished that proposed it. An owner approval
    carries it (engine.application.owner_approval.approve_pending `proposal`), so a repeated
    identical proposal is approved as its own request."""
    return f"{pos.attempt_id}:{proposal_seq(ledger, project, pos.attempt_id, request)}"


def _manifest_holds(repo: Path, manifest: str, change: object) -> bool:
    """True iff the repository's current `manifest` already carries
    `change` (a JSON manifest's sections, or a line manifest's lines)."""
    import json

    from engine.infrastructure.installers import InstallFailed, merge_one_level, validate_add_lines

    try:
        data = (repo / manifest).read_bytes()
    except OSError:
        return False
    if not isinstance(change, dict):
        return False
    if manifest.endswith(".json"):
        try:
            current = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        return isinstance(current, dict) and merge_one_level(current, change) == current
    try:
        wanted = validate_add_lines(change)
    except InstallFailed:
        return False
    present = {line.strip() for line in data.decode("utf-8", "replace").splitlines()}
    return all(line in present for line in wanted)


def _edit_request_fulfilled(ledger: Ledger, project: str, request: dict, *,
                            job_name: str | None, task_id: str | None,
                            after_seq: int) -> bool:
    """True iff `request` (repair_target.is_owns_scope_request -- a
    `kind="edit"` request, or (결함 31) any unknown kind shaped
    {"path": str, ...} -- OWNS_SCOPE_REQUEST_RULE, 결함 26) names a path
    an owner's `gb2 fix <app> --also-own <path>` grant
    (repair_target.OWNS_EXTENDED_KIND, for THIS job's `task_id`) already
    covers, with that grant itself recorded strictly after `request` was
    proposed (`after_seq`). Such a request is never submitted to any
    processor (PENDING_KIND_WHITELIST never lists "edit", nor any kind
    it does not itself recognize -- see repair_target.
    is_owns_scope_request's own docstring), so there is no
    "install.recorded"-shaped engine run to look for the way
    _request_fulfilled's whitelisted branch does; the owner's grant
    event is the only evidence of approval that can ever exist."""
    if job_name is None or task_id is None:
        return False
    path = request.get("path")
    if not isinstance(path, str) or not path:
        return False
    # Handed to a later task of the plan (결함 31): fulfilled for THIS stop.
    for seq, event in ledger.events(project, HANDOFF_KIND):
        if (seq > after_seq and event.payload.get("job_name") == job_name
                and event.payload.get("from_task_id") == task_id
                and event.payload.get("path") == path):
            return True
    for seq, event in ledger.events(project, OWNS_EXTENDED_KIND):
        if seq <= after_seq:
            continue
        if (event.payload.get("job_name") != job_name
                or event.payload.get("task_id") != task_id):
            continue
        raw = event.payload.get("paths")
        if not isinstance(raw, list):
            continue
        if any(owns_within_prefixes((path,), (str(p),)) for p in raw):
            return True
    return False


def _request_fulfilled(ledger: Ledger, project: str, request: dict, *, after_seq: int,
                       repo: Path | None, proposal: str,
                       job_name: str | None = None, task_id: str | None = None) -> bool:
    """True iff `request` is a kind an owner can approve, an engine run of
    THAT kind applied exactly its manifest+change after the proposal was
    made (`after_seq`), and -- when `repo` is given -- the manifest still
    holds the change now. `job_name`/`task_id` (the position's own, see
    outstanding_requests) are only read for the EDIT_REQUEST_KIND branch
    (_edit_request_fulfilled), which has no manifest/change at all."""
    kind = request.get("kind")
    if kind == NOTE_REQUEST_KIND:
        return True  # informational note, never awaits the owner (결함 33)
    if is_owns_scope_request(request):
        return _edit_request_fulfilled(ledger, project, request, job_name=job_name,
                                       task_id=task_id, after_seq=after_seq)
    if kind not in PENDING_KIND_WHITELIST:
        return False
    manifest = _normalized_manifest(request.get("manifest"))
    change = request.get("change")
    applied = any(
        seq > after_seq and event.payload.get("kind") == kind
        and _normalized_manifest(event.payload.get("manifest")) == manifest
        and event.payload.get("change") == change
        and _answers(ledger, event.payload.get("request_id"), proposal)
        for seq, event in ledger.events(project, "install.recorded")
    )
    if not applied:
        return False
    return repo is None or _manifest_holds(repo, manifest, change)


def _answers(ledger: Ledger, request_id: object, proposal: str) -> bool:
    """An install run answers `proposal` unless it was approved for a
    different proposal (a request carrying no proposal answers any)."""
    if not isinstance(request_id, str):
        return True
    try:
        approved_for = ledger.request(request_id).payload.get("proposal")
    except LedgerError:
        return True
    return approved_for is None or approved_for == proposal


def outstanding_requests(ledger: Ledger, project: str, pos: JobPosition | None, *,
                         repo: Path | None = None) -> tuple[dict, ...]:
    if pos is None:
        return ()
    return tuple(
        r for r in pos.pending_requests
        if not _request_fulfilled(ledger, project, r, repo=repo,
                                  after_seq=proposal_seq(ledger, project, pos.attempt_id, r),
                                  proposal=proposal_id(ledger, project, pos, r),
                                  job_name=pos.job, task_id=pos.task_id)
    )


def pending_owner_requests(ledger: Ledger, project: str, job: str, *,
                           repo: Path | None = None) -> tuple[dict, ...]:
    """The owner requests `job` is stopped on that are not fulfilled --
    the one computation every resume gate and `gb2 approve-install`
    reads. A request is fulfilled only by an engine run of its own kind
    after it was proposed, with the current manifest (when `repo` is
    given) still holding the change."""
    return outstanding_requests(ledger, project, load_position(ledger, project, job), repo=repo)


# ---------------------------------------------------------------------
# resuming
# ---------------------------------------------------------------------

def resume_job(config: "EngineConfig", ledger: Ledger,
               fakes: "Fakes | Callable[[str], Fakes]", project: str, job: str, *,
               on_progress: "orbit.ProgressCallback | None" = None,
               processor_factory: "orbit.ProcessorFactory | None" = None) -> "FullOutcome":
    """The one entry point that continues a stopped or crashed job:
    `gb2 continue`, `approve-design`, `approve-install` and the
    continuation after `gb2 fix` all come here. Before dispatching it
    finishes a close of this job's attempt left in flight, reconciles an
    opening intent with the attempt it was for, and checks the plan
    against its review (full_orbit.resumed_plan_stop)."""
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


__all__ = [
    "JobPosition", "FixState", "record_position", "load_position", "pending_owner_requests",
    "resume_job", "resume_job_from", "resume_after_fix", "FLOW_BOUNDARIES", "STAGES",
    "fix_opened_position", "fix_opened_attempt", "task_stop_position", "without_release",
    "is_full_job", "stages_spec", "stages_from_spec", "opening_roles", "proposal_id",
    "widen_stage_owns", "outstanding_requests",
]


def hand_off_later_task_requests(ledger: Ledger, project: str, job_name: str,
                                 pos: "JobPosition", plan: object) -> None:
    """Hands every still-outstanding owns-scope request whose path a LATER
    task of `plan` owns to that task (결함 31b): such a request is that
    task's job, never a reason to keep this one stopped. Called both by the
    owner's approval (`gb2 fix --also-own`) and by a plain resume."""
    if pos.task_id is None:
        return
    later = tuple(t for t in (pos.remaining_task_ids or ()) if t != pos.task_id)
    for request in outstanding_requests(ledger, project, pos, repo=None):
        if not is_owns_scope_request(request):
            continue
        owner = owning_later_task(plan, later, str(request["path"]))
        if owner is not None:
            record_request_handoff(ledger, project, job_name, pos.attempt_id or "",
                                   pos.task_id, owner, request)
