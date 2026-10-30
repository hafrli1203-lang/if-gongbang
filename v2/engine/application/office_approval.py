"""Office-approval I/O: the real secret-key file, the ledger-side
nonce record (replay protection), each approval kind's target_version
(what "the pending request" hashes to right now), and the two entry
points the AI office web server itself calls -- issue a signed token
for its own [approve] button, and list what is currently pending for
an app. All built on engine.domain.office_approval's pure signing/
verification; see that module's docstring for the token's field list
and verification order.

Layering: application, not domain, precisely because every function
here touches something domain code never may -- a file on disk (the
key), wall-clock time, a random nonce, or the ledger. engine.domain.
office_approval.verify_token stays pure and deterministic for a given
input; everything "real" (the current time, the actual key bytes,
whether a nonce was already spent, what is actually pending for a
job) is decided here and handed to it.

confirm_source="office" (engine.application.owner_approval.
approve_pending's third accepted value, alongside "tty"/"--yes") is
what a caller records once verify_office_token returns ok -- this
module never calls approve_pending itself; tools/gb2lib/approve_ops
.py's --office-token branches do, the same way their existing TTY
branches already call approve_pending with confirm_source="tty".

Snapshot pinning: a caller that already queried "what
is pending" once (approve_ops.py's `pending` list for install, or a
plan_digest it is about to act on for plan_edit) MUST pass that exact
value back in as `expected_target_version` instead of letting
verify_office_token recompute it from a SECOND, possibly-different-by-
now query. Every verify_office_token call site in this codebase does
this; compute_target_version/list_pending_office_approvals are the
only callers that still compute it fresh, because they have no
"pending" snapshot of their own to pin against.

Key location: the HMAC secret
key is NOT an operator-configurable path. It always lives at
`secrets_dir / "office-approval.key"` -- secrets_dir is already the
one directory every worker/reviewer/bench/installer/bootstrap sandbox
in this engine hides unconditionally (engine.application.steps.
worker_sandbox._hidden_roots, .suite_run's bench hidden tuple, engine.
application.steps.review's own hidden list, engine.interface.cli's
bootstrap hidden list, tools/gb2lib/workspace.py's installer hidden
list -- see each one's own docstring). A configurable path was the
root cause of several 1st-pass findings (a custom location sits
outside every one of those independently-maintained hidden-root
lists, and a sandbox builder that forgets to add it stays silently
vulnerable); fixing the location once, structurally, closes the whole
class of findings instead of chasing every sandbox call site one by
one."""
from __future__ import annotations

import hashlib
import os
import secrets
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING

from engine.config import EngineConfig
from engine.domain import ids
from engine.domain.office_approval import (
    APPROVAL_KINDS, DEFAULT_TTL_SECONDS,
    build_token as _build_token,
    verify_token as _verify_token,
)
from engine.domain.plan_model import plan_path
from engine.domain.plan_review import check_resumed_plan, is_plan_reviewed, plan_digest
from engine.domain.records import Event, LedgerError
from engine.domain.stage_checks import check_design_approved
from engine.application.job_state import STAGE_DESIGN_APPROVAL, load_position
from engine.application.job_state_requests import pending_owner_requests, proposal_id
from engine.application.owner_approval import PENDING_KIND_WHITELIST, redacted_change_preview

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

# The key's fixed filename inside secrets_dir -- see this module's own
# docstring ("Key location") for why the directory itself is never a
# configurable setting.
KEY_FILENAME = "office-approval.key"

# Only the owner (and this engine process, reading as the owner) may
# ever see the key file's bytes: an office-approval secret readable by
# ANY other account on the host is exactly as dangerous as one that
# does not exist, so both are refused the same way, before the bytes
# are ever read.
_MAX_PERMISSION_BITS = 0o600
_MAX_KEY_BYTES = 1 << 20  # 1 MiB -- generous for an HMAC key, small enough to refuse a decoy

_NONCE_AUDIT_EVENT_KIND = "office_approval.nonce_used"
# engine.domain.stage_checks._STATION_RECORD_TEMPLATES["design"] -- the
# REAL repo-relative subdirectories a design station's own record
# lives under, reused here rather than invented.
_DESIGN_ARTIFACT_SUBDIRS = ("design", "screens")


