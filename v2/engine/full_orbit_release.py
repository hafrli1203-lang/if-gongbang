"""Compatibility alias: `engine.full_orbit_release` now lives at `engine.application.pipeline_release`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import pipeline_release as _module

sys.modules[__name__] = _module
