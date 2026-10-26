"""Compatibility alias: `engine.handoff` now lives at `engine.application.handoff`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import handoff as _module

sys.modules[__name__] = _module
