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
from engine.domain.records import Event, RequestRow

if TYPE_CHECKING:
    from engine.config import EngineConfig
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

def fix_rounds_used(ledger: Ledger, project: str, job_name: str,
                    exclude_attempt_id: str) -> int:
    """How many times this job has already been through `gb2 fix` for a
    failed review or bench request, since its last commit -- a pure read,
    never a write. Walks the SAME earlier-attempts window
    _latest_merged_archive does (_earlier_attempts_since_last_commit,
    결함 37: never past a committed attempt) and counts an attempt
    toward the total iff it itself closed on a failed review or bench
    request (a run_station/merge crash that find_fix_target's own case
    (b) carries forward is not, on its own, a round spent on review/
    bench feedback -- it is the SAME round resuming after a crash, not
    an extra one). Used by engine.application.repair.fix to enforce
    JobPosition.max_fix_rounds (운영자 기준 "최대 2바퀴", `IF_공방_시스템_v2.md`)
    BEFORE it ever opens a new replacement attempt -- see the module's
    own 결함 41, plans/-gongbang-entry-unify.md
    §B) for the real incident this exists for (sample-app T7 재시도 5바퀴
    이상)."""
    count = 0
    for attempt in _earlier_attempts_since_last_commit(ledger, project, job_name,
                                                        exclude_attempt_id):
        if any(r.kind in ("review", "bench") and r.status == "failed"
               for r in requests_for_attempt(ledger, project, attempt.attempt_id)):
            count += 1
    return count


def review_findings_for_attempt(ledger: Ledger, project: str,
                                attempt_id: str) -> list[object]:
    """This attempt's own newest "review" evidence findings (plan
    review or build-task review alike -- evidence.recorded's own
    "review" kind carries the same {"findings": [...]} shape either
    way), or [] if it never recorded one. The same read
    _carried_findings_and_bench_tail already does inline for ITS OWN
    purpose (crash-recovery carry-forward), exposed on its own for
    review_findings_repeat's two callers (engine.application.repair.fix,
    engine.application.pipeline_resume's plan-review early-station
    reopen -- a past incident, 반장 지시."""
    review_evidence = evidence.latest(ledger, attempt_id, "review")
    if review_evidence is None:
        return []
    findings = review_evidence.payload.get("findings")
    return findings if isinstance(findings, list) else []


def previous_review_findings(ledger: Ledger, project: str, job_name: str,
                             exclude_attempt_id: str) -> list[object]:
    """The most recent EARLIER attempt's own review findings, in the
    SAME since-last-commit window fix_rounds_used/_latest_merged_
    archive use -- the round just before `exclude_attempt_id`'s own, to
    compare against via review_findings_repeat. A thin public name for
    the first element of _carried_findings_and_bench_tail's own return
    (that function serves a different purpose there -- crash recovery,
    not no-progress detection -- but the read is identical)."""
    findings, _bench_tail = _carried_findings_and_bench_tail(ledger, project, job_name,
                                                              exclude_attempt_id)
    return findings


def findings_key(findings: list[object]) -> frozenset[tuple[object, object, object]]:
    """Normalizes a reviewer's findings list (each item a dict with
    file/severity/note -- review evidence's own shape, repair_target.
    find_fix_target's own review_findings) into a comparable set of
    (file, severity, note) triples -- deliberately never `line` (반장
    정의,: "같은 파일+같은 severity+note가 같은 것"), since the
    same unresolved point can shift lines between rounds while still
    being the identical finding a reviewer keeps raising."""
    out: set[tuple[object, object, object]] = set()
    for item in findings:
        if isinstance(item, dict):
            out.add((item.get("file"), item.get("severity"), item.get("note")))
    return frozenset(out)


def review_findings_repeat(current: list[object], previous: list[object]) -> bool:
    """True iff `current` and `previous` are both non-empty and name
    EXACTLY the same findings (findings_key equal) -- the no-progress
    signal a past incident asks for: a review (build-task or plan, 반장 지시
    "검토(구현 검토·계획 검토 모두)") that comes back with the
    identical file+severity+note it already raised last round, meaning
    the round spent in between changed nothing the reviewer cared
    about. Never true for two empty lists -- a round with no findings
    at all is not "the same finding repeated", it is something else
    (e.g. a crash) this function has no opinion on."""
    if not current or not previous:
        return False
    return findings_key(current) == findings_key(previous)


