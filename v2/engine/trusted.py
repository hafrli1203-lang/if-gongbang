"""Compatibility alias: `engine.trusted` now lives at `engine.infrastructure.trusted`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import trusted as _module

sys.modules[__name__] = _module
