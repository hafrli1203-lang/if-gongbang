"""Compatibility alias: `engine.full_orbit_resume` now lives at `engine.application.pipeline_resume`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import pipeline_resume as _module

sys.modules[__name__] = _module
