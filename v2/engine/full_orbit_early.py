"""Compatibility alias: `engine.full_orbit_early` now lives at `engine.application.pipeline_planning`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import pipeline_planning as _module

sys.modules[__name__] = _module