def resolve_key_path(config: EngineConfig) -> Path:
    """The office-approval key file's one, fixed, non-configurable
    path: `secrets_dir / "office-approval.key"`. See this module's own
    docstring ("Key location") for why this is a fixed filename inside
    the already-protected secrets_dir rather than an operator-set
    path."""
    return config.secrets_dir / KEY_FILENAME


def load_office_secret(config: EngineConfig) -> tuple[bytes | None, str]:
    """(secret_bytes, "ok") on success, or (None, <reason>) -- never
    raises.

    opens the path with O_NOFOLLOW (a symlink AT that
    exact path is refused outright -- opening it raises OSError) and
    then checks ownership/type/permissions via fstat() on that SAME
    open file descriptor, reading from that SAME fd afterward. This is
    deliberately NOT `path.stat()` followed by a separate `path.
    read_bytes()` (the previous, vulnerable shape): two separate calls
    re-resolve the path independently, so whoever can replace the
    file at that path between them (e.g. controls a parent directory)
    swaps in a DIFFERENT file -- attacker-chosen content that
    nonetheless passes the permission check done against the file
    seen a moment earlier. A single fd, opened once, keeps pointing at
    the original inode even if the path is later replaced, so every
    check below and the read after it all see the exact same bytes.

    Refused, all before any byte is read as "the secret":
      - missing, a symlink, or otherwise unopenable: "office_key_missing"
      - not a plain regular file (a FIFO, device, directory, ...):
        "office_key_not_regular_file"
      - owned by a different user than this process: "office_key_wrong_owner"
      - mode bits outside _MAX_PERMISSION_BITS (group/other has any
        permission, or the owner has execute): "office_key_permissions_too_open"
      - larger than _MAX_KEY_BYTES: "office_key_too_large"
      - empty file: "office_key_empty" -- an HMAC key of zero bytes
        signs nothing meaningful."""
    path = resolve_key_path(config)
    try:
        # O_NONBLOCK alongside O_NOFOLLOW: a FIFO opened O_RDONLY alone
        # blocks the WHOLE process until some other process opens the
        # same path for writing -- a plain "wrong file type" refusal
        # must never be able to hang this call. O_NONBLOCK has no
        # effect on a regular file's open() or the read() below, so
        # the happy path (an actual key file) is unchanged.
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, "office_key_missing"
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            return None, "office_key_not_regular_file"
        if file_stat.st_uid != os.geteuid():
            return None, "office_key_wrong_owner"
        mode_bits = stat.S_IMODE(file_stat.st_mode)
        if mode_bits & ~_MAX_PERMISSION_BITS:
            return None, "office_key_permissions_too_open"
        if file_stat.st_size > _MAX_KEY_BYTES:
            return None, "office_key_too_large"
        secret = os.read(fd, int(file_stat.st_size))
    except OSError:
        return None, "office_key_missing"
    finally:
        os.close(fd)
    if not secret:
        return None, "office_key_empty"
    return secret, "ok"


def _job_name_for_attempt(ledger: "Ledger", project: str, attempt_id: str) -> str:
    """job_name of the job.opened event that created this attempt_id.
    Every other module in this engine that needs this keeps its own
    tiny private copy rather than importing one across module
    boundaries (see e.g. engine.interface.cli._job_name_for_attempt's
    own docstring) -- this is that same, deliberately duplicated, few
    lines."""
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            return str(event.payload["job_name"])
    raise LedgerError(f"no job.opened event for attempt_id {attempt_id!r}")


def pending_install_requests(ledger: "Ledger", project: str, job_name: str,
                              repo: Path) -> list[dict]:
    """Every currently outstanding install/scripts_change/
    test_config_change request for this job, in pending_owner_
    requests' own order. Public (no leading underscore): approve_ops.
    py's --office-token branch calls this to query the batch exactly
    ONCE and then passes that SAME list to both install_target_version
    (what gets verified) and the actual approval loop (what gets
    approved) -- see this module's own docstring on snapshot pinning."""
    return [r for r in pending_owner_requests(ledger, project, job_name, repo=repo)
            if r.get("kind") in PENDING_KIND_WHITELIST]


