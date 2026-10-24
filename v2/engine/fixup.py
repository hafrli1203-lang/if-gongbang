"""Fix/resume: continue a job whose latest attempt failed at review or
bench, instead of starting a fresh run from scratch.

Why this needs its own module rather than just calling open_job() again:
open_job()/_make_copies() always builds a fresh builder copy via `git
archive HEAD` -- it never looks at the project repo's current dirty
working tree. By the time a job has failed at review or bench, the
merge stage has ALREADY landed the builder's file changes into the real
repo as uncommitted changes (orbit.default_stages runs merge right
after run_station, before review/bench ever see the tree). HEAD itself
has not moved (commit only runs after review+bench both pass), so a
plain open_job() for a new attempt would archive HEAD -- the pre-change
tree -- and the merged work would simply be lost the moment the next
step (closing the old attempt) resets the repo back to HEAD.

The fix this module implements:
  1. find_fix_target() reads the ledger for the current (failed) attempt:
     the original run_station task, the reviewer's findings (if the job
     failed at review) and the failing test output's tail (if it failed
     at bench).
  2. close_failed_attempt() closes that attempt NORMALLY (the real
     "close" request, same one `gb2 skip-ship` and a plain successful
     job both use) -- this is what actually preserves the merged work:
     close's own archive_tmp/archive_commit steps copy every changed
     path to config.home/archive/{project}/{attempt}/{token}/ BEFORE
     reset_to_head wipes the working tree back to HEAD. Nothing here
     duplicates that archiving logic; it just locates the archive
     close already made (via the archive.done event) once close is
     done.
  3. open_job() for a NEW attempt on the SAME job (same job_name --
     open_job's own re-entry path bumps n and reuses job_id). Its
     builder copy is freshly archived from HEAD, same as any other new
     attempt -- clean, because close() just reset the repo back to HEAD.
  4. The previous attempt's archived files are copied into this new
     attempt's builder copy (not git-added or committed there -- see
     engine.steps.merge's own module docstring: collect_copies finds
     candidate files via `git status` in the copy, same as any worker
     edit, and compares their BYTES against base_head directly, never
     against the copy's own git history). Since base_head is the same
     sha for both attempts (HEAD never moved), merge's conflict checks
     see exactly what they would for a worker that was never
     interrupted.
  5. A composed task ("Continue this task. Original request: ... Review
     findings to address: ... Failing test output: ... Extra: ...") is
     handed to a fresh run_station request on the new attempt, and the
     job's normal stage list (engine.orbit.fix_stages -- currently
     identical to default_stages) runs the rest of the way.

Every effect here is an existing, already-audited engine entry point
(processor.submit, engine.steps.open_job.open_job, orbit.continue_job)
-- this module adds no new ledger event kind and no new state
transition. The only genuinely new filesystem operation is step 4's
copy from the archive directory into the new builder copy, which never
touches the project repo itself.

Fix-after-a-failed-fix: a fix() attempt can itself fail before its OWN
merge ever completes (the builder crashes at run_station, or merge
itself fails) -- that attempt's own close then archives NOTHING (the
real repo was never touched for it). find_fix_target still accepts this
attempt as fixable as long as an EARLIER attempt of the same job has an
archive that DOES hold merged work (_latest_merged_archive walks back
past any number of empty archives to find it); FixTarget.carried_archive
names that directory, and fix() overlays it instead of the failing
attempt's own (empty) one. See find_fix_target's own docstring.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from engine import evidence, orbit
from engine import release as release_mod
from engine.config import EngineConfig
from engine.ledger import Ledger, RequestRow
from engine.processor import Fakes, Processor, submit
from engine.steps.open_job import copy_root, open_job


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


@dataclass(frozen=True)
class FixResult:
    old_attempt_id: str
    new_attempt_id: str
    job_name: str
    task: str
    outcome: orbit.JobOutcome


FakesFor = Callable[[str], Fakes]


# ---------------------------------------------------------------------
# find_fix_target
# ---------------------------------------------------------------------

def _job_name_for_attempt(ledger: Ledger, project: str, attempt_id: str) -> str:
    """job_name recorded on this attempt's own job.opened event. Not
    reused from engine.steps.open_job (its equivalent helper is
    private) or engine.cli (ditto) -- duplicated here deliberately,
    same reasoning open_job.py's own _has_unknown_request docstring
    gives for not depending on another module's internals."""
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            return str(event.payload["job_name"])
    raise FixupError(f"unknown attempt_id: {attempt_id!r}")


