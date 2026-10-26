"""Compatibility alias: `engine.orbit` now lives at `engine.application.task_pipeline`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import task_pipeline as _module

sys.modules[__name__] = _module