def pinned_install_batch(ledger: "Ledger", project: str, job_name: str,
                          pending: list[dict]) -> list[dict]:
    """[{"request": ..., "proposal": ...}, ...] for `pending` (a list a
    caller already queried -- never re-queried here), each entry's
    `proposal` computed ONCE via engine.application.job_state_requests.
    proposal_id against THIS job's CURRENT JobPosition (load_position,
    called exactly once here, not once per caller). None if there is no
    recorded position at all (e.g. a test building a request by hand).

    approve_ops.py's install approval
    loop used to look up EACH request's proposal_id itself, a SECOND,
    independent call -- if a station finished in between this batch's
    own lookup (install_target_version, what got SIGNED) and that
    second one (what actually got SUBMITTED), the two could disagree
    (reproduced: signed `attempt:3`, submitted `attempt:6`). Returning
    the (request, proposal) PAIRS here, for the caller to submit
    VERBATIM, means there is only ever one lookup, shared by both the
    hash that gets signed and the submission that gets queued."""
    pos = load_position(ledger, project, job_name)
    return [
        {"request": request,
         "proposal": (proposal_id(ledger, project, pos, request) if pos is not None else None)}
        for request in pending
    ]


def install_target_version_from_batch(batch: list[dict]) -> str:
    """ids.content_hash over an ALREADY-PINNED (request, proposal) batch
    (pinned_install_batch) -- the one place this hash is actually
    computed, so a caller that pins its own batch and a caller that
    just wants the fresh hash (compute_target_version) can never drift
    apart on the algorithm."""
    return ids.content_hash(batch)


def install_target_version(ledger: "Ledger", project: str, job_name: str,
                            pending: list[dict]) -> str:
    """install_target_version_from_batch(pinned_install_batch(...)) in
    one call, for a caller with no pinned batch of its own to reuse
    (compute_target_version, list_pending_office_approvals, tests) --
    see pinned_install_batch's own docstring for why a caller that DOES
    go on to submit requests (approve_ops.py) must call that function
    directly instead and reuse its exact batch, rather than this
    wrapper."""
    return install_target_version_from_batch(
        pinned_install_batch(ledger, project, job_name, pending))


class DesignArtifactUnreadable(ValueError):
    """Raised by design_target_version (via _design_artifact_digests)
    when a file under .gongbang/<job>/{design,screens}/ cannot be
    enumerated or read. design_target_version must never treat a read
    failure as if that file did not exist or were empty: an owner approved some REAL content; silently
    hashing nothing for a now-unreadable file would let a token signed
    against the ORIGINAL version keep verifying against whatever
    broken/replaced file sits there at verify time. A ValueError
    subclass so every existing `except ValueError` in this module
    (verify_office_token, issue_office_approval_token) already handles
    it correctly -- str(exc) is the reason string."""


def _raise_onerror(exc: OSError) -> None:
    raise exc


def _list_files_deep_sorted(base: Path) -> list[Path]:
    """Every file under `base`, at any depth, sorted. Raises OSError
    (never swallows it) if ANY directory in the tree cannot be listed.

    `Path.rglob`/`glob` is deliberately NOT used
    here -- on this Python version, pathlib's own recursive walk
    silently SKIPS a subdirectory it cannot os.scandir (a
    PermissionError enumerating it never reaches the caller at all, it
    just yields fewer files), so wrapping the rglob call itself in
    `try/except OSError` catches nothing real: the exception is
    swallowed one layer further down, inside pathlib, before this
    function's own try ever sees it. Reproduced: sign a token against
    an EMPTY design/ dir, then make a LATER enumeration of that same
    directory fail (a file now exists there, but listing raises) --
    rglob-based code still returns {} (as if still empty), and the
    stale token keeps verifying as (True, "ok"). os.walk(onerror=...)
    does not have this gap: the onerror callback receives the REAL
    OSError and re-raises it, propagating out of the walk entirely."""
    paths: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(base, onerror=_raise_onerror):
        for name in filenames:
            paths.append(Path(dirpath) / name)
    return sorted(paths)


