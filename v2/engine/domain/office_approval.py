"""Office-approval tokens: the HMAC-signed, nonce-bound documents that
let AI office (a separate web app) approve a design/install/plan_edit
request with its own [approve] button, without ever posing as a TTY.

Pure here on purpose -- no file I/O, no wall clock, no ledger, no
randomness. `build_token`/`verify_token` take `issued_at`/`now`/`nonce`
as plain caller-supplied values, so a test can build a fixed, fully
deterministic token without faking time.time()/os.urandom, and so this
module can never itself read the wrong key file or disagree with the
application layer about what time it is. engine.application.
office_approval is the thin, impure layer around this: it reads the
real secret key file (checking its permissions first), picks a real
nonce and timestamp, computes each approval kind's real target_version
from the ledger/repo, and checks a nonce has not already been spent --
none of which this module may do.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Mapping

# The three request kinds the office approval button is defined to
# bless. Elsewhere in the engine "install" also covers the TTY-only
# scripts_change/test_config_change kinds (engine.application.
# owner_approval.PENDING_KIND_WHITELIST) -- that restriction is NOT
# re-decided here; a token with kind="install" still has to pass
# approve_pending's own confirm_source check downstream, which keeps
# refusing scripts_change/test_config_change for any confirm_source
# other than "tty", office included.
APPROVAL_KINDS: frozenset[str] = frozenset({"design", "install", "plan_edit"})

# The office approval request document expires if not verified within
# this many seconds of being issued -- short enough that a token
# leaked or logged somewhere is useless well before anyone could reuse
# it, long enough that a human looking at the office UI before
# clicking [approve] is never rushed.
DEFAULT_TTL_SECONDS = 600

# A small allowance for clock drift between the office server's own
# clock and this engine host's -- a token whose issued_at is a few
# seconds "in the future" because of ordinary drift is still a
# legitimate, freshly-issued token, not an attack; one actually far in
# the future is refused as "issued_in_future".
CLOCK_SKEW_TOLERANCE_SECONDS = 60

# Every field the signature covers, in a fixed order -- "sig" itself
# is deliberately excluded (it is computed FROM these, so including it
# would make the signature depend on itself).
_SIGNED_FIELDS: tuple[str, ...] = (
    "app", "job_name", "attempt_id", "kind", "target_version",
    "issued_at", "nonce", "ttl_seconds",
)

_STRING_FIELDS: tuple[str, ...] = (
    "app", "job_name", "attempt_id", "kind", "target_version", "nonce",
)
_INT_FIELDS: tuple[str, ...] = ("issued_at", "ttl_seconds")


def _canonical_bytes(fields: Mapping[str, object]) -> bytes:
    """The exact bytes signed/verified: every _SIGNED_FIELDS entry,
    sorted keys, compact separators -- the same bytes for the same
    content regardless of dict insertion order, so build_token and
    verify_token can never disagree over whitespace or key order."""
    body = {key: fields[key] for key in _SIGNED_FIELDS}
    return json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def compute_signature(secret: bytes, fields: Mapping[str, object]) -> str:
    """hex HMAC-SHA256 of `fields`' canonical bytes, keyed by `secret`
    -- the one place both build_token and verify_token compute a
    signature, so they can never drift apart on the algorithm."""
    return hmac.new(secret, _canonical_bytes(fields), hashlib.sha256).hexdigest()


def build_token(secret: bytes, *, app: str, job_name: str, attempt_id: str,
                 kind: str, target_version: str, issued_at: int, nonce: str,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, object]:
    """A signed office-approval document naming exactly one (app,
    job_name, attempt_id, kind, target_version). Raises ValueError if
    `kind` is not one of APPROVAL_KINDS -- a caller must never be able
    to mint a token for a kind this module does not know how to
    verify. `issued_at`/`nonce` are supplied by the caller (the
    application layer picks real wall-clock time and a fresh random
    nonce); this function stays pure and deterministic for a given
    input."""
    if kind not in APPROVAL_KINDS:
        raise ValueError(f"unknown approval kind: {kind!r}")
    fields: dict[str, object] = {
        "app": app, "job_name": job_name, "attempt_id": attempt_id,
        "kind": kind, "target_version": target_version,
        "issued_at": int(issued_at), "nonce": nonce,
        "ttl_seconds": int(ttl_seconds),
    }
    return {**fields, "sig": compute_signature(secret, fields)}


def _shape_problem(token: Mapping[str, object]) -> str | None:
    """None iff `token` carries every _SIGNED_FIELDS key plus "sig",
    each the right type -- else the first reason code found, checked
    in a fixed field order so the same malformed token always reports
    the same reason."""
    for key in (*_SIGNED_FIELDS, "sig"):
        if key not in token:
            return f"field_missing:{key}"
    for key in (*_STRING_FIELDS, "sig"):
        if not isinstance(token[key], str) or not token[key]:
            return f"field_invalid:{key}"
    for key in _INT_FIELDS:
        value = token[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return f"field_invalid:{key}"
    return None


def verify_token(secret: bytes, token: object, *, expected_app: str,
                  expected_job_name: str, expected_attempt_id: str,
                  expected_kind: str, expected_target_version: str,
                  now: int) -> tuple[bool, str]:
    """(True, "ok") iff `token` is a well-shaped, correctly signed,
    unexpired office-approval document naming exactly the approval the
    caller is about to grant. Never touches a nonce ledger or any
    other persisted state -- replay protection across separate calls
    is the application layer's job (it has the ledger; this function
    does not), so a fresh nonce and an already-spent one verify
    identically here.

    Checked in this order (first failure wins; every reason is a fixed
    code, never the secret or the raw token):
      1. shape -- every signed field + "sig" present, right types
      2. kind is one of APPROVAL_KINDS
      3. signature -- hmac.compare_digest against `secret`, constant-time
      4. expiry window -- now in [issued_at, issued_at + ttl_seconds],
         with CLOCK_SKEW_TOLERANCE_SECONDS slack on the "future" side
      5. context match -- app/job_name/attempt_id/kind/target_version
         each equal the caller's OWN idea of what is pending right now
         (a token signed for an earlier version of the same request is
         rejected here, never accepted as "close enough")."""
    if not isinstance(token, Mapping):
        return False, "token_not_dict"
    problem = _shape_problem(token)
    if problem is not None:
        return False, problem
    kind = token["kind"]
    if kind not in APPROVAL_KINDS:
        return False, "kind_not_whitelisted"

    fields = {key: token[key] for key in _SIGNED_FIELDS}
    expected_sig = compute_signature(secret, fields)
    if not hmac.compare_digest(expected_sig, str(token["sig"])):
        return False, "signature_invalid"

    issued_at = int(token["issued_at"])
    ttl_seconds = int(token["ttl_seconds"])
    if now > issued_at + ttl_seconds:
        return False, "expired"
    if issued_at > now + CLOCK_SKEW_TOLERANCE_SECONDS:
        return False, "issued_in_future"

    if token["app"] != expected_app:
        return False, "app_mismatch"
    if token["job_name"] != expected_job_name:
        return False, "job_mismatch"
    if token["attempt_id"] != expected_attempt_id:
        return False, "attempt_mismatch"
    if token["kind"] != expected_kind:
        return False, "kind_mismatch"
    if token["target_version"] != expected_target_version:
        return False, "target_version_mismatch"
    return True, "ok"


__all__ = [
    "APPROVAL_KINDS", "DEFAULT_TTL_SECONDS", "CLOCK_SKEW_TOLERANCE_SECONDS",
    "build_token", "compute_signature", "verify_token",
]
