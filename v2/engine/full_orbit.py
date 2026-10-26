"""Compatibility alias: `engine.full_orbit` now lives at `engine.application.pipeline`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import pipeline as _module

sys.modules[__name__] = _module
