"""Compatibility alias: `engine.steps.commit` now lives at `engine.application.steps.commit`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import commit as _module

sys.modules[__name__] = _module
