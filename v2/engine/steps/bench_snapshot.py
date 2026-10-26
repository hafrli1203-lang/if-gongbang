"""Compatibility alias: `engine.steps.bench_snapshot` now lives at `engine.application.steps.suite_snapshot`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import suite_snapshot as _module

sys.modules[__name__] = _module
