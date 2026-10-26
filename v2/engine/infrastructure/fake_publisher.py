"""Fake external publishing API. Records calls; idempotent by key."""
from __future__ import annotations

from dataclasses import dataclass, field


class PublisherCrashed(Exception):
    """Raised after the side effect was recorded (response never arrived)."""


@dataclass
class PublishCall:
    key: str
    target: str
    artifact_hash: str
    artifact_path: str = ""
    artifact_digest: str = ""


@dataclass
class FakePublisher:
    calls: list[PublishCall] = field(default_factory=list)
    fail_after_effect: bool = False
    lookup_fails: bool = False

    def publish(self, key: str, target: str, artifact_hash: str, *,
                artifact_path: str = "", artifact_digest: str = "",
                verify_cmd: tuple[str, ...] = ()) -> dict[str, object]:
        """Record the call (once per key). If fail_after_effect, raise
        PublisherCrashed AFTER recording. Returns {"id": key, "status": "ok"}.

        `artifact_path`/`artifact_digest` are recorded as-given (never
        measured here -- this is a fake, same "it models no 'failed'
        exit code" shape its own lookup() docstring already describes).
        `verify_cmd` is accepted for interface parity with engine.
        publishers.ShellPublisher.publish but never run -- a fake has
        no real deploy to verify, so it is always treated as "nothing
        to check" (lookup()'s own "status": "done" never depends on
        it)."""
        del verify_cmd
        existing = self._find(key)
        if existing is None:
            self.calls.append(PublishCall(
                key=key, target=target, artifact_hash=artifact_hash,
                artifact_path=artifact_path, artifact_digest=artifact_digest,
            ))
            if self.fail_after_effect:
                raise PublisherCrashed(key)
        return {"id": key, "status": "ok"}

    def lookup(self, key: str) -> dict[str, object] | None:
        """Return the recorded call or None. Raises RuntimeError if
        lookup_fails (simulates the status API being down). "status":
        "done" always -- FakePublisher has only one terminal outcome
        (a recorded call always means the effect happened; it models
        no "failed" exit code), so every receipt it can produce is
        exactly that. "key" is included alongside "id" so callers that
        validate a receipt's own identity fields (key/target/
        artifact_hash) against the request's, the same way they would
        for engine.infrastructure.publishers.ShellPublisher's receipts, see it too.
        "verified" is always False: a fake never actually ran a
        verify_cmd (there is nothing real to verify), so it must never
        claim to have confirmed one -- same "degrade, never over-claim"
        rule ShellPublisher's own real receipts follow."""
        if self.lookup_fails:
            raise RuntimeError(f"publisher lookup unavailable for {key!r}")
        call = self._find(key)
        if call is None:
            return None
        return {"id": call.key, "key": call.key, "target": call.target,
                "artifact_hash": call.artifact_hash, "artifact_path": call.artifact_path,
                "artifact_digest": call.artifact_digest, "status": "done", "verified": False}

    def _find(self, key: str) -> PublishCall | None:
        for call in self.calls:
            if call.key == key:
                return call
        return None
