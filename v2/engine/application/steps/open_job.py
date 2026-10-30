"""Owner input: open a job (synchronous, not a queued request).

open_job():
  1. validate FIRST, before any ledger write: state CLOSED, no queued
     requests, no request with status 'unknown' (the same guards
     state.transition('open_job') enforces, replicated here so a refused
     open never gets as far as appending job.opened — see note below)
  2. job_id = new_job_id(project) unless an existing job with that job_name
     is being re-entered (then n = last n + 1 -> new attempt_id, same job_id)
  3. dirty_paths = paths in the commit-tree range that differ from HEAD
     right now (user's pre-existing changes; close must never reset them)
  4. base_head = `git rev-parse HEAD` of the project repo, recorded on
     job.opened (the sha the worker copies are made from; merge reads it)
  5. append job.open_intent {attempt_id, job_id, job_name, n, copies_dir}
     -- BEFORE any copy is made (see note on (6a) below)
  6. make every role's worker copy (git archive | tar) under a temporary
     staging directory, then atomically rename it into
     config.home/copies/{attempt_id} — all BEFORE job.opened itself (see
     note on (6) below)
  7. generation = project.generation + 1
  8. append job.opened, then state.changed OPEN

Note on (1): state.transition('open_job') enforces these same guards, but
only AFTER whatever the caller already appended. Appending job.opened
before calling transition meant a refused open (queued/unknown requests
pending) still left behind a committed attempt row and advanced
generation, because the guard failure surfaced only from the later
transition() call. Validating first — using only the public Ledger API,
the same way state._has_unknown_request does — closes that gap.

Note on (6): copies used to be made AFTER job.opened/state.changed OPEN,
with no journal of their own. A failure partway through copy creation
(disk full, one role's archive failing after another's succeeded) left
the project OPEN with a committed attempt but broken/partial copies, and
nothing to retry or roll back. Building every role's copy in a `.tmp-`
staging directory first and renaming it into place only once ALL of them
have succeeded means: the ledger is never touched until copies are known
good, and a failure cleans up its own temp directory and raises — no
attempt row, no generation bump, no OPEN state.

Note on (6a): (6)'s own fix closed the "broken/partial copies" case, but
left a NARROWER crash window open: a crash strictly AFTER `_make_copies`'
own atomic rename succeeded (so config.home/copies/{attempt_id} now
holds a complete, good copy on disk) but BEFORE job.opened is appended
leaves that directory on disk with NOTHING in the ledger naming it at
all -- job_id/attempt_id/n were only ever computed in-memory this call,
never journaled before the rename. The NEXT open_job call for the same
job_name sees no trace of this attempt (nothing was ever appended for
it) and simply computes a brand-new job_id/attempt_id, leaving the old
copies/{attempt_id} directory behind forever: no event names it, so
nothing could ever clean it up. Recording job.open_intent first, BEFORE
`_make_copies` runs at all, closes this the same way engine.infrastructure.procs.spawn's
own proc.launch_intent closes its analogous crash window (Popen
succeeding before proc.started is appended): _reconcile_orphaned_intents
(called early in open_job, before this call's own new attempt is even
computed) finds any job.open_intent with no matching job.opened for the
project and removes that intent's own copies_dir (and its `.tmp-` staging
dir, in case the crash landed even earlier, before the rename) if either
is still present -- safe unconditionally, since no job.opened ever
pointed at it, so nothing else in the ledger could possibly still
reference that attempt_id's copies.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain import ids
from engine.application.steps.open_job_sweep import sweep_stale_tmp_dirs
from engine.infrastructure import procs
from engine.domain import state
from engine.infrastructure.gitutil import git_argv
from engine.config import EngineConfig, ProjectConfig
from engine.domain.records import Event, LedgerError

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


def _has_unknown_request(ledger: Ledger, project: str) -> bool:
    """True iff some request submitted for this project currently has
    status 'unknown'. Mirrors state._has_unknown_request (private there);
    duplicated here deliberately so open_job's pre-checks don't depend on
    state.py internals."""
    for _seq, event in ledger.events(project, "request.submitted"):
        rid = event.payload.get("request_id")
        if rid is None:
            continue
        try:
            if ledger.request(str(rid)).status == "unknown":
                return True
        except LedgerError:
            continue
    return False


def _reconcile_pending_open(ledger: Ledger, project: str, job_name: str,
                            actor_id: str) -> str | None:
    """None (nothing to do) unless THIS job_name's own PREVIOUS open_job
    call crashed between appending job.opened and calling
    state.transition (see the module docstring's "Note on (6)" for the
    analogous copies-staging fix -- this closes the other half of the
    same crash-atomicity gap, for the ledger side rather than the
    filesystem side). job.opened's own handler (engine.infrastructure.db.ledger.
    _h_job_opened) sets projects.current_attempt_id/generation and
    inserts the attempt as 'active' in the SAME transaction as the
    event itself; state.changed only updates projects.state, in a
    SEPARATE ledger.append call (there is no cross-event-append
    transaction API in engine.infrastructure.db.ledger -- see this module's docstring). A
    crash in between leaves project.state == 'CLOSED' with a
    current_attempt_id whose own status is still 'active'.

    That combination is NOT, by itself, proof of a crash -- only that
    `job_name` matches the recorded attempt's own job_name narrows it
    to "this exact call's own previous attempt, retried": a bare
    CLOSED + active-current-attempt can also arise from code (test
    fixtures included) that transitions a project to CLOSED without
    ever appending attempt.finished for its current attempt, in which
    case a DIFFERENT job_name being opened next must get a genuinely
    new attempt, same as before this reconciliation existed -- it is
    never this function's job to repair a project left in that shape
    by something else. Rather than let a retried call for THIS job_name
    silently start a brand-new attempt (job_id is random, so it would
    not collide with anything, but the orphaned 'active' attempt row --
    and its already-made worker copies -- would be left behind forever,
    looking like a second, phantom active attempt), this finishes the
    interrupted transition for the SAME attempt and returns its
    attempt_id, mirroring how recover_job's own
    _recover_resume_after_interrupted resumes a crash mid-recovery
    instead of starting over."""
    project_row = ledger.project(project)
    if project_row.state != "CLOSED" or project_row.current_attempt_id is None:
        return None
    attempt_id = project_row.current_attempt_id
    attempt = ledger.attempt(attempt_id)
    if attempt.status != "active":
        return None
    if job_name_for(ledger, project, attempt.job_id) != job_name:
        return None
    state.transition(ledger, project, "open_job", actor_id)
    return attempt_id


def _reconcile_orphaned_intents(ledger: Ledger, project: str, config: EngineConfig) -> None:
    """Clean up config.home/copies/{attempt_id} directories left behind
    by an open_job call that crashed strictly after _make_copies' own
    atomic rename but before job.opened was appended -- see the module
    docstring's "Note on (6a)". For every job.open_intent event for
    this project with no job.opened sharing its own attempt_id, both
    `_copies_dir(config, attempt_id)` (the renamed-into-place result)
    and its own `.tmp-` staging root (in case the crash landed even
    earlier, before the rename itself) are removed if either is still
    present. Always safe: no job.opened ever named that attempt_id, so
    nothing else in the ledger -- no attempt row (job.opened's handler
    is the only thing that ever inserts one), no request, no proc --
    could possibly still reference its copies. Idempotent (a directory
    an earlier call already removed is simply not there), and run
    UNCONDITIONALLY near the top of every open_job call -- including
    one for a completely different job_name -- since an intent's own
    orphan status depends only on whether job.opened for ITS OWN
    attempt_id ever landed, never on which job_name the CURRENT call
    is opening."""
    opened_attempt_ids = {
        event.payload.get("attempt_id")
        for _seq, event in ledger.events(project, "job.opened")
    }
    for _seq, event in ledger.events(project, "job.open_intent"):
        attempt_id = event.payload.get("attempt_id")
        if attempt_id is None or attempt_id in opened_attempt_ids:
            continue
        copies_root = config.home / "copies"
        dirname = ids.attempt_dirname(str(attempt_id))
        for candidate in (copies_root / dirname, copies_root / f".tmp-{dirname}"):
            if candidate.exists():
                shutil.rmtree(candidate, ignore_errors=True)
    sweep_stale_tmp_dirs(ledger, project, config)


def open_job(config: EngineConfig, ledger: Ledger, project: str, job_name: str,
             kind: str, roles: tuple[str, ...], actor_id: str = "owner") -> str:
    """Return the new attempt_id.

    Takes the same project lock a Processor.run_once() holds for the
    whole duration of validating and writing (released in `finally`,
    like Processor.run_once does): without it, an owner's open_job could
    read a consistent project/queue snapshot and then write job.opened
    while a Processor was still mid-request on the SAME project (e.g. a
    slow run_installer), racing the processor's own in-flight read of
    project/attempt state. Refused outright (TransitionError
    "project_busy", no ledger write) if the lock is already held -- by a
    live processor or another concurrent owner call."""
    holder = f"owner-{uuid.uuid4().hex[:8]}"
    if not ledger.acquire_lock(project, holder):
        raise state.TransitionError("open_job refused: project_busy")
    try:
        _reconcile_orphaned_intents(ledger, project, config)
        reconciled = _reconcile_pending_open(ledger, project, job_name, actor_id)
        if reconciled is not None:
            return reconciled
        if ledger.project(project).state != "CLOSED":
            raise state.TransitionError(
                f"required state in ('CLOSED',), got {ledger.project(project).state!r}"
            )
        if ledger.queued_requests(project):
            raise state.TransitionError("open_job refused: queued requests pending")
        if _has_unknown_request(ledger, project):
            raise state.TransitionError("open_job refused: unknown_requests_pending")

        project_config = config.projects[project]
        existing = ledger.attempts_for_job_name(project, job_name)
        if existing:
            job_id = existing[0].job_id
            n = max(attempt.n for attempt in existing) + 1
        else:
            job_id = ids.new_job_id(project)
            n = 1
        new_attempt_id = ids.attempt_id(job_id, n)
        dirty = dirty_paths(project_config.repo, project_config.excludes)
        base_head = subprocess.run(
            git_argv(project_config.repo, "rev-parse", "HEAD"),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Recorded BEFORE _make_copies runs at all (so before its own
        # atomic rename, let alone before job.opened below) -- see the
        # module docstring's "Note on (6a)" for the crash window this
        # closes and _reconcile_orphaned_intents for how a later
        # open_job call cleans up after it if this one never finishes.
        ledger.append(Event(
            project=project, kind="job.open_intent", actor_kind="owner",
            actor_id=actor_id, attempt_id=new_attempt_id,
            payload={"job_id": job_id, "job_name": job_name, "n": n,
                      "attempt_id": new_attempt_id,
                      "copies_dir": str(_copies_dir(config, new_attempt_id))},
        ))
        copy_links_skipped, copy_git_config_sha256 = _make_copies(
            project_config, config, new_attempt_id, roles)
        generation = ledger.project(project).generation + 1
        ledger.append(Event(
            project=project, kind="job.opened", actor_kind="owner", actor_id=actor_id,
            attempt_id=new_attempt_id,
            payload={
                "job_id": job_id, "job_name": job_name, "attempt_id": new_attempt_id,
                "n": n, "kind": kind, "generation": generation,
                "dirty_paths": list(dirty), "base_head": base_head,
                "copy_links_skipped": copy_links_skipped,
                "copy_git_config_sha256": copy_git_config_sha256,
            },
        ))
        state.transition(ledger, project, "open_job", actor_id)
        return new_attempt_id
    finally:
        ledger.release_lock(project, holder)


def job_name_for(ledger: Ledger, project: str, job_id: str) -> str:
    """job_name recorded on the job's original job.opened event. recover_job
    re-enters the same job_id (like open_job's job_name re-entry path) but
    is never given job_name as an input, so it has to be looked up."""
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("job_id") == job_id:
            return str(event.payload["job_name"])
    raise LedgerError(f"unknown job_id: {job_id}")


def _job_opened_payload_for_attempt(ledger: Ledger, project: str,
                                    attempt_id: str) -> dict[str, object]:
    """The job.opened event payload that created this specific attempt_id
    (not just its job_id -- a job can have several attempts, each with its
    own job.opened event)."""
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            return event.payload
    raise LedgerError(f"unknown attempt_id: {attempt_id}")


def _distinct_live_stations(ledger: Ledger, project: str, attempt_id: str) -> set[str]:
    """Station names of every currently-live (proc.ended not yet recorded)
    process of `attempt_id`, read from the matching proc.started events --
    live_procs() only returns (pid, pgid), not which station spawned it.
    recover_job uses this to close every station with a live process
    before marking the old attempt interrupted -- otherwise those
    process groups would be left running forever once recovery moved
    on."""
    live_pids = {pid for pid, _pgid in ledger.live_procs(attempt_id)}
    if not live_pids:
        return set()
    stations: set[str] = set()
    for _seq, event in ledger.events(project, "proc.started"):
        if event.attempt_id == attempt_id and event.payload.get("pid") in live_pids:
            station = event.payload.get("station")
            if station is not None:
                stations.add(str(station))
    return stations


def _close_live_stations(ledger: Ledger, project: str, attempt_id: str) -> None:
    for station in _distinct_live_stations(ledger, project, attempt_id):
        procs.close_station(ledger, project, attempt_id, station)


def _recovery_payload(old_attempt, job_name: str, old_payload: dict[str, object],
                      new_attempt_id: str, generation: int) -> dict[str, object]:
    return {
        "job_id": old_attempt.job_id, "job_name": job_name,
        "attempt_id": new_attempt_id, "n": old_attempt.n + 1, "kind": "recovery",
        "generation": generation, "dirty_paths": list(old_payload.get("dirty_paths", [])),
        "base_head": old_payload.get("base_head"), "recovered_from": old_attempt.attempt_id,
        # No new copies are made for a recovery (ENGINE.md: 사본 없음) --
        # the old attempt's own copies, and their recorded git-config
        # sha256, carry over unchanged so verify_copy_git_config can
        # still check a resumed request against them.
        "copy_git_config_sha256": dict(old_payload.get("copy_git_config_sha256", {})),
    }


def _recover_fresh(ledger: Ledger, project: str, old_attempt, project_row,
                   actor_id: str) -> str:
    """No crash yet seen for this recovery: close every live station of
    the old attempt, mark it interrupted, open the new one, transition.
    Mirrors open_job's own effect ordering (make everything real BEFORE
    the ledger says it happened), so a crash here always leaves
    `_recover_resume_*` something coherent to pick up from."""
    old_attempt_id = old_attempt.attempt_id
    job_name = job_name_for(ledger, project, old_attempt.job_id)
    old_payload = _job_opened_payload_for_attempt(ledger, project, old_attempt_id)
    new_attempt_id = ids.attempt_id(old_attempt.job_id, old_attempt.n + 1)
    generation = project_row.generation + 1

    _close_live_stations(ledger, project, old_attempt_id)
    ledger.append(Event(
        project=project, kind="attempt.finished", actor_kind="owner",
        actor_id=actor_id, attempt_id=old_attempt_id,
        payload={"attempt_id": old_attempt_id, "status": "interrupted"},
    ))
    ledger.append(Event(
        project=project, kind="job.opened", actor_kind="owner", actor_id=actor_id,
        attempt_id=new_attempt_id,
        payload=_recovery_payload(old_attempt, job_name, old_payload,
                                  new_attempt_id, generation),
    ))
    state.transition(ledger, project, "recover_job", actor_id)
    return new_attempt_id


def _recover_resume_after_interrupted(ledger: Ledger, project: str, old_attempt,
                                      project_row, actor_id: str) -> str:
    """Crash hit after attempt.finished{interrupted} but before job.opened
    for the recovery attempt: stations are already closed (that happens
    strictly before the attempt.finished write in _recover_fresh), so
    pick up from job.opened without writing attempt.finished again."""
    old_attempt_id = old_attempt.attempt_id
    job_name = job_name_for(ledger, project, old_attempt.job_id)
    old_payload = _job_opened_payload_for_attempt(ledger, project, old_attempt_id)
    new_attempt_id = ids.attempt_id(old_attempt.job_id, old_attempt.n + 1)
    generation = project_row.generation + 1

    ledger.append(Event(
        project=project, kind="job.opened", actor_kind="owner", actor_id=actor_id,
        attempt_id=new_attempt_id,
        payload=_recovery_payload(old_attempt, job_name, old_payload,
                                  new_attempt_id, generation),
    ))
    state.transition(ledger, project, "recover_job", actor_id)
    return new_attempt_id


def recover_job(config: EngineConfig, ledger: Ledger, project: str,
                actor_id: str = "owner") -> str:
    """Owner-initiated recovery: a NEW attempt on the same job, not a
    re-use of the interrupted one. Returns the new attempt_id (#n+1).

    Crash-safe / resumable: a crash can land between any two
    of recover_job's three effects (station closes + attempt.finished,
    job.opened, state.changed). The guard below accepts being called
    again in either partial state and continues from where it stopped,
    rather than only accepting (state OPEN, current attempt 'active') --
    which a retry after such a crash could never satisfy again:
      - current attempt 'active', kind != "recovery": nothing has
        happened yet -- _recover_fresh does all three effects.
      - current attempt 'active', kind == "recovery": job.opened for
        THIS recovery already landed (its handler always repoints
        current_attempt_id/marks it active the moment that event is
        appended), only state.changed is missing -- finish just that.
      - current attempt 'interrupted': attempt.finished landed but
        job.opened for the recovery didn't -- _recover_resume_after_
        interrupted picks up from job.opened. (If job.opened for a
        newer attempt already existed, _h_job_opened would already have
        repointed current_attempt_id away from this interrupted one, so
        that state is unreachable here.)
    Mirrors open_job's own validate-before-any-write discipline (see
    open_job's docstring note on (1)) for whichever effect is next.

    Takes the same project lock a Processor.run_once() holds for the
    whole duration of validating and writing (released in `finally`,
    like open_job): without it, this could read a consistent attempt
    snapshot and then mark it 'interrupted' while a Processor was still
    mid-request on it (e.g. a slow run_installer), leaving that in-flight
    request to finish its effects under an attempt the ledger already
    calls interrupted. Refused outright (TransitionError "project_busy",
    no ledger write) if the lock is already held.
    """
    holder = f"owner-{uuid.uuid4().hex[:8]}"
    if not ledger.acquire_lock(project, holder):
        raise state.TransitionError("recover_job refused: project_busy")
    try:
        project_row = ledger.project(project)
        if project_row.state != "OPEN":
            raise state.TransitionError(
                f"recover_job refused: required state OPEN, got {project_row.state!r}"
            )
        current_attempt_id = project_row.current_attempt_id
        if current_attempt_id is None:
            raise state.TransitionError("recover_job refused: no current attempt")
        current_attempt = ledger.attempt(current_attempt_id)

        if current_attempt.status == "active" and current_attempt.kind == "recovery":
            state.transition(ledger, project, "recover_job", actor_id)
            return current_attempt_id
        if current_attempt.status == "interrupted":
            return _recover_resume_after_interrupted(
                ledger, project, current_attempt, project_row, actor_id
            )
        if current_attempt.status != "active":
            raise state.TransitionError(
                "recover_job refused: current attempt is not active"
            )
        return _recover_fresh(ledger, project, current_attempt, project_row, actor_id)
    finally:
        ledger.release_lock(project, holder)


ENGINE_RECORDS_DIR = ".gongbang"


def dirty_paths(repo: Path, excludes: tuple[str, ...]) -> tuple[str, ...]:
    """Relative paths (literal filenames, never git-quoted) in the
    commit-tree range that differ from HEAD. Uses `-z` so non-ASCII/special
    filenames come back as literal UTF-8 bytes instead of octal-escaped,
    quoted text (`git status --porcelain` without `-z` would otherwise emit
    e.g. "src/\\355\\225\\234.py" for a Korean filename, which does not
    match any real path). For a rename/copy entry (status starts with R or
    C), `-z` emits the NEW path as the normal entry and then the OLD path
    as a separate, unprefixed token — both are recorded."""
    out = subprocess.run(
        git_argv(repo, "status", "--porcelain", "-z", "--untracked-files=all"),
        check=True, capture_output=True,
    ).stdout
    tokens = out.decode("utf-8", "surrogateescape").split("\0")
    paths: set[str] = set()
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry:
            continue
        status, rel = entry[:2], entry[3:]
        paths.add(rel)
        if status[0] in ("R", "C") and i < len(tokens) and tokens[i]:
            paths.add(tokens[i])
            i += 1
    # The engine's own records under .gongbang/ (facts.md, probe/t<N>.log,
    # plan, review files) must travel even when the app's .gitignore
    # matches them (a common "*.log" rule dropped every probe log and
    # stopped a real job at probe_failed). Ignored files elsewhere stay
    # out, exactly as before.
    ignored = subprocess.run(
        git_argv(repo, "status", "--porcelain", "-z", "--ignored=matching",
                 "--untracked-files=all", "--", ENGINE_RECORDS_DIR),
        check=True, capture_output=True,
    ).stdout.decode("utf-8", "surrogateescape").split("\0")
    for entry in ignored:
        if entry.startswith("!! "):
            rel = entry[3:]
            if rel.endswith("/"):
                for f in (repo / rel).rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        paths.add(str(f.relative_to(repo)))
            else:
                paths.add(rel)
    excluded_prefixes = tuple(e.rstrip("/") for e in excludes)
    def is_excluded(p: str) -> bool:
        return any(p == e or p.startswith(e + "/") for e in excluded_prefixes)
    return tuple(sorted(p for p in paths if not is_excluded(p)))


def _copies_dir(config: EngineConfig, attempt_id: str) -> Path:
    """config.home/copies/{attempt_id} -- the directory `_make_copies`
    atomically renames its `.tmp-` staging root into, holding every
    role's own subdirectory. Factored out so job.open_intent's own
    payload (open_job) and _reconcile_orphaned_intents' cleanup (same
    module) compute the exact same path `_make_copies` itself does,
    never a second, separately-maintained copy of this join."""
    return config.home / "copies" / ids.attempt_dirname(attempt_id)


def copy_root(config: EngineConfig, attempt_id: str, role: str) -> Path:
    return _copies_dir(config, attempt_id) / role


def copy_git_config_matches_recorded(ledger: Ledger, project: str, attempt_id: str,
                                     role: str, copy_root_path: Path) -> bool:
    """True iff copy_root_path/.git/config's CURRENT sha256 matches the
    one job.opened recorded for this role right after _make_copy
    created it (or recovery inherited it -- see _recovery_payload).
    False means the copy's own git identity/config was altered after
    creation -- callers (station.record_station's dirty_paths call,
    today's only engine-side git invocation against a worker copy)
    must fail closed (StepFailed("copy_git_config_changed")) rather
    than trust a further git call against it: a worker-run command
    (e.g. an allowed `git` terminal pattern, or code running inside
    the copy) could otherwise plant a `core.fsmonitor`/hooks entry
    there for THIS call to execute with engine privileges, same shape
    as the bench/.git finding gitutil.py closes for the real repo.
    Missing on either side (no recorded value, or the copy/.git/config
    file itself gone) is also a mismatch -- fail closed, not skip."""
    recorded: object = None
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            recorded = event.payload.get("copy_git_config_sha256", {}).get(role)
            break
    config_file = copy_root_path / ".git" / "config"
    if not config_file.exists():
        return False
    current = hashlib.sha256(config_file.read_bytes()).hexdigest()
    return recorded == current


def _make_copies(project_config: ProjectConfig, config: EngineConfig,
                 attempt_id: str, roles: tuple[str, ...],
                 ) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Build every role's worker copy under a `.tmp-{attempt_id}` staging
    directory, then atomically rename it into config.home/copies/{attempt_id}
    once all of them succeed. On any failure, remove the temp directory and
    re-raise — nothing is left in the real copies/ location. Returns
    (skipped, git_config_sha256): every copy_links entry _make_copy
    refused to link (fail closed, not an exception), as
    {"role", "entry", "reason"}, and role -> sha256(copy/.git/config)
    right after _make_copy's own `git init`/`commit` -- both journaled
    onto job.opened's own payload by the caller, the latter so a later
    engine-side git call against the copy (station.record_station's
    dirty_paths, e.g.) can detect the copy's own git identity/config
    having been altered since creation and refuse to trust it further."""
    copies_root = config.home / "copies"
    dirname = ids.attempt_dirname(attempt_id)
    tmp_root = copies_root / f".tmp-{dirname}"
    skipped: list[dict[str, str]] = []
    git_config_sha256: dict[str, str] = {}
    try:
        if tmp_root.exists():
            shutil.rmtree(tmp_root)
        tmp_root.mkdir(parents=True)
        for role in roles:
            role_skipped, sha256 = _make_copy(
                project_config.repo, tmp_root / role, project_config.copy_links,
                project_config.copy_link_writable_subpaths,
            )
            for entry, reason in role_skipped:
                skipped.append({"role": role, "entry": entry, "reason": reason})
            git_config_sha256[role] = sha256
        tmp_root.rename(_copies_dir(config, attempt_id))
    except BaseException:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    return skipped, git_config_sha256


_COPY_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "engine", "GIT_AUTHOR_EMAIL": "engine@local",
    "GIT_COMMITTER_NAME": "engine", "GIT_COMMITTER_EMAIL": "engine@local",
}


