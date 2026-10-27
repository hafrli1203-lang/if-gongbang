"""fix()'s own journal for resuming an interrupted fix transition (split
out of engine.application.repair purely to keep that module under the
repo's own 800-line budget -- see its module docstring's "Resuming an
interrupted fix() transition itself" section for the whole story these
helpers implement).

fix() does several non-idempotent things in sequence (close the old
attempt, open a new one, overlay the archive, submit the new
run_station request) before ever handing off to orbit.continue_job. A
crash between any two of those steps used to leave the project stuck;
fix() now journals its own progress through this one transition as a
small sequence of ledger events ("fix.started", "fix.closed_old",
"fix.opening_new", "fix.opening_failed", "fix.opened_new",
"fix.restored", "fix.submitted", "fix.finished"), and
`find_unfinished_fix_cycle` looks for a "fix.started" with no later
"fix.finished" at the very top of fix(), resuming from whichever of
these events already exist. Every name here is private to repair.py
(and only ever called from there) -- this split is a pure line-count
move, not a new public surface."""
from __future__ import annotations

from typing import TYPE_CHECKING

from engine.domain import faults
from engine.domain.records import Event

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


def append_fix_event(ledger: "Ledger", project: str, kind: str, old_attempt_id: str,
                     payload: dict[str, object]) -> int:
    """Append one of this module's own journal events (see module
    docstring) and return its seq -- used by fix.started's own seq to
    bound which later events belong to the same cycle."""
    faults.flow_boundary(f"fixup.{kind}")
    return ledger.append(Event(
        project=project, kind=kind, actor_kind="owner", actor_id="owner",
        attempt_id=old_attempt_id, payload=payload,
    ))


def latest_fix_event_after(ledger: "Ledger", project: str, kind: str,
                           after_seq: int) -> Event | None:
    """The most recent event of this journal `kind` appended strictly
    after `after_seq` (the current cycle's own "fix.started" seq) -- any
    such event belongs to the CURRENT, still-unfinished cycle: a new
    "fix.started" can only ever be appended once the previous cycle's
    own "fix.finished" has been (see find_unfinished_fix_cycle), so
    nothing from an earlier, already-finished cycle can have a seq past
    this one's "fix.started"."""
    matches = [event for seq, event in ledger.events(project, kind) if seq > after_seq]
    return matches[-1] if matches else None


def latest_fix_event_seq_after(ledger: "Ledger", project: str, kind: str,
                               after_seq: int) -> int | None:
    """Same lookup as latest_fix_event_after, but returns the matching
    event's own seq rather than its payload -- used only for
    "fix.opening_new" (see its use in repair.fix()), whose seq itself
    becomes the fix id that already_opened_new_attempt matches against,
    not anything carried in its payload."""
    matches = [seq for seq, event in ledger.events(project, kind) if seq > after_seq]
    return matches[-1] if matches else None


def current_opening_marker_seq(ledger: "Ledger", project: str, started_seq: int) -> int | None:
    """This cycle's own still-valid "fix.opening_new" marker seq, or None
    if there is none yet, or the most recent one was already
    invalidated by a "fix.opening_failed" written after it (see fix()'s
    own call order). That only happens once THIS cycle's own open_job()
    call has itself raised -- conclusive proof it never produced a
    job.opened of its own -- at which point the marker is worthless for
    already_opened_new_attempt's identity check: a fresh marker (and a
    fresh open_job() attempt) must replace it, or an unrelated
    job.opened for the same job_name landing anywhere after the stale
    marker -- however much later, once this project is genuinely CLOSED
    again -- would still wrongly pass the old marker's bound."""
    marker_seq = latest_fix_event_seq_after(ledger, project, "fix.opening_new", started_seq)
    if marker_seq is None:
        return None
    failed_seq = latest_fix_event_seq_after(ledger, project, "fix.opening_failed", started_seq)
    if failed_seq is not None and failed_seq > marker_seq:
        return None
    return marker_seq


