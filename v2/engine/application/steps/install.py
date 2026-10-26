"""install / scripts_change / test_config_change — serialized per project.

acquire_project_lock  ledger lock "install:{project}" (second request waits:
                      processor processes one request at a time anyway, the
                      lock guards against a second processor). A lock whose
                      current holder is a request that is "failed" or "done"
                      is reclaimed (released then re-acquired) rather than
                      left to leak forever (P1-7): a holder that is
                      "running" or "unknown", or that is not a known
                      request_id at all, keeps the lock -> StepFailed
                      ("install_locked").
invalidate_evidence   evidence.invalidate_after(..., ("review","bench","cross")),
                      run BEFORE run_installer (not after): even an
                      install_cmd that fails and rolls the manifest back
                      byte-for-byte may already have changed .deps/ or
                      other environment state before it failed, so any
                      review/bench evidence recorded against the OLD
                      dependency state must be invalidated regardless of
                      whether the install itself succeeds.
run_installer         re-reads the snapshot FILE (not the journal
                      payload, and not whatever is currently on disk --
                      a crash between a first apply() writing the
                      modified manifest and install_cmd finishing would
                      otherwise make a resumed apply() treat the
                      half-applied bytes as "before") and passes those
                      bytes as `original` to
                      fakes.installer.apply(key=request_id, manifest,
                      change, original=...), so rollback and re-derivation
                      of `change` both anchor on the true pre-install
                      bytes. Missing snapshot file on resume ->
                      StepFailed("snapshot_missing").
                      reconcile: installer.applied(request_id)
verify_manifest       manifest now contains every key of `change`. `manifest`
                      is validated the same way as merge's paths (P2-1):
                      must resolve under the repo and not be policy-
                      protected -> StepFailed("path_rejected").
record                append install.recorded; release lock

snapshot_manifest also refuses the request before any write (B-12) if the
manifest's relative path is one of the project's current dirty_paths (the
owner's own uncommitted edit, taken at open_job time): StepFailed
("manifest_dirty"), so an install can never silently clobber an edit the
owner has not committed. The pre-install manifest bytes (not just a hash)
are saved to config.home/"jobs"/{attempt_id}/snapshots/{request_id}-manifest
so a bad install is recoverable. `manifest` is posix-normalized
(_normalized_manifest_rel) everywhere it is used -- a raw string compare
against dirty_paths missed e.g. "./package.json" (B-12 remainder).
"""
from __future__ import annotations

import dataclasses
import json
import os
import posixpath
from pathlib import Path

from engine.infrastructure import evidence
from engine.domain import ids
from engine.infrastructure.installers import InstallFailed
from engine.domain.records import Event, LedgerError
from engine.domain.policy import Policy
from engine.application.processor import StepContext, StepDef, StepFailed, StepUnknown


def steps(ctx: StepContext) -> list[StepDef]:
    return [
        StepDef("acquire_project_lock", _acquire_lock, _reconcile_acquire_lock),
        StepDef("snapshot_manifest", _snapshot_manifest, _reconcile_false),
        StepDef("invalidate_evidence", _invalidate_evidence, _reconcile_false),
        StepDef("run_installer", _run_installer, _reconcile_run_installer),
        StepDef("verify_manifest", _verify_manifest, _reconcile_verify_manifest),
        StepDef("record", _record, _reconcile_record),
    ]


def _lock_key(project: str) -> str:
    return f"install:{project}"


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _normalized_manifest_rel(ctx: StepContext) -> str:
    """posix-normalized `manifest` (B-12 remainder): "./package.json" and
    "package.json" must be recognized as the same path by the dirty-path
    check and by every other use of this value -- a raw string compare
    against dirty_paths missed "./package.json" even though it is the
    same file the owner has an uncommitted edit on. normpath still leaves
    a leading ".." alone (it cannot resolve further), so the existing
    ".." rejection below is unaffected."""
    raw = str(ctx.request.payload["manifest"])
    return posixpath.normpath(raw)