def is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _link_copy_link_entry(repo_real: Path, dest_real: Path, dest: Path,
                          repo: Path, entry: str,
                          writable_subpaths: tuple[str, ...] = ()) -> str | None:
    """Create the symlink(s) for one copy_links entry, or return a
    "copy_link_*" skip reason instead of creating anything -- never
    raises. Fails closed on either side: the SOURCE (repo/entry) must
    resolve under the repo (an archived tree can itself track a
    symlink at an intermediate component, e.g. "deps -> ../../shared",
    through which a lexically-valid entry resolves outside the repo
    entirely), and no component of the DESTINATION's parent chain
    inside `dest` may already be a symlink (the git archive just
    extracted into `dest` can itself contain one at that path --
    mkdir(exist_ok=True) on top of it is a silent no-op, and the
    symlink this function would create under it physically lands
    wherever that pre-existing symlink points, outside `dest`).

    With no `writable_subpaths`: `dest/entry` is a single symlink to
    `repo/entry` -- UNLESS `dest/entry` already exists as a real path
    (e.g. a copy_links entry like "knowledge" whose own .gitignore
    only ignores PART of it -- "knowledge/*" + "!knowledge/SOURCES.md"
    -- so the copy's `git archive HEAD` extraction already materialized
    a real "knowledge/" directory containing that one tracked file
    before this function ever runs; a flat `symlink_to` there raises
    FileExistsError). In that case this merges instead, via
    `link_entry_children` (defect 22): only `repo/entry`'s children
    that do not already exist in the copy get symlinked in, recursing
    one level deeper only where BOTH sides are already a real
    directory at the same child name; a tracked file the archive
    already placed there is left exactly as committed. With
    `writable_subpaths` (e.g. node_modules needing .vite-temp/.vite
    writable at runtime despite the rest being read-only): `dest/entry`
    becomes a REAL directory instead -- a symlink has no "contents" of
    its own to selectively override, and nesting a writable OS-sandbox
    mount under a read-only re-exposure of `repo/entry` is impossible
    (bwrap cannot create a missing mountpoint under an already
    read-only bind, verified empirically -- so this split has to happen
    at copy-creation time instead, never at sandbox-mount time). Each
    of `repo/entry`'s own immediate children is individually symlinked
    in via the same `link_entry_children` merge (so a partly-tracked
    entry combined with writable_subpaths also just works), and each
    configured writable_subpaths name becomes a real, empty, writable
    directory living ONLY in the copy -- it is never symlinked to the
    repo, so writes under it can never reach the real repo's
    node_modules, and it needs no sandbox mount at all (the copy itself
    is already writable). Either way, nothing here ever raises: a
    conflict this function cannot resolve is reported as a
    "copy_link_*" skip reason instead."""
    target = repo / entry
    if not target.exists():
        return None  # nothing to link; not a failure (pre-existing behavior)
    target_real = Path(os.path.realpath(target))
    if target_real != repo_real and not is_within(target_real, repo_real):
        return "copy_link_source_escapes_repo"

    current = dest
    for part in Path(entry).parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            return "copy_link_parent_is_symlink"
    current.mkdir(parents=True, exist_ok=True)
    parent_real = Path(os.path.realpath(current))
    if parent_real != dest_real and not is_within(parent_real, dest_real):
        return "copy_link_parent_escapes_copy"

    entry_dest = dest / entry
    if not writable_subpaths:
        if not entry_dest.exists():
            entry_dest.symlink_to(target)
            return None
        if entry_dest.is_symlink():
            return None  # already linked (e.g. a re-run); nothing further to do
        if not target.is_dir() or not entry_dest.is_dir():
            # A real, non-directory conflict on at least one side (a
            # tracked FILE already sits at this exact path) -- the
            # archive's own tracked content wins, nothing to link.
            return None
        return link_entry_children(target, entry_dest, dest_real, repo_real)

    if not target.is_dir():
        return "copy_link_writable_subpaths_requires_dir"
    entry_dest.mkdir(parents=True, exist_ok=True)
    reason = link_entry_children(target, entry_dest, dest_real, repo_real,
                                   skip_names=frozenset(writable_subpaths))
    if reason is not None:
        return reason
    for subpath in writable_subpaths:
        (entry_dest / subpath).mkdir(parents=True, exist_ok=True)
    return None


