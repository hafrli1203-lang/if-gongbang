"""Compatibility alias: `engine.evidence` now lives at `engine.infrastructure.evidence`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import evidence as _module

sys.modules[__name__] = _module
