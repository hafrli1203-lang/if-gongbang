"""Compatibility alias: `engine.full_orbit_runner` now lives at `engine.application.pipeline_stages`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import pipeline_stages as _module

sys.modules[__name__] = _module