def _requests_for_attempt(ledger: Ledger, project: str,
                          attempt_id: str) -> list[RequestRow]:
    """Every request ever submitted for this attempt, oldest first.
    Ledger has no direct "requests for an attempt" query, so this reads
    request.submitted events (which carry attempt_id) and looks each
    one up -- the same pattern engine.steps.open_job._has_unknown_request
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


# archive_tmp (engine/steps/close.py) writes these two marker files
# alongside the archived paths themselves -- never part of the real
# diff. Defined here (rather than only where it was originally used,
# further down in this module) because _archive_has_content below also
# needs it, and that helper is used by find_fix_target.
_ARCHIVE_MARKER_FILES = {".deleted", ".modes"}


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
        if path.is_file() and str(path.relative_to(archive_dir)) not in _ARCHIVE_MARKER_FILES:
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
            (r for r in _requests_for_attempt(ledger, project, attempt.attempt_id)
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
    requests = _requests_for_attempt(ledger, project, attempt_id)

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


# ---------------------------------------------------------------------
# close the failed attempt, recover its archive
# ---------------------------------------------------------------------

def close_failed_attempt(config: EngineConfig, ledger: Ledger, fakes: Fakes,
                         project: str, attempt_id: str) -> Path:
    """Submit and run the real "close" request for `attempt_id` (the
    same request kind a normal successful job and `gb2 skip-ship` both
    use -- see engine.steps.close's module docstring: archive_tmp/
    archive_commit copy every changed path out BEFORE reset_to_head
    wipes the working tree back to HEAD). Returns the archive directory
    close just created (from the archive.done event it appends), so the
    caller can restore those files into the NEW attempt's builder copy.
    Raises FixupError if the close request does not end 'done' (e.g.
    "changed_after_archive") -- the project is left exactly as close
    left it (still OPEN/CLOSING, never force-advanced) for the owner to
    investigate; no new attempt is opened."""
    proc = Processor(config, ledger, fakes)
    proc.register_defaults()
    close_rid = submit(ledger, project, attempt_id, "close", {}, "owner")
    proc.run_once(project)
    request = ledger.request(close_rid)
    if request.status != "done":
        result = request.result if isinstance(request.result, dict) else {}
        reason = result.get("reason", request.status)
        raise FixupError(f"close_failed: {reason}")

    archive_path: Path | None = None
    for _seq, event in ledger.events(project, "archive.done"):
        if event.attempt_id == attempt_id:
            archive_path = Path(str(event.payload["path"]))
    if archive_path is None:
        raise FixupError("archive_missing: no archive.done event for the closed attempt")
    return archive_path


def _overlay_archive_into_builder_copy(config: EngineConfig, attempt_id: str,
                                       builder_role: str, archive_dir: Path) -> None:
    """Copy every archived file (the previous, failed attempt's own
    merged-but-uncommitted work) into the new attempt's builder copy,
    as plain filesystem writes -- no `git add`/`git commit` inside the
    copy. engine.steps.merge's own collect_copies finds candidate files
    via `git status` in the copy (same call engine.steps.open_job.
    dirty_paths makes for the real repo), so an untracked/modified file
    here is picked up exactly like any worker edit; and it compares
    their BYTES against base_head directly (never against the copy's
    own git history), so no commit inside the copy is needed for merge
    to see these as real changes. Paths close's own archive_tmp
    recorded as deleted (".deleted") are removed from the copy instead
    of being written -- a worker that deleted a file in the previous
    attempt must still see it gone when it resumes."""
    dest_root = copy_root(config, attempt_id, builder_role)

    modes: dict[str, object] = {}
    modes_file = archive_dir / ".modes"
    if modes_file.exists():
        try:
            modes = json.loads(modes_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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


# ---------------------------------------------------------------------
# top-level entry point
# ---------------------------------------------------------------------

def fix(config: EngineConfig, ledger: Ledger, project: str, extra: str,
       fakes_for: FakesFor,
       on_progress: orbit.ProgressCallback | None = None,
       processor_factory: orbit.ProcessorFactory | None = None) -> FixResult:
    """Continue the project's current (failed-at-review-or-bench)
    attempt instead of starting a fresh job. `fakes_for(attempt_id)`
    builds the Fakes a Processor needs for that specific attempt_id
    (mirrors tools/gb2's own `_build_fakes(config, app, attempt_id,
    None)` -- the installer's marker_dir is attempt-scoped, so closing
    the old attempt and running the new one each need their own).
    `processor_factory`, like orbit.continue_job's own parameter of the
    same name, is forwarded only to the NEW attempt's stage list --
    closing the old attempt always uses the real default steps (every
    caller, live or test, needs the real close behavior; nothing stubs
    "close"). Raises FixupError (no ledger write beyond whatever
    close_failed_attempt itself already committed) if there is nothing
    eligible to fix, or if closing the failed attempt does not end
    'done'."""
    target = find_fix_target(ledger, project)

    # Cancel any publish request still queued for the attempt being
    # replaced BEFORE close_failed_attempt below runs processor.run_once
    # (which drains the WHOLE project queue, including that request) --
    # a fix must never let a stale, now-superseded publish go out just
    # because closing the old attempt happened to touch the queue.
    release_mod.cancel_pending_publish(ledger, project, target.attempt_id, "superseded_by_fix")

    old_fakes = fakes_for(target.attempt_id)
    own_archive_dir = close_failed_attempt(config, ledger, old_fakes, project, target.attempt_id)
    # carried_archive (see find_fix_target case (b)) means THIS attempt's
    # own merge never completed, so own_archive_dir is necessarily empty
    # -- the earlier attempt's own archive, found by find_fix_target, is
    # what actually holds merged work to continue from.
    archive_dir = target.carried_archive if target.carried_archive is not None else own_archive_dir

    # Only the builder role needs a fresh copy -- the reviewer runs
    # against its own exported snapshot (engine.steps.review._export_
    # review_snapshot), never a worker copy at all, so making one for
    # it here would just be an unused checkout cost on every fix.
    new_attempt_id = open_job(
        config, ledger, project, target.job_name, "full",
        (target.builder_role,),
    )
    _overlay_archive_into_builder_copy(config, new_attempt_id, target.builder_role, archive_dir)

    task = compose_fix_task(target, extra)
    stages = orbit.fix_stages(
        config, new_attempt_id, target.builder_role, target.reviewer_role,
        task, target.job_name,
    )
    new_fakes = fakes_for(new_attempt_id)
    outcome = orbit.continue_job(
        config, ledger, new_fakes, project, new_attempt_id, stages,
        processor_factory=processor_factory, on_progress=on_progress,
    )
    return FixResult(
        old_attempt_id=target.attempt_id, new_attempt_id=new_attempt_id,
        job_name=target.job_name, task=task, outcome=outcome,
    )


__all__ = [
    "FixupError", "FixTarget", "FixResult", "find_fix_target",
    "compose_fix_task", "close_failed_attempt", "fix", "original_task",
]


def original_task(ledger: Ledger, project: str, attempt_id: str) -> str:
    """The task text the attempt's run_station request was given ("" if
    there is none). Used wherever a later stage list is rebuilt without
    the caller holding the task, so the reviewer still sees the request."""
    for request in _requests_for_attempt(ledger, project, attempt_id):
        if request.kind == "run_station":
            return str(request.payload.get("task") or "")
    return ""
