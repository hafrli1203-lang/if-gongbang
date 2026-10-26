"""Compatibility alias: `engine.steps.station_gate` now lives at `engine.application.steps.worker_gate_log`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import worker_gate_log as _module

sys.modules[__name__] = _module