def link_entry_children(target: Path, entry_dest: Path, dest_real: Path,
                         repo_real: Path | None,
                         skip_names: frozenset[str] = frozenset()) -> str | None:
    """Symlink each of `target`'s own children into the already-existing
    real directory `entry_dest`, skipping any name in `skip_names`
    (copy_link_writable_subpaths) and any child `entry_dest` already
    has (a tracked file/dir the copy's git-archive, or the snapshot's
    export_tree, already materialized there -- that tracked content
    wins, nothing to link over it) -- recursing one level deeper only
    for a child that is a real directory on BOTH sides (so a tracked
    file nested two levels down under an otherwise-untracked tree still
    gets its untracked siblings linked in). Shared by
    `_link_copy_link_entry` (worker copies, open_job._make_copy) and
    `engine.application.steps.suite_snapshot._link_snapshot_dep_entry`
    (disposable snapshots), so the one merge rule that avoids
    FileExistsError on a partly-tracked copy_links entry (defect 22)
    only has to be right in one place.

    `repo_real` fail-closes a child the same way the whole-entry case
    already does (a child whose realpath resolves outside the repo is
    skipped, not the whole entry) -- callers that must NOT fail closed
    on source (the snapshot caller; see that module's own docstring for
    why) pass `repo_real=None` instead, to skip only that check while
    keeping this same merge behavior. Never raises: a conflict this
    cannot resolve for one child is simply left as whatever already
    exists there, same fail-closed-but-non-raising shape as the
    whole-entry case."""
    for child_name in os.listdir(target):
        if child_name in skip_names:
            continue
        child = target / child_name
        if repo_real is not None:
            child_real = Path(os.path.realpath(child))
            if child_real != repo_real and not is_within(child_real, repo_real):
                continue  # fail closed for this one child, not the whole entry
        child_dest = entry_dest / child_name
        if not child_dest.exists():
            child_dest.symlink_to(child)
            continue
        if child_dest.is_symlink():
            continue  # already linked; nothing further to do
        if child.is_dir() and child_dest.is_dir():
            reason = link_entry_children(child, child_dest, dest_real, repo_real)
            if reason is not None:
                return reason
            continue
        # A real, non-directory conflict (a tracked file already sits
        # at this exact child path) -- the already-materialized tracked
        # content wins, nothing to link.
    return None


