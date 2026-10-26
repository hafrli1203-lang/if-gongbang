"""Compatibility alias: `engine.steps.bench_zero_tests` now lives at `engine.application.steps.suite_zero_tests`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import suite_zero_tests as _module

sys.modules[__name__] = _module
