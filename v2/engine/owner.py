"""Owner-decision boundary: the code gate between "a station asked the
owner for something" (orbit.JobOutcome.pending_requests, a `requests`
entry off a `station.finished` event) and actually submitting that
request to the processor.

Today (2단계) the test harness calls `processor.submit` on a pending
request directly (tests/acceptance/test_15_dependency_external.py) --
that is a stand-in for a human reading the request and deciding. In real
operation nothing may reach `submit` without passing `validate_pending`
first, and `approve_pending` is the only path that is allowed to combine
"validated" with "actually queue it": the CLI (Task 1) collects the
human's explicit confirmation and passes `confirm=True`; this module
never reads stdin or any other input itself, so a caller cannot satisfy
the gate just by calling it twice.

Reusing the step's own validation (not duplicating its rules): the
payload shape mirrors engine.steps.install's contract exactly (plan
docs, engine/steps/__init__.py) -- {"manifest": str, "change": {...}} --
and a line-list manifest's `change` is checked with
engine.installers.validate_add_lines, the very function run_installer
itself calls, so a request this module approves is, by construction,
one install.py would also accept.
"""
from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

from engine import ids
from engine.config import EngineConfig
from engine.installers import InstallFailed, validate_add_lines
from engine.ledger import Event, Ledger
from engine.policy import _SHELL_METACHARS
from engine.processor import submit

# Only these kinds may ever reach approve_pending -- data, not scattered
# ifs, so adding a new owner-approvable kind is a one-line change here
# (and, deliberately, nowhere else: a kind not in this set is rejected
# before any shape/path checking even runs).
PENDING_KIND_WHITELIST: frozenset[str] = frozenset(
    {"install", "scripts_change", "test_config_change"}
)

# engine.steps.install's payload contract for all three whitelisted
# kinds (engine/steps/__init__.py docstring): exactly these two keys,
# nothing else -- an extra key is a sign the request was not built by
# the station protocol this gate expects.
_INSTALL_PAYLOAD_KEYS: frozenset[str] = frozenset({"manifest", "change"})
# engine.position.proposal_id: "<attempt_id>:<station.finished seq>".
_PROPOSAL_ID = re.compile(r"[A-Za-z0-9_#.-]{1,120}:[0-9]{1,12}")

_MAX_REQUEST_BYTES = 16 * 1024
_PREVIEW_MAX_CHARS = 200


