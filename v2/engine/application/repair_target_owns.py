"""`--also-own`: the owner widening one task's own write prefixes after a
fix finds work the builder is not allowed to touch (실제 사고,
sample-app 결함 26,: task T2 removed a field it owned, but
the only consumer of that field (src/lib/production/copy.ts) was in NO
task's `owns`, so a fix's builder could never actually land the
matching change there -- merge's own per-task narrowing
(engine.application.steps.merge._owner_prefixes) silently drops a
write outside `owns` rather than failing loudly, so the SAME review
finding kept coming back on every `gb2 fix`. plan.json itself stays
frozen evidence (never edited, same reasoning engine.application.
repair_target's own module docstring gives for never re-deriving
find_fix_target's result from a tampered plan) -- the owner's grant is
recorded as its own ledger event instead, which engine.application.
repair._fix_spec folds into the REPLACEMENT attempt's run_station/merge
"owns" (plan.json's own owns, plus every such event's paths, for this
one task only).

Split out of engine.application.repair_target purely to keep that
module under the repo's own 800-line budget; repair_target re-exports
every public name here at its own bottom (see that module's own
docstring/import block) so every existing call site (task_stage_runner,
repair_approval, pipeline_resume_tasks, repair, pipeline_resume,
job_state_requests, tools/gb2lib/ops, the T9D acceptance test) keeps
importing them from engine.application.repair_target unchanged."""
from __future__ import annotations

from typing import TYPE_CHECKING

from engine.domain.policy import owns_within_prefixes
from engine.domain.records import Event
from engine.application.owner_approval import PENDING_KIND_WHITELIST

if TYPE_CHECKING:
    from pathlib import Path
    from engine.domain.plan import Plan
    from engine.infrastructure.db.ledger import Ledger

OWNS_EXTENDED_KIND = "task.owns_extended"

# The `requests` entry kind OWNS_SCOPE_REQUEST_RULE tells a builder to
# use when it needs a path outside its own `owns` (결함 26's own
# mechanism). It is NEVER a kind owner_approval.PENDING_KIND_WHITELIST
# accepts (that whitelist is for install/scripts_change/
# test_config_change, each shaped {"manifest", "change"} -- an "edit"
# request is shaped {"path", "reason"} and is never submitted to any
# processor at all), so it can only ever be resolved the way
# engine.application.repair.add_also_own already resolves it: the
# owner grants the path via --also-own, recorded as this same
# OWNS_EXTENDED_KIND event. 결함 28 (sample-app T4, is
# exactly a pending "edit" request that find_fix_target can never see
# (the attempt never failed -- it is still `active`, simply stopped at
# job_state.STAGE_TASK_OWNER): see engine.application.repair.
# approve_pending_edit and engine.application.job_state's own "edit"
# branch in _request_fulfilled.
EDIT_REQUEST_KIND = "edit"


def is_owns_scope_request(request: dict[str, object]) -> bool:
    """True iff `request` reports a path outside a task's own `owns` the
    OWNS_SCOPE_REQUEST_RULE way -- a `kind="edit"` request, or (결함 31,
    sample-app T4, a request of ANY kind this engine does not
    otherwise recognize (owner_approval.PENDING_KIND_WHITELIST never
    lists it) that still carries a non-empty string "path". A kind the
    whitelist DOES list (install/scripts_change/test_config_change) is
    never treated as an owns-scope report even if it happens to carry a
    "path" key -- it keeps going through its own processor exactly as
    before. engine.application.job_state._request_fulfilled and
    repair_approval.approve_pending_edit both match on this instead of
    the bare `kind == EDIT_REQUEST_KIND` check they used before 결함 31,
    so a builder's own invented kind for the same report is approvable
    the same way. engine.application.pipeline_tasks's own request_filter
    (owning_later_task below) uses it too, to decide whether such a
    report can be handed to a later task instead of stopping the job at
    all."""
    path = request.get("path")
    if not isinstance(path, str) or not path:
        return False
    kind = request.get("kind")
    if kind == EDIT_REQUEST_KIND:
        return True
    return kind not in PENDING_KIND_WHITELIST


# 결함 31 (sample-app T4,: a build task's own owns-scope report
# (is_owns_scope_request above) names a path that may already be listed
# in a LATER task's own `owns` -- the plan already intends that later
# task to touch it, so stopping the current task for the owner (the
# classic 결함 26/28 path) would be asking the owner to approve
# something the plan already settled. HANDOFF_KIND records that this
# engine noticed and routed the report there instead; the later task's
# own builder instructions get a note (handoff_note below) rather than
# the owner ever seeing it, UNLESS no later task's `owns` covers the
# path -- then it is left in `requests` exactly as an "edit" request
# always was (is_owns_scope_request's own fallback keeps it approvable
# via --also-own either way, per 결함 31's own extension of 결함 28's
# fix).
HANDOFF_KIND = "task.request_handed_to_task"

