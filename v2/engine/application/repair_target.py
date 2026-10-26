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
from engine.domain.records import RequestRow

if TYPE_CHECKING:
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


def _latest_merged_archive(ledger: Ledger, project: str, job_name: str,
                           exclude_attempt_id: str) -> Path | None:
    """The most recent EARLIER attempt of this job (by n, descending,
    never the attempt being excluded) whose own archive actually holds
    merged work -- walking back past any number of attempts that each
    crashed before merge ran (empty archives), not just the immediately
    preceding one, so a run of several fix-after-fix crashes in a row
    still finds the last real archive instead of giving up after one
    hop. None if this job has no such attempt (a fresh job's first
    attempt, or a job where every closed attempt's archive was empty)."""
    for attempt in reversed(ledger.attempts_for_job_name(project, job_name)):
        if attempt.attempt_id == exclude_attempt_id:
            continue
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
    past attempt_id instead of the current one."""
    for attempt in reversed(ledger.attempts_for_job_name(project, job_name)):
        if attempt.attempt_id == exclude_attempt_id:
            continue
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

def compose_fix_task(target: FixTarget, extra: str) -> str:
    """"Continue this task. Original request: ... [Review findings to
    address: ...] [Failing test output: ...] [Extra: ...]" -- the
    sections for findings/test output/extra are only included when
    there is actually something to say (a bench-only failure has no
    review findings; an owner who ran `gb2 fix <app>` with no extra
    text has nothing to add there)."""
    parts = [f"Continue this task. Original request: {target.original_task}"]
    if target.review_findings:
        parts.append(
            "Review findings to address: "
            + json.dumps(target.review_findings, ensure_ascii=False)
        )
    if target.bench_tail:
        parts.append(f"Failing test output: {target.bench_tail}")
    if extra:
        parts.append(f"Extra: {extra}")
    return "\n\n".join(parts)


__all__ = [
    "FixupError", "FixTarget", "find_fix_target", "compose_fix_task", "requests_for_attempt",
    "ARCHIVE_MARKER_FILES",
]