def job_cost_usd(ledger: Ledger, project: str, job_name: str) -> float:
    """This job's cumulative cost so far: every one of its attempts'
    own station.finished cost_usd, summed -- the same formula
    engine.interface.board_data._derive_job uses for its own cost_usd
    column (board_data.py:242-244), duplicated here rather than
    imported because that function is private and board-shaped (it
    also needs request_status/repo/ledger_path just to build a whole
    JobBoard row) -- a flow-layer needs_owner stop reporting "money
    already spent so far" has no business importing the board layer."""
    total = 0.0
    attempt_ids = {a.attempt_id for a in ledger.attempts_for_job_name(project, job_name)}
    for _seq, event in ledger.events(project, "station.finished"):
        if event.attempt_id in attempt_ids:
            cost = event.payload.get("cost_usd")
            if isinstance(cost, (int, float)):
                total += cost
    return round(total, 4)


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
    # fix()'s own replacement run_station always resubmits as station
    # "build" (engine.application.repair's station_prefixes lookup hardcodes
    # it) -- this is only ever correct for an attempt whose ORIGINAL
    # run_station was itself a "build" task. A facts/probe/plan/design
    # station's attempt (e.g. `gb2 change --small`'s own combined
    # "plan" station, run_station payload {"station": "plan", "role":
    # "planner"}) that stopped at a failed review must never be treated
    # as fixable here (결함 50) -- find_fix_target used to read its role
    # back as builder_role regardless of station, and the "fix" attempt
    # that followed hit station_role_not_authorized (station "build"
    # has no role "planner") then merge's unknown_owner, leaving the
    # attempt stuck active with no way forward. The owner's real path
    # for an early-station stop is `gb2 continue` (engine.application.
    # job_state.resume_job dispatches by the job's own position stage,
    # e.g. STAGE_SMALL_PLAN -> engine.application.quick_pipeline.resume_small_plan),
    # never `gb2 fix`.
    run_station_station = str(run_station.payload.get("station") or "")
    if run_station_station != "build":
        raise FixupError(
            "no_failed_attempt: current attempt's run_station was station "
            f"{run_station_station!r}, not a build task -- use `gb2 continue` instead"
        )
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

def inject_context_into_fix_task(config: "EngineConfig", ledger: "Ledger", project: str,
                                 job_name: str, task: str, full_job: bool) -> str:
    """결함 48 (appX live #49/#50): compose_fix_task's own `task` is
    plain text -- a fix round's own builder instructions never got a
    "## 주입된 자료" block (company library/lesson/decision items,
    station skills) at all, unlike a plain first-run build task
    (engine.application.pipeline_tasks.run_tasks' own build_task_gate +
    .prompt() call). Appends the SAME kind of block here, the same
    way, for a full job only (one with a real plan.json to load) --
    a plain single-task `gb2 run` job has no Plan object for
    build_task_gate's own planned_skills()/review_query to work from,
    and skipping context there is a no-op, never a crash; likewise if
    plan.json cannot be loaded at all. Deferred import of
    engine.application.pipeline_tasks (never a module-level one): that
    module already imports THIS one (handed_off_requests_for_task,
    handoff_note_text), so a module-level import the other way would
    be a real cycle."""
    if not full_job:
        return task
    from engine.application.pipeline_tasks import build_task_gate
    from engine.domain.plan import PlanError, load_plan
    try:
        plan = load_plan(config.projects[project].repo, job_name)
    except PlanError:
        return task
    return build_task_gate(config, ledger, project, plan, task).prompt(task)


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


# Re-exported from repair_target_owns (split out to keep this module
# under the 800-line architecture cap -- see that module's own
# docstring). Imported here at the BOTTOM, after FixupError already
# exists on this module's own namespace: repair_target_owns.
# validate_also_own imports FixupError from here lazily, inside its own
# function body, for exactly the same reason -- see its own comment.
from engine.application.repair_target_owns import (  # noqa: E402
    DECLINED_KIND, EDIT_REQUEST_KIND, ENGINE_RECORD_PREFIXES, HANDOFF_KIND,
    HANDOFF_NOTE_PREFIX, OWNS_EXTENDED_KIND, declined_for_attempt, extended_owns_for_task,
    handed_off_requests_for_task, handoff_note_text, is_owns_scope_request, owning_later_task,
    record_owns_extension, record_request_declined, record_request_handoff, request_declined,
    validate_also_own,
)

__all__ = [
    "DECLINED_KIND", "record_request_declined", "request_declined", "declined_for_attempt",
    "FixupError", "FixTarget", "find_fix_target", "compose_fix_task", "requests_for_attempt",
    "ARCHIVE_MARKER_FILES", "OWNS_EXTENDED_KIND", "EDIT_REQUEST_KIND", "ENGINE_RECORD_PREFIXES",
    "validate_also_own", "extended_owns_for_task", "record_owns_extension",
    "is_owns_scope_request", "owning_later_task", "record_request_handoff", "handoff_note_text",
    "handed_off_requests_for_task", "inject_context_into_fix_task",
    "HANDOFF_KIND", "HANDOFF_NOTE_PREFIX", "fix_rounds_used",
    "review_findings_for_attempt", "previous_review_findings", "findings_key",
    "review_findings_repeat", "job_cost_usd",
]