HANDOFF_NOTE_PREFIX = "[앞 작업이 남긴 이 파일 관련 메모]"


def owning_later_task(plan: "Plan", later_task_ids: tuple[str, ...], path: str) -> str | None:
    """The id of the first task in `plan.tasks` whose id is in
    `later_task_ids` and whose own `owns` already covers `path` -- None
    if none does. `later_task_ids` is the caller's own remaining-tasks
    list with the CURRENT task's id already dropped (run_tasks' own
    `remaining_task_ids[1:]`) -- a task cannot hand a report to itself,
    and an already-finished task's `owns` is no longer anyone's to
    route to."""
    for task in plan.tasks:
        if task.id in later_task_ids and owns_within_prefixes((path,), task.owns):
            return task.id
    return None


def record_request_handoff(ledger: "Ledger", project: str, job_name: str, attempt_id: str,
                           from_task_id: str, to_task_id: str, request: dict[str, object]) -> None:
    """Appends the one event recording that `request` (an owns-scope
    report from `from_task_id`'s own attempt) was routed to `to_task_id`
    instead of stopping the job for the owner -- the ledger's own
    permanent record of the handoff (never just the note text folded
    into `to_task_id`'s builder instructions, which a later resume
    cannot read back out of a prompt string)."""
    ledger.append(Event(
        project=project, kind=HANDOFF_KIND, actor_kind="engine", actor_id="engine",
        attempt_id=attempt_id,
        payload={
            "job_name": job_name, "from_task_id": from_task_id, "to_task_id": to_task_id,
            "path": request.get("path"), "reason": request.get("reason"),
            "kind": request.get("kind"),
        },
    ))


def handed_off_requests_for_task(ledger: "Ledger", project: str, job_name: str,
                                 task_id: str) -> list[dict[str, object]]:
    """Every owns-scope report ever handed off (handoff_request_filter's
    own record_request_handoff, HANDOFF_KIND) TO `task_id` of
    `job_name`, oldest first -- read back from the ledger's own
    permanent record, not an in-memory dict scoped to one
    run_tasks() call. a review finding: a handoff recorded while an
    EARLIER task was being resumed (engine.application.pipeline_resume_tasks.
    continue_task_stages, whose own HandoffContext always passes
    `notes=None` -- it has nowhere in-process to carry the note to) used
    to vanish once that resume returned and `task_id` started in a
    brand-new engine.application.pipeline_tasks.run_tasks() call with its own
    fresh, empty dict -- the ledger event was still there the whole
    time, simply never read back. Called every time `task_id`'s own
    builder instructions are (re)built (first run or any later resume
    of `task_id` itself), the same re-derive-from-the-ledger pattern
    extended_owns_for_task/declined_for_attempt already use, so it is
    idempotent to call more than once and safe to call instead of
    (never in addition to) the in-memory dict."""
    out: list[dict[str, object]] = []
    for _seq, event in ledger.events(project, HANDOFF_KIND):
        if event.payload.get("job_name") == job_name and event.payload.get("to_task_id") == task_id:
            out.append({
                "path": event.payload.get("path"),
                "reason": event.payload.get("reason"),
                "kind": event.payload.get("kind"),
            })
    return out


def handoff_note_text(requests: list[dict[str, object]]) -> str:
    """One `HANDOFF_NOTE_PREFIX` line per handed-off request, each
    naming the path and the reason the earlier task gave -- appended to
    a later task's own builder instructions (engine.application.
    pipeline_tasks.run_tasks) so the builder knows why, without the
    owner ever having to approve anything for it."""
    lines = [
        f"{HANDOFF_NOTE_PREFIX} {request.get('path')}: {request.get('reason')}"
        for request in requests
    ]
    return "\n".join(lines)


# Paths reserved for the engine's own bookkeeping records (every
# station's own record under .gongbang/<job>/ -- facts.md, plan.json,
# report.md, design/, screens/) and tests/scenes/ (reserved for the
# planner alone, engine.application.steps.merge._PLANNER_ONLY_PREFIX) --
# an owner's --also-own grant can never widen a task into either, the
# same config-independent floor merge.py itself enforces for a worker's
# own writes.
ENGINE_RECORD_PREFIXES = (".gongbang/", "tests/scenes/")


def _is_engine_record_path(rel: str) -> bool:
    normalized = rel.rstrip("/")
    for prefix in ENGINE_RECORD_PREFIXES:
        base = prefix.rstrip("/")
        if normalized == base or normalized.startswith(prefix):
            return True
    return False


