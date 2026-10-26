"""Fake dependency installer. Applies a manifest change to a file in the
worker copy; idempotent by key."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from engine.infrastructure.installers import merge_one_level as _merge_one_level  # re-export


@dataclass
class FakeInstaller:
    applied_keys: dict[str, dict[str, object]] = field(default_factory=dict)
    fail_after_effect: bool = False

    def apply(self, key: str, manifest: Path, change: dict[str, object],
              original: bytes | None = None) -> None:
        """Merge `change` (JSON object) into the manifest file, record key.
        If fail_after_effect, raise RuntimeError after writing. `original`
        is accepted for Installer-protocol compatibility and ignored:
        this fake has no crash/resume rollback story to protect."""
        if key in self.applied_keys:
            return  # idempotent: already applied, no-op
        current: dict[str, object] = {}
        if manifest.exists():
            loaded = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        merged = _merge_one_level(current, change)
        manifest.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.applied_keys[key] = change
        if self.fail_after_effect:
            raise RuntimeError(f"installer crashed after applying {key!r}")

    def applied(self, key: str) -> bool:
        return key in self.applied_keys
