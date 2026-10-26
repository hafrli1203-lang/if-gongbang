"""Compatibility alias: `engine.small_orbit` now lives at `engine.application.quick_pipeline`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import quick_pipeline as _module

sys.modules[__name__] = _module
