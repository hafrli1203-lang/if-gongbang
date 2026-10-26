"""Compatibility alias: `engine.steps.station` now lives at `engine.application.steps.worker_step`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import worker_step as _module

sys.modules[__name__] = _module