def _manifest_path(ctx: StepContext) -> Path:
    project_config = ctx.config.projects[ctx.project]
    rel = _normalized_manifest_rel(ctx)
    target = project_config.repo / rel
    # P2-1: validate `manifest` the same way merge validates paths.
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise StepFailed("path_rejected")
    # A symlinked manifest could point outside the repo (or at a
    # protected engine path) despite `rel` itself looking innocuous --
    # os.path.realpath below would silently follow it. Reject the link
    # outright rather than trusting where it resolves to.
    if os.path.islink(target):
        raise StepFailed("path_rejected")
    real_repo = Path(os.path.realpath(project_config.repo))
    real_target = Path(os.path.realpath(target))
    if real_target != real_repo and not _is_under(real_target, real_repo):
        raise StepFailed("path_rejected")
    # The project's configured manifest is the one file an install request
    # exists to change, so a config.protected_paths entry for it (which
    # keeps WORKERS from editing it directly) must not reject the
    # processor's own install path. Only the entries that name the
    # manifest itself are dropped (exact relative path, or its bare
    # filename) -- every other protected path (e.g. a different file
    # that happens to share a directory) stays protected even on this
    # request.
    policy_config = ctx.config
    if rel == project_config.manifest:
        manifest_name = Path(rel).name
        policy_config = dataclasses.replace(
            ctx.config,
            protected_paths=tuple(
                p for p in ctx.config.protected_paths
                if p != rel and p != manifest_name
                and not rel.startswith(p.rstrip("/") + "/")
            ),
        )
    policy = Policy(policy_config, ctx.ledger)
    if policy.is_protected(ctx.project, target):
        raise StepFailed("path_rejected")
    return target


def _reconcile_true(ctx: StepContext) -> bool:
    return True


def _reconcile_false(ctx: StepContext) -> bool:
    return False


# ---- acquire_project_lock -------------------------------------------------

def _holder_reclaimable(ctx: StepContext, holder: str) -> bool:
    """True if `holder` is a known request whose status is failed or done
    (so its lock can be safely reclaimed). A holder that is running or
    unknown, or that is not a request_id the ledger recognizes at all,
    is NOT reclaimable: we cannot prove the lock is actually free."""
    try:
        held_request = ctx.ledger.request(holder)
    except LedgerError:
        return False
    return held_request.status in ("failed", "done")


def _acquire_lock(ctx: StepContext) -> dict[str, object]:
    holder = ctx.request.request_id
    key = _lock_key(ctx.project)
    if ctx.ledger.acquire_lock(key, holder):
        return {"holder": holder}
    current_holder = ctx.ledger.lock_holder(key)
    if current_holder is not None and _holder_reclaimable(ctx, current_holder):
        # #21 remainder: the failed/done holder's lock may have been
        # acquired by a different process (a different pid) than this
        # one -- plain release_lock requires a matching pid and would
        # refuse to drop it. force_release_lock drops it regardless,
        # which is safe here specifically because _holder_reclaimable
        # already proved the holder's request is terminal (failed/done).
        ctx.ledger.force_release_lock(key, current_holder)
        if ctx.ledger.acquire_lock(key, holder):
            return {"holder": holder}
    raise StepFailed("install_locked")


def _reconcile_acquire_lock(ctx: StepContext) -> bool:
    key = _lock_key(ctx.project)
    if ctx.ledger.acquire_lock(key, ctx.request.request_id):
        return True
    return ctx.ledger.lock_holder(key) == ctx.request.request_id


# ---- snapshot_manifest ------------------------------------------------

def _snapshot_dir(ctx: StepContext) -> Path:
    return ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id) / "snapshots"


def _snapshot_path(ctx: StepContext) -> Path:
    """The snapshot file _snapshot_manifest writes for this request --
    computed directly rather than read from the journal, since it is a
    pure function of (ctx.config.home, attempt_id, request_id)."""
    return _snapshot_dir(ctx) / f"{ctx.request.request_id}-manifest"


