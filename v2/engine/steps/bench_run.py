"""Compatibility alias: `engine.steps.bench_run` now lives at `engine.application.steps.suite_run`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import suite_run as _module

sys.modules[__name__] = _module
