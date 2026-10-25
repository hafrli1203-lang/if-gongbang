"""close / abandon — 8 steps, each crash-safe (plan §7).

set_closing        state -> CLOSING (remember prior state in step payload)
archive_tmp        copy HEAD-vs-tree differences (range only, minus
                   dirty_paths) to archive/{project}/{attempt_id}/.tmp-{ts}
archive_commit     rename .tmp-{ts} -> {ts}; append archive.done
                   reconcile: done only if final dir AND archive.done exist
reset_to_head      git checkout HEAD -- / clean within range, skipping
                   dirty_paths and excludes
apply_deferred_approvals  (stage 1: no-op that records 'none')
verify_tree_eq_head       fingerprint(repo) == HEAD tree (dirty_paths aside)
                   -> on mismatch stay CLOSING, request failed
set_closed         attempt.finished {closed|abandoned}; state -> CLOSED
open_queue         (stage 1: records queue length)
Archive failure in archive_tmp/archive_commit -> state back to prior, request failed.

The archive directory name ("ts") is a deterministic token derived from the
request id rather than a wall-clock timestamp, so a crash-and-resume never
produces two archive directories for the same close/abandon request.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from engine import ids, procs, state
from engine.evidence import FingerprintError, fingerprint, sha256_file
from engine.ledger import Event
from engine.processor import StepContext, StepDef, StepFailed


# ---- shared helpers --------------------------------------------------

def _project_config(ctx: StepContext):
    return ctx.config.projects[ctx.project]


def _repo(ctx: StepContext) -> Path:
    return _project_config(ctx).repo


def _excludes(ctx: StepContext) -> tuple[str, ...]:
    return _project_config(ctx).excludes


def _dirty_paths(ctx: StepContext) -> set[str]:
    return set(ctx.ledger.project(ctx.project).dirty_paths)


def _archive_token(ctx: StepContext) -> str:
    return hashlib.sha256(ctx.request.request_id.encode("utf-8")).hexdigest()[:12]


def _archive_root(ctx: StepContext) -> Path:
    return ctx.config.home / "archive" / ctx.project / ids.attempt_dirname(ctx.attempt_id)


def _tmp_dir(ctx: StepContext) -> Path:
    return _archive_root(ctx) / f".tmp-{_archive_token(ctx)}"


def _final_dir(ctx: StepContext) -> Path:
    return _archive_root(ctx) / _archive_token(ctx)


def _changed_paths(ctx: StepContext) -> list[tuple[str, str, str | None]]:
    """(status_code, path, orig_path) triples from `git status --porcelain
    -z --untracked-files=all`, minus excludes and minus dirty_paths.
    orig_path is the pre-rename/copy path for R/C entries, else None.
    NUL-separated (-z) output is used rather than line-splitting: a plain
    newline split mishandles quoted paths with special characters, and a
    rename's original path is a separate NUL-terminated field rather than
    an " -> " substring (which -z does not emit) -- reset_to_head needs
    that original path to restore it from HEAD, not just drop it."""
    repo = _repo(ctx)
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
         "status", "--porcelain", "-z", "--untracked-files=all"],
        check=True, capture_output=True, text=True,
    ).stdout
    dirty = _dirty_paths(ctx)
    excludes = tuple(e.rstrip("/") for e in _excludes(ctx))

    def is_excluded(p: str) -> bool:
        return any(p == e or p.startswith(e + "/") for e in excludes)

    tokens = out.split("\0")
    result: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if entry == "":
            continue
        code, path = entry[:2], entry[3:]
        orig: str | None = None
        if code[0] in ("R", "C"):
            # Rename/copy: the next NUL-terminated field is the original
            # path.
            orig = tokens[i] if i < len(tokens) else None
            i += 1
        if path in dirty or is_excluded(path):
            continue
        result.append((code, path, orig))
    return result


def _on_archive_failure(ctx: StepContext) -> None:
    # Read the prior state from the state.changed event itself (its 'prev'
    # field), not from set_closing's journaled step payload: if the
    # processor crashed right after set_closing's run() and resumed via
    # reconcile (state already CLOSING), the step is marked done with an
    # empty payload, so 'prior' would not be recoverable from there.
    prior = None
    for _seq, event in reversed(ctx.ledger.events(ctx.project, "state.changed")):
        if event.payload.get("state") == "CLOSING":
            prior = event.payload.get("prev")
            break
    input_name = "archive_failed_open" if prior == "OPEN" else "archive_failed_recovering"
    state.transition(ctx.ledger, ctx.project, input_name, "processor")


# ---- step bodies -----------------------------------------------------

def _has_report(ctx: StepContext) -> bool:
    return any(
        event.attempt_id == ctx.attempt_id
        for _seq, event in ctx.ledger.events(ctx.project, "report.written")
    )


def _run_set_closing(ctx: StepContext) -> dict[str, object]:
    # The close/abandon distinction is the request kind itself (requests
    # of kind "abandon" always carry an "abandon" mode); payload.mode may
    # be absent (e.g. an abandon request submitted with an empty payload).
    mode = ctx.request.kind
    current = ctx.ledger.project(ctx.project).state
    if current == "CLOSING":
        # A prior close/abandon request already drove the project into
        # CLOSING and then failed (e.g. verify_tree_eq_head) without a
        # path back to OPEN/RECOVERING. (CLOSING, close_job)/(CLOSING,
        # abandon_job) are not in TRANSITIONS, so a second close request's
        # own set_closing must not attempt that transition again — it
        # just records the prior state and proceeds to close stations.
        # But skipping state.transition() also skips the guard it applies
        # to a real (RECOVERING, close_job) transition (a report for this
        # attempt must exist) -- re-apply that guard here so a stuck
        # CLOSING-from-RECOVERING can't be closed around it.
        prior = _prior_closing_state(ctx)
        if prior == "RECOVERING" and mode == "close" and not _has_report(ctx):
            raise StepFailed("report_required")
    else:
        prior = current
        input_name = "close_job" if mode == "close" else "abandon_job"
        state.transition(ctx.ledger, ctx.project, input_name, "processor")
    for station in ctx.config.stations:
        procs.close_station(ctx.ledger, ctx.project, ctx.attempt_id, station)
    return {"prior": prior, "mode": mode}


def _prior_closing_state(ctx: StepContext) -> str | None:
    for _seq, event in reversed(ctx.ledger.events(ctx.project, "state.changed")):
        if event.payload.get("state") == "CLOSING":
            return event.payload.get("prev")
    return None


def _reconcile_set_closing(ctx: StepContext) -> bool:
    # State alone isn't enough: set_closing's own run() transitions to
    # CLOSING and THEN kills every station's live processes. A crash in
    # between (or a resumed run that only got partway through the station
    # loop) leaves the state already CLOSING with children still alive;
    # treating that as "done" would leave them running forever. Re-run
    # (idempotent: the transition is skipped, close_station is called
    # again) whenever any are still live.
    if ctx.ledger.project(ctx.project).state != "CLOSING":
        return False
    return not ctx.ledger.live_procs(ctx.attempt_id)


def _entry_mode(path: Path) -> str:
    """Git-style mode string for a working-tree path (mirrors
    evidence._hash_entry's mode detection, so archived mode and the
    fingerprint's own notion of mode agree)."""
    if path.is_symlink():
        return "120000"
    return "100755" if os.access(path, os.X_OK) else "100644"


def _run_archive_tmp(ctx: StepContext) -> dict[str, object]:
    tmp = _tmp_dir(ctx)
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        deleted: list[str] = []
        modes: dict[str, str] = {}
        repo = _repo(ctx)
        for _code, path, _orig in _changed_paths(ctx):
            src = repo / path
            if not src.exists():
                deleted.append(path)
                continue
            dest = tmp / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            modes[path] = _entry_mode(src)
        if deleted:
            (tmp / ".deleted").write_text(
                "\n".join(sorted(deleted)) + "\n", encoding="utf-8"
            )
        # Record each archived path's mode (executable bit / symlink) so
        # reset_to_head can refuse a change whose bytes match the archive
        # but whose mode (e.g. chmod +x after archiving) does not.
        (tmp / ".modes").write_text(json.dumps(modes), encoding="utf-8")
    except OSError as exc:
        _on_archive_failure(ctx)
        raise StepFailed("archive_failed") from exc
    return {"tmp": str(tmp), "token": _archive_token(ctx)}


def _run_archive_commit(ctx: StepContext) -> dict[str, object]:
    tmp = _tmp_dir(ctx)
    final = _final_dir(ctx)
    try:
        if not final.exists():
            if not tmp.exists():
                raise OSError(f"archive tmp missing: {tmp}")
            final.parent.mkdir(parents=True, exist_ok=True)
            os.rename(tmp, final)
    except OSError as exc:
        _on_archive_failure(ctx)
        raise StepFailed("archive_failed") from exc
    ctx.ledger.append(Event(
        project=ctx.project, kind="archive.done", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"attempt_id": ctx.attempt_id, "path": str(final)},
    ))
    return {"path": str(final)}