def _iter_strings(value: object):
    """Yield every string leaf inside a JSON-like value (dict/list/str),
    so path/shell checks below cover nested fields too, not just the
    top level. Dict KEYS are yielded too, not just values -- a key like
    "$(...)" or "../x" is just as capable of carrying a path-traversal
    or shell-metacharacter payload as any value, and a request shaped
    with unsafe data only in a key would otherwise sail through
    unchecked."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(sub)
    elif isinstance(value, list):
        for sub in value:
            yield from _iter_strings(sub)


def _unsafe_string_reason(text: str) -> str | None:
    """None if `text` is a safe field value; else a short reason code."""
    if Path(text).is_absolute():
        return "absolute_path"
    if ".." in text:
        return "path_traversal"
    if "~" in text:
        return "home_reference"
    # BOM and other invisible/control characters could hide a traversal
    # or metacharacter from the checks above while still being
    # interpreted by a shell or filesystem later -- reject any string
    # carrying a non-printable character outright.
    if any(not ch.isprintable() and ch not in ("\t",) for ch in text):
        return "unsafe_character"
    if any(ch in text for ch in _SHELL_METACHARS):
        return "shell_metachar"
    return None


# A JSON manifest's `change` (install kind only -- scripts_change/
# test_config_change legitimately write other sections, e.g. "scripts",
# of the same package.json and are NOT restricted by this allowlist)
# may only touch these top-level sections. Rejecting "scripts" here
# closes a real exploit: an "install" request whose `change` was really
# `{"scripts": {"postinstall": "<shell>"}}` would otherwise validate as
# an ordinary dependency bump and then run through the unsandboxed
# RealInstaller.
_JSON_INSTALL_SECTIONS = frozenset(
    {"dependencies", "devDependencies", "optionalDependencies"}
)
# npm package name: optional "@scope/" prefix, then lowercase
# alphanumerics/._- (conservative; real npm allows a few more corner
# cases, but this is a validator, not a registry mirror).
_PACKAGE_NAME_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
# A version SPECIFIER's allowed character set, not a full semver-range
# grammar: deliberately excludes ":" and "/", so none of "file:",
# "git+ssh://", "http://", "link:", "workspace:" can ever match --
# all of them need at least one of those two characters.
_VERSION_SPECIFIER_RE = re.compile(r"^[A-Za-z0-9.^~<>=*x+,|\- ]+$")


def _validate_json_install_change(change: dict) -> tuple[bool, str]:
    """Only for kind="install" against a .json manifest. Returns
    (True, "ok") or (False, <constant reason code -- never the
    offending section/name/value itself, which could be an attacker's
    payload or carry a secret>)."""
    if not change:
        return False, "manifest_change_empty"
    for section, packages in change.items():
        if section not in _JSON_INSTALL_SECTIONS:
            return False, "manifest_section_not_allowed"
        if not isinstance(packages, dict) or not packages:
            return False, "manifest_section_not_dict"
        for name, version in packages.items():
            if not isinstance(name, str) or not _PACKAGE_NAME_RE.match(name):
                return False, "manifest_package_name_invalid"
            if not isinstance(version, str) or not _VERSION_SPECIFIER_RE.match(version):
                return False, "manifest_version_invalid"
    return True, "ok"


def _redacted_change_preview(change: object) -> object:
    """Section names and, inside each section, key names only -- never
    a value. A value could be an attacker's exploit payload (a shell
    metacharacter string that was REJECTED, but still should not be
    echoed back into an audit trail) or a secret (an authToken, a
    credential embedded in a dependency URL that WAS accepted). Used
    for 'owner.approved.request_preview' -- a persisted ledger event
    other code may read, unlike engine.cli's own stderr printout of
    the full raw request (shown only to the human approving it, in
    their own terminal, so they can judge it before confirming)."""
    if not isinstance(change, dict):
        return "<non-dict>"
    out: dict[str, object] = {}
    for section, body in change.items():
        if isinstance(body, dict):
            out[str(section)] = sorted(str(k) for k in body.keys())
        else:
            out[str(section)] = "<non-dict>"
    return out


def validate_pending(config: EngineConfig, project: str,
                      request: dict) -> tuple[bool, str]:
    """Accept-or-reject a single `requests` entry a station asked the
    owner to submit. Never touches the ledger -- a pure check so
    approve_pending (and tests, and a CLI's preview) can call it freely.

    Returns (True, "ok") or (False, <snake_case reason>)."""
    if not isinstance(request, dict):
        return False, "request_not_dict"
    try:
        # UTF-8 BYTES, not Python characters: a non-ASCII payload (e.g.
        # several thousand CJK characters) can be several times more
        # bytes than characters, and the byte count is what actually
        # bounds anything downstream (the ledger event, a wire
        # transfer) that this limit is meant to protect.
        size = len(json.dumps(request, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return False, "request_not_json_serializable"
    if size > _MAX_REQUEST_BYTES:
        return False, "request_too_large"

    kind = request.get("kind")
    if kind not in PENDING_KIND_WHITELIST:
        return False, "kind_not_whitelisted"

    payload = {key: value for key, value in request.items() if key != "kind"}
    if set(payload) != _INSTALL_PAYLOAD_KEYS:
        return False, "payload_shape_invalid"

    manifest = payload.get("manifest")
    if not isinstance(manifest, str):
        return False, "manifest_not_string"

    # Path/shell safety is checked on every string field (including
    # `manifest` itself) BEFORE comparing it against the project's
    # configured manifest: an unsafe spelling is unsafe regardless of
    # which file it claims to be, and a precise reason (absolute_path,
    # path_traversal, ...) is more useful than a blanket mismatch.
    for text in _iter_strings(payload):
        reason = _unsafe_string_reason(text)
        if reason is not None:
            return False, reason

    project_config = config.projects.get(project)
    if project_config is None:
        return False, "unregistered_project"
    configured_manifest = project_config.manifest
    if configured_manifest is None:
        return False, "manifest_not_configured"
    if posixpath.normpath(manifest) != posixpath.normpath(configured_manifest):
        return False, "manifest_mismatch"

    change = payload.get("change")
    if not isinstance(change, dict):
        return False, "change_not_dict"

    if manifest.endswith(".json"):
        if kind == "install":
            # scripts_change/test_config_change legitimately touch other
            # sections (e.g. "scripts") of the same package.json and are
            # not restricted by _JSON_INSTALL_SECTIONS -- only "install"
            # (which should only ever be a dependency bump) is.
            return _validate_json_install_change(change)
    else:
        # Line-list manifest (e.g. requirements.txt): reuse the
        # installer's own line validator -- the same function
        # engine.installers.RealInstaller.apply runs -- instead of
        # re-deriving the allowed requirement-line grammar here. The
        # reason is a constant code, never the rejected line itself
        # (which could carry a secret, e.g. a credential embedded in a
        # VCS/URL requirement).
        try:
            validate_add_lines(change)
        except InstallFailed:
            return False, "invalid_line:rejected"

    return True, "ok"


def approve_pending(config: EngineConfig, ledger: Ledger, project: str,
                     attempt_id: str, request: dict, *, confirm: bool,
                     confirm_source: str = "unknown", proposal: str | None = None) -> str:
    """Owner input: approve one pending request and queue it.

    `confirm` must be True -- the caller (a CLI) is responsible for
    asking the human and passing their explicit yes; this function never
    reads stdin or any other input on its own, so nothing can satisfy
    the gate except that explicit caller-side confirmation.
    `confirm_source` records HOW that confirmation was obtained ("tty"
    or "--yes" from engine.cli's cmd_approve) in the 'owner.approved'
    event, and IS checked for "scripts_change"/"test_config_change":
    those two kinds' `change` carries executable strings by nature (a
    shell command, test-runner config) that validate_pending cannot
    make safe by shape alone, so only an interactive ("tty") confirm
    is accepted for them -- raises ValueError
    ("kind_requires_interactive_confirm") otherwise, even with
    confirm=True. "install" has no such restriction (its `change` is
    already shape-restricted to dependency sections).

    Validates with validate_pending(); on failure raises
    ValueError(reason), appends nothing, submits nothing. On success
    calls processor.submit with actor_id="owner" FIRST, then appends an
    'owner.approved' event (kind, a sha256 of the submitted payload, a
    short preview -- no secrets, since the payload is exactly what the
    whitelist already allowed -- and confirm_source). This order (submit
    before the approval event, not after) means an exception from submit
    itself (e.g. the processor rejecting the kind/payload for a reason
    validate_pending does not check) never leaves behind an orphan
    'owner.approved' event claiming something was approved that was
    never actually queued. Approving the same request twice is safe:
    submit() is idempotent on (project, attempt_id, kind, payload), so
    the second call returns the same request_id and simply appends a
    second 'owner.approved' event.
    """
    if not confirm:
        raise ValueError("not_confirmed")
    ok, reason = validate_pending(config, project, request)
    if not ok:
        raise ValueError(reason)

    kind = str(request["kind"])
    # scripts_change/test_config_change carry executable strings by
    # nature (shell commands, test-runner config) -- unlike install's
    # change (restricted to dependency sections, see
    # _validate_json_install_change), validate_pending has no way to
    # make these kinds safe by shape alone. A human must read the
    # actual content on a terminal before confirming, so --yes (a
    # non-interactive confirm_source) is only ever valid for "install".
    if kind != "install" and confirm_source != "tty":
        raise ValueError("kind_requires_interactive_confirm")

    payload = {key: value for key, value in request.items() if key != "kind"}
    if proposal is not None:
        # The proposal this approval answers (engine.position.proposal_id)
        # is part of the request's identity, so an identical proposal made
        # again gets its own request instead of the earlier completed one.
        if not _PROPOSAL_ID.fullmatch(proposal):
            raise ValueError("proposal_id_invalid")
        payload["proposal"] = proposal
    # kind + manifest + section/key NAMES only -- never a value (see
    # _redacted_change_preview's docstring for why this, not the raw
    # payload, is what a persisted ledger event carries).
    safe_preview = {
        "manifest": payload.get("manifest"),
        "change": _redacted_change_preview(payload.get("change")),
    }
    preview = json.dumps(safe_preview, ensure_ascii=False,
                         sort_keys=True)[:_PREVIEW_MAX_CHARS]

    request_id = submit(ledger, project, attempt_id, kind, payload, "owner")
    ledger.append(Event(
        project=project, kind="owner.approved", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={
            "kind": kind,
            "payload_sha256": ids.content_hash(payload),
            "request_preview": preview,
            "confirm_source": confirm_source,
        },
    ))
    return request_id


__all__ = ["PENDING_KIND_WHITELIST", "validate_pending", "approve_pending"]