def _design_artifact_digests(repo: Path, job_name: str) -> dict[str, str]:
    """{repo-relative file path -> sha256 hex} for every file under
    .gongbang/<job>/design/ and .gongbang/<job>/screens/ (the real
    design-station record paths, _DESIGN_ARTIFACT_SUBDIRS). {} if
    neither directory exists. Raises DesignArtifactUnreadable if any
    file in either directory cannot be enumerated (e.g. a permission
    error walking the tree, see _list_files_deep_sorted's own
    docstring) or read -- see DesignArtifactUnreadable's own docstring
    for why this must never fall back to treating the file as empty."""
    out: dict[str, str] = {}
    for subdir in _DESIGN_ARTIFACT_SUBDIRS:
        base = repo / ".gongbang" / job_name / subdir
        try:
            if not base.is_dir():
                continue
            file_paths = [p for p in _list_files_deep_sorted(base) if p.is_file()]
        except OSError as exc:
            raise DesignArtifactUnreadable("design_artifact_unreadable") from exc
        for file_path in file_paths:
            try:
                data = file_path.read_bytes()
            except OSError as exc:
                raise DesignArtifactUnreadable("design_artifact_unreadable") from exc
            rel = file_path.relative_to(repo).as_posix()
            out[rel] = hashlib.sha256(data).hexdigest()
    return out


def design_target_version(repo: Path, job_name: str) -> str:
    """plan_digest(plan.json) folded together with every file under
    this job's design/screens record (_design_artifact_digests), so a
    design-approval token goes stale the instant EITHER the plan OR
    the actual design deliverable (design.md, a comp, a screenshot)
    changes -- not only the plan (the previous version
    hashed plan.json alone, so a swapped comp/screenshot under an
    UNCHANGED plan still verified against a stale token)."""
    hasher = hashlib.sha256(plan_digest(plan_path(repo, job_name)).encode("utf-8"))
    for rel_path, digest in sorted(_design_artifact_digests(repo, job_name).items()):
        hasher.update(f"\n{rel_path}:{digest}".encode("utf-8"))
    return hasher.hexdigest()


def plan_edit_target_version(repo: Path, job_name: str) -> str:
    """plan_digest(plan.json) alone -- what a plan_edit approval is
    actually re-blessing (engine.domain.plan_review.
    record_owner_plan_amend compares against this same digest)."""
    return plan_digest(plan_path(repo, job_name))


def design_approval_still_matches(ledger: "Ledger", project: str, job_name: str,
                                   repo: Path) -> bool:
    """True iff the latest design.approved event's own recorded
    `target_version` (the exact value cmd_approve_design pinned and
    stored at approval time -- see that function's docstring; never
    re-derived here) still equals design_target_version(repo, job_name)
    computed RIGHT NOW.

    the previous fix verified the
    design content ONCE, at approval time, but recorded nothing from
    that verification -- a later RESUME point (engine.application.
    pipeline_resume) trusted check_design_approved's bare presence
    check alone, so a design.md/comp/screenshot swapped AFTER approval
    (but before the job actually resumed building) went undetected.
    The fix: store what was approved (cmd_approve_design does), and
    have the resume point compare against it (this function), fail
    closed on any mismatch.

    True (never refuses) when the latest design.approved event has no
    `target_version` field at all -- a ledger from before this field
    existed recorded nothing to compare against, and this check must
    never retroactively invalidate an approval that already happened
    under the OLD contract. True (vacuously) when there is no design.
    approved event at all -- "has this job's design approval gone
    stale" does not apply before an approval exists; check_design_
    approved itself is still what gates on that separately."""
    recorded_target_version: str | None = None
    for _seq, event in ledger.events(project, "design.approved"):
        if event.payload.get("job") == job_name:
            recorded = event.payload.get("target_version")
            recorded_target_version = recorded if isinstance(recorded, str) else None
    if recorded_target_version is None:
        return True
    return recorded_target_version == design_target_version(repo, job_name)


