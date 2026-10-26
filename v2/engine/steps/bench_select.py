"""Compatibility alias: `engine.steps.bench_select` now lives at `engine.application.steps.suite_select`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import suite_select as _module

sys.modules[__name__] = _module