def _snapshot_manifest(ctx: StepContext) -> dict[str, object]:
    path = _manifest_path(ctx)
    # B-12: never overwrite a manifest the owner has not committed yet.
    rel = _normalized_manifest_rel(ctx)
    dirty = set(ctx.ledger.project(ctx.project).dirty_paths)
    if rel in dirty:
        raise StepFailed("manifest_dirty")
    before = evidence.sha256_file(path) if path.exists() else ""
    snapshot_path = _snapshot_path(ctx)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(path.read_bytes() if path.exists() else b"")
    return {"before": before, "snapshot_path": str(snapshot_path)}


# ---- run_installer -----------------------------------------------------

def _run_installer(ctx: StepContext) -> dict[str, object]:
    manifest = _manifest_path(ctx)
    change = ctx.request.payload["change"]
    snapshot_path = _snapshot_path(ctx)
    if not snapshot_path.exists():
        raise StepFailed("snapshot_missing")
    original = snapshot_path.read_bytes()
    try:
        ctx.fakes.installer.apply(ctx.request.request_id, manifest, change,
                                   original=original)
    except InstallFailed as exc:
        raise StepFailed("install_failed") from exc
    except RuntimeError as exc:
        raise StepUnknown("installer_response_lost") from exc
    return {"change": change}


def _reconcile_run_installer(ctx: StepContext) -> bool:
    return ctx.fakes.installer.applied(ctx.request.request_id)


# ---- verify_manifest -----------------------------------------------------

def _verify_manifest(ctx: StepContext) -> dict[str, object]:
    if not _manifest_matches(ctx):
        raise StepFailed("manifest_mismatch")
    return {}


def _reconcile_verify_manifest(ctx: StepContext) -> bool:
    return _manifest_matches(ctx)


def _lines_manifest_matches(path: Path, change: dict[str, object]) -> bool:
    """Line-list manifest (e.g. requirements.txt): every entry of
    change["add_lines"] must be present as a whole line."""
    add_lines = change.get("add_lines")
    if not isinstance(add_lines, list) or not all(isinstance(x, str) for x in add_lines):
        return False
    try:
        present = set(line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return False
    return all(line.strip() in present for line in add_lines)


def _manifest_matches(ctx: StepContext) -> bool:
    """True iff every top-level key of `change` is present in the manifest
    with the merged value (nested dicts merge one level deep)."""
    path = _manifest_path(ctx)
    change = ctx.request.payload["change"]
    if not path.exists():
        return False
    if path.suffix != ".json":
        return _lines_manifest_matches(path, change)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    for key, expected in change.items():
        actual = data.get(key)
        if isinstance(expected, dict):
            if not isinstance(actual, dict):
                return False
            for sub_key, sub_val in expected.items():
                if actual.get(sub_key) != sub_val:
                    return False
        elif actual != expected:
            return False
    return True


# ---- invalidate_evidence -----------------------------------------------

def _invalidate_evidence(ctx: StepContext) -> dict[str, object]:
    evidence.invalidate_after(
        ctx.ledger, ctx.project, ctx.attempt_id, ("review", "bench", "cross")
    )
    return {}


# ---- record --------------------------------------------------------------

def _record(ctx: StepContext) -> dict[str, object]:
    ctx.ledger.append(Event(
        project=ctx.project, kind="install.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={
            "request_id": ctx.request.request_id, "kind": ctx.request.kind,
            "manifest": _normalized_manifest_rel(ctx),
            "change": ctx.request.payload["change"],
        },
    ))
    ctx.ledger.release_lock(_lock_key(ctx.project), ctx.request.request_id)
    return {}


def _reconcile_record(ctx: StepContext) -> bool:
    for _seq, event in ctx.ledger.events(ctx.project, "install.recorded"):
        if event.payload.get("request_id") == ctx.request.request_id:
            return True
    return False
