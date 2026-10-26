"""Compatibility alias: `engine.state` now lives at `engine.domain.state`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import state as _module

sys.modules[__name__] = _module