def _reconcile_archive_commit(ctx: StepContext) -> bool:
    final = _final_dir(ctx)
    if not final.exists():
        return False
    done_events = ctx.ledger.events(ctx.project, "archive.done")
    return any(event.payload.get("path") == str(final) for _seq, event in done_events)


def _verify_changes_are_archived(ctx: StepContext,
                                 changed: list[tuple[str, str, str | None]]) -> None:
    """Refuse to reset/delete any live change that archive_commit did not
    actually back up. archive_tmp/archive_commit are 'done' by the time
    reset_to_head runs; a crash after them followed by a worker editing,
    creating, or just chmod'ing a file means the live change list
    (recomputed here) can contain paths the archive never saw a copy of,
    or whose mode no longer matches what was archived. Comparing against
    the bytes AND mode sitting in the final archive directory on disk
    (not the step journal payload, which may be stale/empty on a
    reconcile-skip) is the only way to tell a genuinely-archived change
    from one that would be silently discarded (data loss) by the rest of
    this step."""
    final = _final_dir(ctx)
    deleted_file = final / ".deleted"
    deleted_set = set(
        deleted_file.read_text(encoding="utf-8").splitlines()
    ) if deleted_file.exists() else set()
    modes_file = final / ".modes"
    archived_modes: dict[str, object] = (
        json.loads(modes_file.read_text(encoding="utf-8")) if modes_file.exists() else {}
    )
    repo = _repo(ctx)
    for _code, path, _orig in changed:
        archived = final / path
        live = repo / path
        if archived.exists():
            if (not live.exists() or sha256_file(live) != sha256_file(archived)
                    or _entry_mode(live) != archived_modes.get(path)):
                raise StepFailed("changed_after_archive")
        elif path in deleted_set:
            if live.exists():
                raise StepFailed("changed_after_archive")
        else:
            # Not represented in the archive at all: a change made after
            # archive_commit ran. Leave it untouched and fail closed.
            raise StepFailed("changed_after_archive")


