"""Compatibility alias: `engine.context` now lives at `engine.infrastructure.context`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import context as _module

sys.modules[__name__] = _module
