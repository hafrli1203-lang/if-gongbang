"""Compatibility alias: `engine.publishers` now lives at `engine.infrastructure.publishers`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import publishers as _module

sys.modules[__name__] = _module