def _design_actually_pending(ledger: "Ledger", project: str, job_name: str) -> bool:
    """True iff this job is genuinely stopped waiting for a design
    approval right now -- engine.application.job_state.
    STAGE_DESIGN_APPROVAL -- AND the plan has been reviewed AND design
    is not already approved. A design
    token only ever verify while a real design approval is pending,
    not merely "the plan was reviewed and design.approved happens to
    be absent" (vacuously true before a design station ever ran, or
    long after the job moved past it on a different path)."""
    if check_design_approved(ledger, project, job_name):
        return False
    if not is_plan_reviewed(ledger, project, job_name):
        return False
    pos = load_position(ledger, project, job_name)
    return pos is not None and pos.stage == STAGE_DESIGN_APPROVAL


# Public alias for _design_actually_pending -- engine.application.
# standing_approval (owner standing approval) needs this exact "is a design
# approval genuinely pending right now" check before it may append a
# design.approved event on the owner's behalf, and must not reach for
# another module's leading-underscore name to get it (same cross-
# module rule owner_approval.redacted_change_preview's own public door
# documents). The function itself stays named/defined once, here.
design_actually_pending = _design_actually_pending


def compute_target_version(config: EngineConfig, ledger: "Ledger", app: str,
                            job_name: str, kind: str) -> str:
    """The FRESH sha256 identity of whatever `kind` of pending request
    is waiting on the owner for (app, job_name) right now -- queries
    everything itself. Used only by callers with no snapshot of their
    own to pin against (issue_office_approval_token,
    list_pending_office_approvals): a caller that already has one (the
    three --office-token CLI branches) must call install_target_version/
    design_target_version/plan_edit_target_version directly against
    that SAME snapshot instead -- see this module's own docstring on
    snapshot pinning.

    Raises ValueError("kind_not_whitelisted") for any other kind."""
    if kind not in APPROVAL_KINDS:
        raise ValueError("kind_not_whitelisted")
    repo = config.projects[app].repo
    if kind == "install":
        pending = pending_install_requests(ledger, app, job_name, repo)
        return install_target_version(ledger, app, job_name, pending)
    if kind == "design":
        return design_target_version(repo, job_name)
    return plan_edit_target_version(repo, job_name)