def _copy_git_config_sha256(dest: Path) -> str:
    return hashlib.sha256((dest / ".git" / "config").read_bytes()).hexdigest()


def _make_copy(repo: Path, dest: Path, copy_links: tuple[str, ...] = (),
               copy_link_writable_subpaths: dict[str, tuple[str, ...]] | None = None,
               ) -> tuple[list[tuple[str, str]], str]:
    """Returns (skipped, sha256): every copy_links entry skipped instead
    of linked, as (entry, reason) -- see _link_copy_link_entry -- and
    sha256(dest/.git/config) right after this function's own `git
    init` created it."""
    writable_subpaths_by_entry = copy_link_writable_subpaths or {}
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        git_argv(repo, "archive", "HEAD"),
        check=True, capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    # copy_links (e.g. "node_modules"): symlink straight to the repo's own
    # copy, BEFORE git init/add/commit below, so the link becomes part of
    # the copy's own base commit (if the repo's .gitignore covers it, `git
    # add -A` simply skips it, same as any other ignored path) -- either
    # way `git status` in the copy is clean right after _make_copy, same
    # as every other file. A missing source path is simply skipped, not
    # a failure (ProjectConfig.copy_links validates the entry itself at
    # config load, not whether it exists on disk for a given repo); an
    # unsafe one (see _link_copy_link_entry) is also skipped, never
    # raised -- fail closed, not crash.
    repo_real = Path(os.path.realpath(repo))
    dest_real = Path(os.path.realpath(dest))
    skipped: list[tuple[str, str]] = []
    for entry in copy_links:
        reason = _link_copy_link_entry(
            repo_real, dest_real, dest, repo, entry,
            writable_subpaths_by_entry.get(entry, ()),
        )
        if reason is not None:
            skipped.append((entry, reason))
    subprocess.run(git_argv(dest, "init", "-q"), check=True,
                    capture_output=True)
    # Commit the base so `git status` in the copy reports only what the
    # worker changed (record_station's changed_paths), not every file as
    # untracked. Identity is fixed and global config is ignored so the
    # copy's history never depends on the operator's git setup.
    env = {**os.environ, **_COPY_GIT_IDENTITY, "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run(git_argv(dest, "add", "-A"), check=True,
                    capture_output=True, env=env)
    # Engine records the repo tracks under .gongbang/ stay in the base even
    # when the app's .gitignore matches them, so the copy starts equal to
    # HEAD there (only .gongbang/ is forced; copy_links stay out).
    if (dest / ENGINE_RECORDS_DIR).is_dir():
        subprocess.run(git_argv(dest, "add", "-f", "--", ENGINE_RECORDS_DIR), check=True,
                        capture_output=True, env=env)
    subprocess.run(git_argv(dest, "commit", "-q", "-m", "base"),
                    check=True, capture_output=True, env=env)
    return skipped, _copy_git_config_sha256(dest)