def already_opened_new_attempt(ledger: "Ledger", project: str, job_name: str,
                               old_attempt_id: str, fix_id_seq: int) -> str | None:
    """If open_job() already succeeded for THIS fix cycle -- the project
    is OPEN on a fresh attempt of this same job -- but a crash hit before
    "fix.opened_new" could be journaled for it, return that attempt_id so
    fix() can record the missing event and move on instead of calling
    open_job() again. A second open_job() call there would simply raise
    (the project is OPEN, not CLOSED, by then -- open_job only accepts
    CLOSED) rather than silently forking a duplicate attempt, but that
    still leaves the fix cycle permanently stuck; detecting the already-
    open attempt here is what actually lets it resume. Returns None when
    there is nothing to detect (open_job genuinely has not run yet, or
    failed before completing) -- fix() then calls open_job() as usual.

    Matching on job_name ALONE is not enough: this same job_name can be
    reopened by something that is not this fix cycle at all (an owner's
    independent `gb2 change` on the same job_name landing in the gap
    between the crash and this retry, or a stale attempt left over from
    an EARLIER, already-finished fix cycle on the same job) --
    attempts_for_job_name would still list it, and adopting it here
    would silently overlay this fix's archived files onto a job it
    never opened. `fix_id_seq` is the actual identity check, and it must
    be THIS cycle's own "fix.opening_new" marker seq (the fix id fix()
    journals immediately before ever calling open_job() for this cycle),
    never "fix.started"'s seq: a crash can hit BEFORE fix() ever reaches
    its own open_job() call at all, leaving an arbitrarily long real-time
    gap after "fix.started" (and after "fix.closed_old") during which an
    unrelated `gb2 change` on the same job_name can legitimately run and
    complete -- its own job.opened event then also lands after
    "fix.started", so bounding on that seq alone would wrongly adopt it.
    Bounding on "fix.opening_new" instead means a candidate only matches
    once this cycle has actually attempted its own open_job() call, which
    only ever happens right after that marker is journaled (see fix()'s
    own call order) -- nothing legitimate can land a job.opened for this
    job_name between the marker and this cycle's own open_job() call,
    since open_job() is lock-serialized and would simply raise, not
    silently lose the CLOSED->OPEN slot, if something else took it
    first."""
    row = ledger.project(project)
    if row.state != "OPEN" or row.current_attempt_id is None:
        return None
    candidate = row.current_attempt_id
    if candidate == old_attempt_id:
        return None
    opened_seq: int | None = None
    opened_job_name: str | None = None
    for seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == candidate:
            opened_seq = seq
            opened_job_name = str(event.payload.get("job_name"))
    if opened_seq is None or opened_seq <= fix_id_seq:
        return None
    if opened_job_name != job_name:
        return None
    return candidate


def find_unfinished_fix_cycle(ledger: "Ledger",
                              project: str) -> tuple[int, dict[str, object]] | None:
    """(fix.started's own seq, its payload) for the most recent fix()
    transition that started but never reached "fix.finished" -- or None
    if there isn't one (the common case: either no fix() has ever run
    for this project, or the last one finished its own transition
    cleanly, whatever happened to the job afterward). fix() calls this
    FIRST, before find_fix_target -- which an interrupted transition's
    CLOSED-but-not-yet-reopened project state can never satisfy ("not
    OPEN") even though there is very much something to resume."""
    started_events = ledger.events(project, "fix.started")
    if not started_events:
        return None
    started_seq, started_event = started_events[-1]
    finished_events = ledger.events(project, "fix.finished")
    if finished_events and finished_events[-1][0] > started_seq:
        return None
    return started_seq, started_event.payload


__all__ = [
    "append_fix_event", "latest_fix_event_after", "latest_fix_event_seq_after",
    "current_opening_marker_seq", "already_opened_new_attempt", "find_unfinished_fix_cycle",
]
