"""Compatibility alias: `engine.full_orbit_base` now lives at `engine.application.pipeline_base`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import pipeline_base as _module

sys.modules[__name__] = _module
