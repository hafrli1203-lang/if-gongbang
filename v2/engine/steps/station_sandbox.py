"""Compatibility alias: `engine.steps.station_sandbox` now lives at `engine.application.steps.worker_sandbox`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import worker_sandbox as _module

sys.modules[__name__] = _module