def _drop_path(repo: Path, path: str) -> None:
    """Unstage (ignore errors: it may not even be in the index) and delete
    a working-tree path that does not exist in HEAD under this name."""
    subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
         "rm", "--cached", "-q", "--ignore-unmatch", "--", path],
        capture_output=True,
    )
    full = repo / path
    if full.is_dir():
        shutil.rmtree(full, ignore_errors=True)
    elif full.exists():
        full.unlink()


def _restore_path(repo: Path, path: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
         "checkout", "-q", "HEAD", "--", path],
        check=True, capture_output=True,
    )


def _run_reset_to_head(ctx: StepContext) -> dict[str, object]:
    repo = _repo(ctx)
    changed = _changed_paths(ctx)
    _verify_changes_are_archived(ctx, changed)
    for code, path, orig in changed:
        if code[0] in ("R", "C") and orig is not None:
            # Rename/copy: HEAD has no entry under the new name, so
            # checking it out there would fail the same way an added
            # path would. Drop the new path and restore the original one
            # (untouched for a copy; the renamed-away original for a
            # rename) from HEAD.
            _drop_path(repo, path)
            _restore_path(repo, orig)
        elif code == "??" or code[0] == "A":
            # Untracked, or staged-new-but-never-committed.
            _drop_path(repo, path)
        else:
            _restore_path(repo, path)
    return {"reset": True}


