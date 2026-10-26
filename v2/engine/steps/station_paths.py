"""Compatibility alias: `engine.steps.station_paths` now lives at `engine.application.steps.worker_paths`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import worker_paths as _module

sys.modules[__name__] = _module
