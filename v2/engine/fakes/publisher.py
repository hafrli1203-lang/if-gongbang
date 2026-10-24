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


@dataclass
class FakePublisher:
    calls: list[PublishCall] = field(default_factory=list)
    fail_after_effect: bool = False
    lookup_fails: bool = False

    def publish(self, key: str, target: str, artifact_hash: str) -> dict[str, object]:
        """Record the call (once per key). If fail_after_effect, raise
        PublisherCrashed AFTER recording. Returns {"id": key, "status": "ok"}."""
        existing = self._find(key)
        if existing is None:
            self.calls.append(PublishCall(key=key, target=target, artifact_hash=artifact_hash))
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
        for engine.publishers.ShellPublisher's receipts, see it too."""
        if self.lookup_fails:
            raise RuntimeError(f"publisher lookup unavailable for {key!r}")
        call = self._find(key)
        if call is None:
            return None
        return {"id": call.key, "key": call.key, "target": call.target,
                "artifact_hash": call.artifact_hash, "status": "done"}

    def _find(self, key: str) -> PublishCall | None:
        for call in self.calls:
            if call.key == key:
                return call
        return None