def _run_apply_deferred_approvals(ctx: StepContext) -> dict[str, object]:
    return {"applied": 0}


def _run_verify_tree_eq_head(ctx: StepContext) -> dict[str, object]:
    repo = _repo(ctx)
    dirty = _dirty_paths(ctx)
    if not dirty:
        try:
            fp = fingerprint(repo, _excludes(ctx))
            head_tree = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        except (FingerprintError, subprocess.CalledProcessError, OSError) as exc:
            raise StepFailed("tree_ne_head") from exc
        if fp != head_tree:
            raise StepFailed("tree_ne_head")
    else:
        remaining = _changed_paths(ctx)  # already excludes dirty_paths
        if remaining:
            raise StepFailed("tree_ne_head")
    return {"ok": True}


def _run_set_closed(ctx: StepContext) -> dict[str, object]:
    """Two durable effects, in order: `attempt.finished` (flips the
    attempt out of 'active') then `state.transition` (flips the project
    CLOSING -> CLOSED). A crash between them used to leave the project
    stuck CLOSING forever: this step's own reconcile (_reconcile_set_
    closed) is "project.state == CLOSED", so the processor's resume rule
    (module docstring) calls run() again from scratch -- which, before
    this guard, re-appended a SECOND attempt.finished event for an
    attempt that was already finished. Checking the attempt's own
    current status first makes a resumed call skip straight to the
    state transition it never got to the first time, rather than
    redoing (and duplicating) the first effect too."""
    status = "closed" if ctx.request.kind == "close" else "abandoned"
    if ctx.ledger.attempt(ctx.attempt_id).status == "active":
        ctx.ledger.append(Event(
            project=ctx.project, kind="attempt.finished", actor_kind="processor",
            actor_id="processor", attempt_id=ctx.attempt_id,
            payload={"attempt_id": ctx.attempt_id, "status": status},
        ))
    if ctx.ledger.project(ctx.project).state != "CLOSED":
        state.transition(ctx.ledger, ctx.project, "finalized", "processor")
    return {"status": status}


def _reconcile_set_closed(ctx: StepContext) -> bool:
    return ctx.ledger.project(ctx.project).state == "CLOSED"


def _run_open_queue(ctx: StepContext) -> dict[str, object]:
    return {"queued": len(ctx.ledger.queued_requests(ctx.project))}


def steps(ctx: StepContext) -> list[StepDef]:
    return [
        StepDef("set_closing", _run_set_closing, _reconcile_set_closing),
        StepDef("archive_tmp", _run_archive_tmp, lambda ctx: False),
        StepDef("archive_commit", _run_archive_commit, _reconcile_archive_commit),
        StepDef("reset_to_head", _run_reset_to_head, lambda ctx: False),
        StepDef("apply_deferred_approvals", _run_apply_deferred_approvals, lambda ctx: False),
        StepDef("verify_tree_eq_head", _run_verify_tree_eq_head, lambda ctx: False),
        StepDef("set_closed", _run_set_closed, _reconcile_set_closed),
        StepDef("open_queue", _run_open_queue, lambda ctx: False),
    ]
