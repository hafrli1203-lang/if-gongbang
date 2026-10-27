"""Finding what engine.application.repair.fix() should repair: the current attempt's
own failed review/bench request (or, for a fix-after-a-failed-fix
retry, an earlier attempt's own carried-forward archive), composed into
one FixTarget -- never the fix transition itself (open/close/overlay/
journal), which stays in engine.application.repair."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from engine.infrastructure import evidence
from engine.domain.policy import owns_within_prefixes
from engine.domain.records import Event, RequestRow
from engine.application.owner_approval import PENDING_KIND_WHITELIST

if TYPE_CHECKING:
    from engine.domain.plan import Plan
    from engine.infrastructure.db.ledger import Ledger


class FixupError(ValueError):
    """Nothing eligible to fix (no current OPEN attempt, no failed
    review/bench request for it, merge never completed, ...) or the
    close that must precede the new attempt did not finish 'done'.
    Callers (tools/gb2's `fix` subcommand) catch this and report a
    refusal instead of letting it surface as an unhandled exception --
    none of these conditions write anything to the ledger."""



@dataclass(frozen=True)
class FixTarget:
    attempt_id: str
    job_id: str
    job_name: str
    builder_role: str
    reviewer_role: str
    original_task: str
    failing_kind: str  # "review" | "bench"
    failing_reason: str | None
    review_findings: list[object]
    bench_tail: str | None
    # Set only when THIS attempt's own merge never completed (it failed at
    # run_station or at merge itself -- a fix-after-a-failed-fix retry,
    # see find_fix_target's docstring): the most recent EARLIER attempt of
    # the same job whose archive actually holds merged work. fix() must
    # overlay THIS directory into the new builder copy instead of
    # close_failed_attempt()'s own return value, which would be an empty
    # archive (nothing was ever merged into the real repo for an attempt
    # that crashed before merge ran).
    carried_archive: Path | None = None

# ---------------------------------------------------------------------
# find_fix_target
# ---------------------------------------------------------------------

def _job_name_for_attempt(ledger: Ledger, project: str, attempt_id: str) -> str:
    """job_name recorded on this attempt's own job.opened event. Not
    reused from engine.application.steps.open_job (its equivalent helper is
    private) or engine.interface.cli (ditto) -- duplicated here deliberately,
    same reasoning open_job.py's own _has_unknown_request docstring
    gives for not depending on another module's internals."""
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            return str(event.payload["job_name"])
    raise FixupError(f"unknown attempt_id: {attempt_id!r}")

def requests_for_attempt(ledger: Ledger, project: str,
                          attempt_id: str) -> list[RequestRow]:
    """Every request ever submitted for this attempt, oldest first.
    Ledger has no direct "requests for an attempt" query, so this reads
    request.submitted events (which carry attempt_id) and looks each
    one up -- the same pattern engine.application.steps.open_job._has_unknown_request
    uses for request.submitted/project-wide."""
    request_ids: list[str] = []
    for _seq, event in ledger.events(project, "request.submitted"):
        if event.attempt_id == attempt_id:
            rid = event.payload.get("request_id")
            if rid is not None:
                request_ids.append(str(rid))
    requests = [ledger.request(rid) for rid in request_ids]
    requests.sort(key=lambda r: r.submitted_seq)
    return requests


# archive_tmp (engine/application/steps/close.py) writes these two marker files
# alongside the archived paths themselves -- never part of the real
# diff. Defined here (rather than only where it was originally used,
# further down in this module) because _archive_has_content below also
# needs it, and that helper is used by find_fix_target.
ARCHIVE_MARKER_FILES = {".deleted", ".modes"}

def _archive_dir_for_attempt(ledger: Ledger, project: str, attempt_id: str) -> Path | None:
    """The archive directory close already created for this attempt (its
    "archive.done" event), or None if it was never closed yet."""
    path: Path | None = None
    for _seq, event in ledger.events(project, "archive.done"):
        if event.attempt_id == attempt_id:
            path = Path(str(event.payload["path"]))
    return path


def _archive_has_content(archive_dir: Path) -> bool:
    """True iff `archive_dir` actually holds a change: a real archived
    file (anything other than the two marker files), or a non-empty
    ".deleted" list. An attempt that failed before merge ever ran closes
    with an archive directory that exists (close_failed_attempt/close
    always creates one) but has nothing in it beyond ".modes" (written
    unconditionally, even when empty) -- that archive must never be
    mistaken for "merged work to continue from"."""
    if not archive_dir.exists():
        return False
    deleted_file = archive_dir / ".deleted"
    if deleted_file.exists() and deleted_file.read_text(encoding="utf-8").strip():
        return True
    for path in archive_dir.rglob("*"):
        if path.is_file() and str(path.relative_to(archive_dir)) not in ARCHIVE_MARKER_FILES:
            return True
    return False


def _earlier_attempts_since_last_commit(ledger: Ledger, project: str, job_name: str,
                                        exclude_attempt_id: str) -> list[object]:
    """Earlier attempts of this job, newest first, STOPPING at (and
    excluding) the first one whose own commit request finished. A
    committed attempt's work is already in HEAD, so anything older is
    stale -- 결함 37 (sample-app T7,: fixing T7's first attempt
    walked back past T5's committed attempt and overlaid T5's pre-commit
    flow.ts onto T7's builder copy."""
    earlier: list[object] = []
    for attempt in reversed(ledger.attempts_for_job_name(project, job_name)):
        if attempt.attempt_id == exclude_attempt_id:
            continue
        if any(r.kind == "commit" and r.status == "done"
               for r in requests_for_attempt(ledger, project, attempt.attempt_id)):
            break
        earlier.append(attempt)
    return earlier


def _latest_merged_archive(ledger: Ledger, project: str, job_name: str,
                           exclude_attempt_id: str) -> Path | None:
    """The most recent EARLIER attempt of this job (by n, descending,
    never the attempt being excluded) whose own archive actually holds
    merged work -- walking back past any number of attempts that each
    crashed before merge ran (empty archives), not just the immediately
    preceding one, so a run of several fix-after-fix crashes in a row
    still finds the last real archive instead of giving up after one
    hop. None if this job has no such attempt (a fresh job's first
    attempt, or a job where every closed attempt's archive was empty).
    Never walks past a committed attempt (결함 37)."""
    for attempt in _earlier_attempts_since_last_commit(ledger, project, job_name,
                                                       exclude_attempt_id):
        archive_dir = _archive_dir_for_attempt(ledger, project, attempt.attempt_id)
        if archive_dir is not None and _archive_has_content(archive_dir):
            return archive_dir
    return None

def _carried_findings_and_bench_tail(ledger: Ledger, project: str, job_name: str,
                                     exclude_attempt_id: str,
                                     ) -> tuple[list[object], str | None]:
    """The most recent EARLIER attempt of this job (excluding
    `exclude_attempt_id`) that itself recorded review findings or a
    failed bench's run_tests tail -- used only when THIS attempt (the
    one being fixed) crashed before producing either of its own (see
    find_fix_target case (b)). Returns ([], None) if no earlier attempt
    has either. Mirrors find_fix_target's own extraction, applied to a
    past attempt_id instead of the current one. Never walks past a
    committed attempt (결함 37: another task's findings are not ours)."""
    for attempt in _earlier_attempts_since_last_commit(ledger, project, job_name,
                                                       exclude_attempt_id):
        review_findings: list[object] = []
        review_evidence = evidence.latest(ledger, attempt.attempt_id, "review")
        if review_evidence is not None:
            findings = review_evidence.payload.get("findings")
            if isinstance(findings, list):
                review_findings = findings
        bench_tail: str | None = None
        bench_failed = next(
            (r for r in requests_for_attempt(ledger, project, attempt.attempt_id)
             if r.kind == "bench" and r.status == "failed"),
            None,
        )
        if bench_failed is not None:
            run_tests_step = ledger.steps(bench_failed.request_id).get("run_tests")
            if run_tests_step is not None:
                tail = run_tests_step.payload.get("tail")
                if isinstance(tail, str):
                    bench_tail = tail
        if review_findings or bench_tail is not None:
            return review_findings, bench_tail
    return [], None

def find_fix_target(ledger: Ledger, project: str) -> FixTarget:
    """Raises FixupError unless the project is OPEN with a current
    attempt that is 'active' AND one of:
      (a) a 'failed' review or bench request AND a 'done' merge request
          for THIS attempt (merge already landed the builder's work in
          the repo's working tree before the job stopped) -- the
          original, single-attempt case; or
      (b) THIS attempt itself failed at run_station or at merge (so its
          own merge never completed -- nothing of ITS OWN was ever
          landed in the repo), but an EARLIER attempt of the same job
          already has an archived, non-empty merge (see
          _latest_merged_archive). This is a fix-after-a-failed-fix
          retry: the previous fix's close already archived its merged
          work (close_failed_attempt's own docstring), this new attempt
          was seeded from it, and it simply crashed again before ever
          re-merging anything new into the real repo. Refusing here
          (the module's ORIGINAL behavior, before this case existed)
          would permanently orphan that earlier archive -- the next fix
          would see a clean repo with nothing to continue from and
          refuse forever, even though the archived work is still sitting
          there, never discarded.
    Case (b) carries the earlier attempt's archive directory forward on
    FixTarget.carried_archive, and falls back to the most recent earlier
    attempt's own review findings / bench tail when THIS attempt crashed
    before producing its own (see _carried_findings_and_bench_tail)."""
    row = ledger.project(project)
    if row.state != "OPEN":
        raise FixupError(f"no_failed_attempt: project state is {row.state!r}, not OPEN")
    attempt_id = row.current_attempt_id
    if attempt_id is None:
        raise FixupError("no_failed_attempt: project has no current attempt")
    attempt = ledger.attempt(attempt_id)
    if attempt.status != "active":
        raise FixupError(
            f"no_failed_attempt: current attempt status is {attempt.status!r}, not active"
        )

    job_name = _job_name_for_attempt(ledger, project, attempt_id)
    requests = requests_for_attempt(ledger, project, attempt_id)

    failing: RequestRow | None = None
    for request in requests:
        if request.kind in ("review", "bench") and request.status == "failed":
            failing = request
    merge_done = any(r.kind == "merge" and r.status == "done" for r in requests)

    carried_archive: Path | None = None
    if failing is None or not merge_done:
        # Neither half of case (a) holds. The only other way this
        # attempt can be fixable is case (b): walk the job's earlier
        # attempts (never just the immediately-preceding one -- a run of
        # several crashes in a row must still find the last REAL
        # archive) for the most recent one whose archive actually holds
        # merged work.
        carried_archive = _latest_merged_archive(ledger, project, job_name, attempt_id)
        if carried_archive is None:
            if failing is None:
                raise FixupError(
                    "no_failed_attempt: no failed review/bench request for the current attempt"
                )
            raise FixupError(
                "no_failed_attempt: merge never completed for the current attempt -- "
                "nothing to continue from"
            )
        if failing is None:
            failing = next(
                (r for r in requests
                 if r.kind in ("run_station", "merge") and r.status == "failed"),
                None,
            )
            if failing is None:
                raise FixupError(
                    "no_failed_attempt: no failed review/bench request for the current attempt"
                )

    run_station = next((r for r in requests if r.kind == "run_station"), None)
    if run_station is None:
        raise FixupError("no_failed_attempt: no run_station request for the current attempt")
    builder_role = str(run_station.payload.get("role") or "builder")
    original_task = str(run_station.payload.get("task") or "")

    review_request = next((r for r in requests if r.kind == "review"), None)
    reviewer_role = (
        str(review_request.payload.get("role") or "reviewer")
        if review_request is not None else "reviewer"
    )

    review_findings: list[object] = []
    review_evidence = evidence.latest(ledger, attempt_id, "review")
    if review_evidence is not None:
        findings = review_evidence.payload.get("findings")
        if isinstance(findings, list):
            review_findings = findings

    # The SELECTED failing request's own run_tests journal -- never
    # just "the first bench request for this attempt" (next(...) over
    # `requests`, oldest first): after a retry, an EARLIER bench
    # request can be a stale pass (or an older, already-fixed failure)
    # while `failing` is the one that actually stopped THIS attempt.
    # Showing the oldest bench's tail instead of the real failure's
    # own output would hand the builder obsolete or misleading context
    # to "fix" against.
    bench_tail: str | None = None
    if failing.kind == "bench":
        run_tests_step = ledger.steps(failing.request_id).get("run_tests")
        if run_tests_step is not None:
            tail = run_tests_step.payload.get("tail")
            if isinstance(tail, str):
                bench_tail = tail

    if carried_archive is not None and not review_findings and bench_tail is None:
        # THIS attempt crashed before ever producing review findings or a
        # bench tail of its own (it failed at run_station/merge, before
        # review/bench even ran again) -- fall back to the most recent
        # EARLIER attempt of this job that has either, so the builder's
        # composed task never silently drops feedback the owner already
        # saw once just because the retry in between crashed.
        review_findings, bench_tail = _carried_findings_and_bench_tail(
            ledger, project, job_name, attempt_id,
        )

    failing_result = failing.result if isinstance(failing.result, dict) else {}
    failing_reason = failing_result.get("reason")

    return FixTarget(
        attempt_id=attempt_id, job_id=attempt.job_id, job_name=job_name,
        builder_role=builder_role, reviewer_role=reviewer_role,
        original_task=original_task, failing_kind=failing.kind,
        failing_reason=(str(failing_reason) if failing_reason is not None else None),
        review_findings=review_findings, bench_tail=bench_tail,
        carried_archive=carried_archive,
    )

# ---------------------------------------------------------------------
# compose the builder's task text
# ---------------------------------------------------------------------

def compose_fix_task(target: FixTarget, extra: str, also_own: tuple[str, ...] = ()) -> str:
    """"Continue this task. Original request: ... [Review findings to
    address: ...] [Failing test output: ...] [Paths the owner added to
    this task: ...] [Extra: ...]" -- the sections for findings/test
    output/also_own/extra are only included when there is actually
    something to say (a bench-only failure has no review findings; an
    owner who ran `gb2 fix <app>` with no extra text has nothing to add
    there; `also_own` is only non-empty once an owner has run `gb2 fix
    <app> --also-own <path>` for this task -- see add_also_own).
    `also_own` never widens anything on its own -- it only tells the
    builder, in plain text, what the ledger-recorded grant
    (record_owns_extension) and _fix_spec's own widened run_station/
    merge "owns" already allow."""
    parts = [f"Continue this task. Original request: {target.original_task}"]
    if target.review_findings:
        parts.append(
            "Review findings to address: "
            + json.dumps(target.review_findings, ensure_ascii=False)
        )
    if target.bench_tail:
        parts.append(f"Failing test output: {target.bench_tail}")
    if also_own:
        parts.append("Paths the owner added to this task: " + ", ".join(also_own))
    if extra:
        parts.append(f"Extra: {extra}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------
# --also-own: the owner widening one task's own write prefixes after a
# fix finds work the builder is not allowed to touch (실제 사고,
# sample-app 결함 26,: task T2 removed a field it owned, but
# the only consumer of that field (src/lib/production/copy.ts) was in
# NO task's `owns`, so a fix's builder could never actually land the
# matching change there -- merge's own per-task narrowing
# (engine.application.steps.merge._owner_prefixes) silently drops a
# write outside `owns` rather than failing loudly, so the SAME review
# finding kept coming back on every `gb2 fix`. plan.json itself stays
# frozen evidence (never edited, same reasoning this module's own
# module docstring gives for never re-deriving find_fix_target's result
# from a tampered plan) -- the owner's grant is recorded as its own
# ledger event instead, which engine.application.repair._fix_spec folds
# into the REPLACEMENT attempt's run_station/merge "owns" (plan.json's
# own owns, plus every such event's paths, for this one task only).
# ---------------------------------------------------------------------

OWNS_EXTENDED_KIND = "task.owns_extended"

# The `requests` entry kind OWNS_SCOPE_REQUEST_RULE tells a builder to
# use when it needs a path outside its own `owns` (결함 26's own
# mechanism). It is NEVER a kind owner_approval.PENDING_KIND_WHITELIST
# accepts (that whitelist is for install/scripts_change/
# test_config_change, each shaped {"manifest", "change"} -- an "edit"
# request is shaped {"path", "reason"} and is never submitted to any
# processor at all), so it can only ever be resolved the way
# add_also_own above already resolves it: the owner grants the path via
# --also-own, recorded as this same OWNS_EXTENDED_KIND event. 결함 28
# (sample-app T4, is exactly a pending "edit" request that
# find_fix_target can never see (the attempt never failed -- it is
# still `active`, simply stopped at job_state.STAGE_TASK_OWNER): see
# engine.application.repair.approve_pending_edit and
# engine.application.job_state's own "edit" branch in _request_fulfilled.
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


__all__ = [
    "FixupError", "FixTarget", "find_fix_target", "compose_fix_task", "requests_for_attempt",
    "ARCHIVE_MARKER_FILES", "OWNS_EXTENDED_KIND", "EDIT_REQUEST_KIND", "ENGINE_RECORD_PREFIXES",
    "validate_also_own", "extended_owns_for_task", "record_owns_extension",
    "is_owns_scope_request", "owning_later_task", "record_request_handoff", "handoff_note_text",
    "HANDOFF_KIND", "HANDOFF_NOTE_PREFIX",
]