def verify_office_token(config: EngineConfig, ledger: "Ledger", app: str,
                         job_name: str, attempt_id: str, kind: str,
                         token: object, *, now: float | None = None,
                         expected_target_version: str | None = None) -> tuple[bool, str]:
    """(True, "ok") iff `token` is a valid, UNUSED office-approval
    document for exactly this (app, job_name, attempt_id, kind), and
    the approval target it names is still the one actually pending.

    `expected_target_version`: pass the value a caller already pinned
    from its own snapshot (install_target_version/design_target_
    version/plan_edit_target_version, called against the SAME
    `pending` list / file read the caller is about to act on) --
    `None` (the default, for a caller with no snapshot of its own)
    falls back to compute_target_version's fresh, independent query.

    Checked in this order:
      1. the secret key file itself (load_office_secret) -- missing or
         too-open, refused before the token is even looked at
      2. the token's own nonce field is present and well-formed
      3. kind="design" only: the job is genuinely stopped awaiting a
         design approval right now (_design_actually_pending)
      4. engine.domain.office_approval.verify_token itself (shape,
         signature, expiry, and that app/job_name/attempt_id/kind/
         target_version all match)
      5. the nonce is atomically consumed (Ledger.
         consume_office_approval_nonce). This is the
         LAST check and the only one with a side effect: a token that
         fails any earlier check never spends its nonce, so a bad
         first attempt (e.g. a transient target_version mismatch)
         does not burn the office server's only chance to retry with
         the SAME, still/again-valid token. Because it is atomic and
         is the single source of truth for "already used" (not a
         read-then-write), two concurrent verify_office_token calls
         for the identical token can never both return True.

    (deliberate scope boundary, not fixed as "one SQL
    transaction"): nonce consumption above and the CALLER's actual
    approval action (owner_approval.approve_pending / record_owner_
    plan_amend / a design.approved append, each in a DIFFERENT module,
    each already multi-step on its own -- Processor.submit included)
    cannot practically share one literal database transaction without
    reworking those modules far beyond this feature. A crash in that
    narrow window burns the ONE token already consumed but never loses
    the approval itself: nothing pending has changed, so the office
    server mints a FRESH token (a new nonce, same target_version) and
    retries -- see tests/unit/test_office_approval.py::
    test_a_burned_nonce_is_recoverable_with_a_freshly_issued_token for
    a worked example of exactly that recovery."""
    secret, reason = load_office_secret(config)
    if secret is None:
        return False, reason
    if not isinstance(token, dict):
        return False, "token_not_dict"
    nonce = token.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return False, "field_invalid:nonce"

    if kind == "design" and not _design_actually_pending(ledger, app, job_name):
        return False, "design_not_pending"

    if expected_target_version is not None:
        target_version = expected_target_version
    else:
        try:
            target_version = compute_target_version(config, ledger, app, job_name, kind)
        except ValueError as exc:
            # compute_target_version itself raises ValueError("kind_not_
            # whitelisted") for an unknown kind; design_target_version
            # (via DesignArtifactUnreadable, a ValueError subclass) can
            # also raise from in here for kind="design" -- str(exc) is
            # ALREADY the right reason code for either case (never the
            # secret, never anything but a fixed message), so there is
            # no reason to collapse both into one hardcoded string.
            return False, str(exc)

    ok, verify_reason = _verify_token(
        secret, token, expected_app=app, expected_job_name=job_name,
        expected_attempt_id=attempt_id, expected_kind=kind,
        expected_target_version=target_version,
        now=int(now if now is not None else time.time()),
    )
    if not ok:
        return False, verify_reason

    if not ledger.consume_office_approval_nonce(app, nonce):
        return False, "nonce_reused"
    # Audit-only record (never the gate itself -- consume_office_approval_
    # nonce's atomic table INSERT above already is): lets an owner/report
    # reading the ledger see WHICH kind/attempt an office nonce answered,
    # the same way 'owner.approved'.confirm_source already distinguishes
    # tty from office for the approval itself.
    ledger.append(Event(
        project=app, kind=_NONCE_AUDIT_EVENT_KIND, actor_kind="owner",
        actor_id="office", attempt_id=attempt_id,
        payload={"nonce": nonce, "approval_kind": kind},
    ))
    return True, "ok"


def issue_office_approval_token(config: EngineConfig, ledger: "Ledger", app: str,
                                 job_name: str, attempt_id: str, kind: str, *,
                                 ttl_seconds: int = DEFAULT_TTL_SECONDS,
                                 now: float | None = None) -> dict[str, object]:
    """What the AI office server calls to mint a signed approval
    document for its own [approve] button: reads the real secret key
    file, computes the CURRENT target_version for `kind`
    (compute_target_version), picks a fresh random nonce (secrets.
    token_hex, 128 bits), and signs everything with engine.domain.
    office_approval.build_token.

    Raises ValueError(reason) if the key file itself is missing or too
    permissive ("office_key_missing"/"office_key_not_regular_file"/
    "office_key_wrong_owner"/"office_key_permissions_too_open"/
    "office_key_too_large"/"office_key_empty"), `kind` is not one of
    APPROVAL_KINDS ("kind_not_whitelisted"), or (kind="design" only) a
    file under .gongbang/<job>/{design,screens}/ cannot be enumerated
    or read (DesignArtifactUnreadable, a ValueError subclass,
    "design_artifact_unreadable") -- the office server must surface
    any of these as a real setup problem, never silently fall back to
    an unsigned approval.

    Example (the office server's own call):
        token = issue_office_approval_token(
            config, ledger, "demo-app", "gb2-change-123", attempt_id,
            "install",
        )
        json.dump(token, open("/tmp/office-token.json", "w"))
        # then, on the engine host:
        #   gb2 approve-install demo-app --office-token /tmp/office-token.json
    """
    secret, reason = load_office_secret(config)
    if secret is None:
        raise ValueError(reason)
    if kind not in APPROVAL_KINDS:
        raise ValueError("kind_not_whitelisted")
    target_version = compute_target_version(config, ledger, app, job_name, kind)
    issued_at = int(now if now is not None else time.time())
    nonce = secrets.token_hex(16)
    return _build_token(
        secret, app=app, job_name=job_name, attempt_id=attempt_id, kind=kind,
        target_version=target_version, issued_at=issued_at, nonce=nonce,
        ttl_seconds=ttl_seconds,
    )


