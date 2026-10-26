"""Compatibility alias: `engine.owner` now lives at `engine.application.owner_approval`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import owner_approval as _module

sys.modules[__name__] = _module
