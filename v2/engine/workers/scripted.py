"""Compatibility alias: `engine.workers.scripted` now lives at `engine.infrastructure.workers.scripted`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure.workers import scripted as _module

sys.modules[__name__] = _module