def _pending_design_entry(config: EngineConfig, ledger: "Ledger", app: str,
                           job_name: str, attempt_id: str,
                           repo: Path) -> dict[str, object] | None:
    if not _design_actually_pending(ledger, app, job_name):
        return None
    return {
        "kind": "design", "job_name": job_name, "attempt_id": attempt_id,
        "target_version": design_target_version(repo, job_name),
        "request_preview": {"job": job_name},
    }


def _pending_install_entry(ledger: "Ledger", app: str, job_name: str,
                            attempt_id: str, repo: Path) -> dict[str, object] | None:
    pending = pending_install_requests(ledger, app, job_name, repo)
    if not pending:
        return None
    return {
        "kind": "install", "job_name": job_name, "attempt_id": attempt_id,
        "target_version": install_target_version(ledger, app, job_name, pending),
        "request_preview": [
            {"manifest": r.get("manifest"), "change": redacted_change_preview(r.get("change"))}
            for r in pending
        ],
    }


def _pending_plan_edit_entry(ledger: "Ledger", app: str, job_name: str,
                              attempt_id: str, repo: Path) -> dict[str, object] | None:
    if not is_plan_reviewed(ledger, app, job_name):
        return None
    problems = check_resumed_plan(ledger, app, job_name, repo)
    if "plan_changed_since_review" not in problems:
        return None
    return {
        "kind": "plan_edit", "job_name": job_name, "attempt_id": attempt_id,
        "target_version": plan_edit_target_version(repo, job_name),
        "request_preview": {"job": job_name, "problems": sorted(problems)},
    }


def list_pending_office_approvals(config: EngineConfig, ledger: "Ledger",
                                   app: str) -> list[dict[str, object]]:
    """A stable, JSON-ready list of {"kind", "job_name", "attempt_id",
    "target_version", "request_preview"} entries -- one per approval
    kind currently waiting on the owner for `app`'s open job, [] if no
    job is open or nothing is waiting. What the AI office server reads
    to render its pending-approval screen (E2): `request_preview`
    never carries a secret value -- for "install" it is section/key
    NAMES only (engine.application.owner_approval.
    redacted_change_preview, reused rather than re-derived, the exact
    shape 'owner.approved'.request_preview already persists to the
    ledger); for "design" just the job name; for "plan_edit" the job
    name plus engine.domain.plan_review.check_resumed_plan's own
    problem codes (never a scene file's content, only which problem
    fired). Read-only: recomputed fresh on every call, nothing here is
    itself persisted."""
    project_row = ledger.project(app)
    attempt_id = project_row.current_attempt_id
    if attempt_id is None:
        return []
    try:
        job_name = _job_name_for_attempt(ledger, app, attempt_id)
    except LedgerError:
        return []
    repo = config.projects[app].repo

    out: list[dict[str, object]] = []
    for entry in (
        _pending_install_entry(ledger, app, job_name, attempt_id, repo),
        _pending_design_entry(config, ledger, app, job_name, attempt_id, repo),
        _pending_plan_edit_entry(ledger, app, job_name, attempt_id, repo),
    ):
        if entry is not None:
            out.append(entry)
    return out


__all__ = [
    "KEY_FILENAME", "resolve_key_path", "load_office_secret",
    "pending_install_requests", "pinned_install_batch", "install_target_version_from_batch",
    "install_target_version", "DesignArtifactUnreadable", "design_target_version",
    "plan_edit_target_version", "design_approval_still_matches", "compute_target_version",
    "verify_office_token", "issue_office_approval_token", "list_pending_office_approvals",
    "design_actually_pending",
]
