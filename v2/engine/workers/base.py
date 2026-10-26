"""Compatibility alias: `engine.workers.base` now lives at `engine.infrastructure.workers.base`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure.workers import base as _module

sys.modules[__name__] = _module