def _also_own_syntax_violation(raw: str) -> str | None:
    """None iff `raw` is a plain repo-relative path with no escape --
    never an absolute path, a ".." component, or any hidden (dot-
    prefixed) path component (which would also always land under
    .gongbang/ or some other dotfile no station record table lists by
    name). Checked before _is_engine_record_path/the other-task-owns
    check in validate_also_own, so a syntactically unsafe path is
    always refused for this reason regardless of what else is true
    about it."""
    from pathlib import Path

    if not raw:
        return "also_own_empty_path"
    if raw.startswith("/") or Path(raw).is_absolute():
        return "also_own_absolute"
    parts = Path(raw).parts
    if any(part == ".." for part in parts):
        return "also_own_parent_reference"
    if any(part.startswith(".") for part in parts):
        return "also_own_hidden_component"
    return None


def _owns_overlap(a: str, b: str) -> bool:
    """True iff `a` and `b` are the same path or one is a sub-path of
    the other, under the same trailing-slash directory-vs-exact-file
    convention engine.domain.policy.owns_within_prefixes already uses
    for a station's own write prefixes -- reused here (never
    reimplementing path normalization) to decide whether a new
    --also-own grant collides with ANOTHER task's own `owns`."""
    return owns_within_prefixes((a,), (b,)) or owns_within_prefixes((b,), (a,))


def _other_tasks_owns(ledger: "Ledger", project: str, job_name: str, plan: "Plan",
                      task_id: str, active_task_ids: "tuple[str, ...] | None" = None,
                      ) -> tuple[str, ...]:
    """Every OTHER task's own write prefixes for this job: plan.json's
    own `owns` for every task but `task_id`, plus any EARLIER
    --also-own grant recorded for a different task of the same job --
    the full set a new grant must never collide with."""
    # When the caller knows which tasks have NOT run yet (`active_task_ids`),
    # only those still own their paths: a finished task's file may be
    # touched by a later task with the owner's grant (결함 34, sample-app T4
    # needing one export line in T11's ai-input.ts).
    owns: list[str] = [entry for task in plan.tasks if task.id != task_id
                       and (active_task_ids is None or task.id in active_task_ids)
                       for entry in task.owns]
    # The same release applies to a finished task's EXTRA --also-own grants
    # (결함 38, sample-app T7 needing a test file T5 had been granted).
    for _seq, event in ledger.events(project, OWNS_EXTENDED_KIND):
        granted_to = event.payload.get("task_id")
        if active_task_ids is not None and granted_to not in active_task_ids:
            continue
        if (event.payload.get("job_name") == job_name
                and granted_to not in (None, task_id)):
            raw = event.payload.get("paths")
            if isinstance(raw, list):
                owns.extend(str(p) for p in raw)
    return tuple(owns)


def validate_also_own(ledger: "Ledger", project: str, job_name: str, plan: "Plan",
                      task_id: str, paths: tuple[str, ...],
                      active_task_ids: "tuple[str, ...] | None" = None, *,
                      station_prefixes: "tuple[str, ...] | None" = None) -> tuple[str, ...]:
    """`paths`, unchanged, iff every one of them is acceptable --
    (also refuses, when `station_prefixes` is given, a path outside the
    build station's own write prefixes -- 결함 39: such a grant was
    recorded and the run then died instantly on
    owns_escapes_station_prefixes) --
    raises FixupError on the FIRST one that is not (nothing recorded
    either way; the caller only ever records once every path has
    passed). Refuses:
      - a syntactically unsafe path (_also_own_syntax_violation: an
        absolute path, a ".." component, or a hidden path component);
      - an engine record path (.gongbang/, tests/scenes/);
      - a path that overlaps some OTHER task's own owns (plan.json's,
        or an earlier --also-own grant for a different task)."""
    # Local import, not module-level: engine.application.repair_target
    # (the owner of FixupError) imports THIS module back at its own
    # bottom to re-export every name here -- a module-level import of
    # FixupError up here would form the exact circular import
    # engine.application.pipeline_base's own docstring warns about,
    # depending on which of the two modules happens to be imported
    # first. By call time both modules are always fully loaded.
    from engine.application.repair_target import FixupError

    other_owns = _other_tasks_owns(ledger, project, job_name, plan, task_id, active_task_ids)
    for raw in paths:
        reason = _also_own_syntax_violation(raw)
        if reason is not None:
            raise FixupError(f"{reason}:{raw}")
        if _is_engine_record_path(raw):
            raise FixupError(f"also_own_engine_record:{raw}")
        if station_prefixes is not None and not owns_within_prefixes((raw,), station_prefixes):
            raise FixupError(f"also_own_outside_station_prefixes:{raw}")
        if any(_owns_overlap(raw, other) for other in other_owns):
            raise FixupError(f"also_own_owned_by_other_task:{raw}")
    return tuple(paths)


