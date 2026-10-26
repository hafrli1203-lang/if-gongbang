"""Compatibility alias: `engine.full_orbit_tasks` now lives at `engine.application.pipeline_tasks`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import pipeline_tasks as _module

sys.modules[__name__] = _module
