"""Which owner requests a job is still waiting on (split out of
engine.application.job_state to keep that module under the 800-line
architecture cap -- see its own module docstring for the whole
picture).

`pending_owner_requests` is the one computation every resume gate and
`gb2 approve-install` reads. A request is fulfilled only by an engine
run of its own kind after it was proposed, with the current manifest
(when a repo is given) still holding the change -- except a
`kind="edit"` request (repair_target.EDIT_REQUEST_KIND,
OWNS_SCOPE_REQUEST_RULE, 결함 26/28), which is never submitted to any
processor at all, so it is fulfilled only by an owner's `--also-own`
grant (repair_target.OWNS_EXTENDED_KIND) covering its path, recorded
strictly after it was proposed (see `_edit_request_fulfilled`).

Imports `load_position`/`JobPosition` from engine.application.job_state
at module level; job_state imports this module back at the BOTTOM of
its own file (after those two names already exist on the job_state
module object), the same deliberate late-import shape the codebase
already uses elsewhere to keep a split module pair from forming a real
cycle -- see job_state._widen_owns_list's own docstring for the same
technique applied to a different pair."""
from __future__ import annotations

import posixpath
from pathlib import Path

from engine.domain.policy import NOTE_REQUEST_KIND, owns_within_prefixes
from engine.domain.records import LedgerError
from engine.application.owner_approval import PENDING_KIND_WHITELIST
from engine.application.repair_target import (
    HANDOFF_KIND, OWNS_EXTENDED_KIND, is_owns_scope_request, owning_later_task,
    record_request_handoff, request_declined,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger
    from engine.application.job_state import JobPosition


def _normalized_manifest(raw: object) -> str:
    return posixpath.normpath(str(raw))


def proposal_seq(ledger: "Ledger", project: str, attempt_id: str | None, request: dict) -> int:
    """The proposal's identity in the ledger: the seq of the newest
    station.finished on `attempt_id` that proposed exactly `request`
    (0 when none -- a position recorded without one)."""
    seq = 0
    for event_seq, event in ledger.events(project, "station.finished"):
        if event.attempt_id == attempt_id and request in (event.payload.get("requests") or []):
            seq = event_seq
    return seq


def proposal_id(ledger: "Ledger", project: str, pos: JobPosition, request: dict) -> str:
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


def _edit_request_fulfilled(ledger: "Ledger", project: str, request: dict, *,
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
    if request_declined(ledger, project, job_name, task_id, path, after_seq):
        return True  # declined by the owner (결함 43): resolved, never granted
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


def request_fulfilled(ledger: "Ledger", project: str, request: dict, *, after_seq: int,
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
    # 결함 57 (a live app run): a whitelisted-kind request (install/
    # scripts_change/test_config_change) whose own `manifest` can never
    # pass validate_pending (e.g. a path outside the project's repo) has
    # no approval route at all -- `gb2 fix <app> --decline <manifest>`
    # (repair_approval.approve_pending_edit) is the owner's only way out,
    # recorded with the SAME task.request_declined event an edit
    # request's own `path` decline uses (repair_target.
    # record_request_declined), just keyed by `manifest` instead of
    # `path`. Checked here, same as _edit_request_fulfilled's own
    # decline check, so a declined install/scripts_change/
    # test_config_change request stops appearing in outstanding_requests
    # once resolved -- never submitted to any processor either way.
    if job_name is not None and task_id is not None:
        raw_manifest = request.get("manifest")
        if (isinstance(raw_manifest, str) and raw_manifest
                and request_declined(ledger, project, job_name, task_id, raw_manifest, after_seq)):
            return True
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


def _answers(ledger: "Ledger", request_id: object, proposal: str) -> bool:
    """An install run answers `proposal` unless it was approved for a
    different proposal (a request carrying no proposal answers any)."""
    if not isinstance(request_id, str):
        return True
    try:
        approved_for = ledger.request(request_id).payload.get("proposal")
    except LedgerError:
        return True
    return approved_for is None or approved_for == proposal


def outstanding_requests(ledger: "Ledger", project: str, pos: JobPosition | None, *,
                         repo: Path | None = None) -> tuple[dict, ...]:
    if pos is None:
        return ()
    return tuple(
        r for r in pos.pending_requests
        if not request_fulfilled(ledger, project, r, repo=repo,
                                  after_seq=proposal_seq(ledger, project, pos.attempt_id, r),
                                  proposal=proposal_id(ledger, project, pos, r),
                                  job_name=pos.job, task_id=pos.task_id)
    )


def pending_owner_requests(ledger: "Ledger", project: str, job: str, *,
                           repo: Path | None = None) -> tuple[dict, ...]:
    """The owner requests `job` is stopped on that are not fulfilled --
    the one computation every resume gate and `gb2 approve-install`
    reads. A request is fulfilled only by an engine run of its own kind
    after it was proposed, with the current manifest (when `repo` is
    given) still holding the change."""
    # Local import: this module is imported from job_state's own BOTTOM
    # (after load_position already exists there), so a module-level
    # import back of it here would only work in that one import order --
    # see this module's own docstring.
    from engine.application.job_state import load_position

    return outstanding_requests(ledger, project, load_position(ledger, project, job), repo=repo)


def hand_off_later_task_requests(ledger: "Ledger", project: str, job_name: str,
                                 pos: JobPosition, plan: object) -> None:
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


__all__ = [
    "proposal_seq", "proposal_id", "outstanding_requests", "pending_owner_requests",
    "hand_off_later_task_requests", "request_fulfilled",
]