def extended_owns_for_task(ledger: "Ledger", project: str, job_name: str,
                           task_id: str) -> tuple[str, ...]:
    """Every path ever granted to `task_id` of `job_name` by a past
    --also-own call, oldest first, deduplicated -- empty if none ever
    was. Read back by engine.application.repair._fix_spec (folded into
    the replacement attempt's own run_station/merge "owns") and by
    compose_fix_task's own `also_own` text."""
    paths: list[str] = []
    for _seq, event in ledger.events(project, OWNS_EXTENDED_KIND):
        if event.payload.get("job_name") == job_name and event.payload.get("task_id") == task_id:
            raw = event.payload.get("paths")
            if isinstance(raw, list):
                paths.extend(str(p) for p in raw)
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


def record_owns_extension(ledger: "Ledger", project: str, job_name: str, task_id: str,
                          attempt_id: str, paths: tuple[str, ...]) -> None:
    """Appends the one event that grants `paths` to `task_id` of
    `job_name`, forever -- plan.json itself is never touched (frozen
    evidence)."""
    ledger.append(Event(
        project=project, kind=OWNS_EXTENDED_KIND, actor_kind="owner", actor_id="owner",
        attempt_id=attempt_id, payload={"job_name": job_name, "task_id": task_id,
                                         "paths": list(paths)},
    ))


# 결함 43 (sample-app T8,: an owner's explicit "no" to one
# pending edit request -- the only way out for a request --also-own can
# never grant (e.g. an engine-record path), which otherwise blocked every
# resume forever.
DECLINED_KIND = "task.request_declined"


def record_request_declined(ledger: "Ledger", project: str, job_name: str, task_id: str,
                            attempt_id: str, path: str, note: str = "") -> None:
    """`note` (결함 45): the owner's own direction for the refusal, handed
    to the resumed worker verbatim (declined_for_attempt)."""
    payload = {"job_name": job_name, "task_id": task_id, "path": path}
    if note:
        payload["note"] = note
    ledger.append(Event(
        project=project, kind=DECLINED_KIND, actor_kind="owner", actor_id="owner",
        attempt_id=attempt_id, payload=payload,
    ))


def declined_for_attempt(ledger: "Ledger", project: str, job_name: str, task_id: str,
                         attempt_id: str) -> tuple[tuple[str, str], ...]:
    """(path, owner note) for every edit request the owner declined on
    this task's attempt, oldest first -- EXCEPT a path the owner later
    re-requested and GRANTED instead (--also-own / approve_pending_edit,
    OWNS_EXTENDED_KIND, record_owns_extension), after that same decline.
    a review finding: without this, rerun_task_owner_stop's own
    declined_followup_note(declined) + edit_approved_followup_task_text
    folded BOTH "고치지 않는다" and "직접 고친다" for the same path into
    the same resumed task text, whichever decision was really the
    owner's latest one. The latest grant for a path always wins over
    an earlier decline of it -- a grant can never itself be declined
    after the fact (there is no "undo a grant" entry point), so only
    this one direction needs checking."""
    declines = [
        (seq, str(event.payload.get("path", "")), str(event.payload.get("note", "")))
        for seq, event in ledger.events(project, DECLINED_KIND)
        if event.attempt_id == attempt_id and event.payload.get("job_name") == job_name
        and event.payload.get("task_id") == task_id
    ]
    latest_grant_seq: dict[str, int] = {}
    for seq, event in ledger.events(project, OWNS_EXTENDED_KIND):
        if event.payload.get("job_name") == job_name and event.payload.get("task_id") == task_id:
            raw = event.payload.get("paths")
            if isinstance(raw, list):
                for p in raw:
                    latest_grant_seq[str(p)] = seq
    return tuple(
        (path, note) for seq, path, note in declines
        if latest_grant_seq.get(path, -1) < seq
    )


def request_declined(ledger: "Ledger", project: str, job_name: str, task_id: str, path: str,
                     after_seq: int) -> bool:
    """True iff the owner declined `path` for this task after `after_seq`."""
    return any(
        seq > after_seq and event.payload.get("job_name") == job_name
        and event.payload.get("task_id") == task_id and event.payload.get("path") == path
        for seq, event in ledger.events(project, DECLINED_KIND)
    )


__all__ = [
    "OWNS_EXTENDED_KIND", "EDIT_REQUEST_KIND", "is_owns_scope_request", "HANDOFF_KIND",
    "HANDOFF_NOTE_PREFIX", "owning_later_task", "record_request_handoff",
    "handed_off_requests_for_task", "handoff_note_text", "ENGINE_RECORD_PREFIXES",
    "validate_also_own", "extended_owns_for_task", "record_owns_extension", "DECLINED_KIND",
    "record_request_declined", "declined_for_attempt", "request_declined",
]
