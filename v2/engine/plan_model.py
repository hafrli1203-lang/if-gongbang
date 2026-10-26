"""Compatibility alias: `engine.plan_model` now lives at `engine.domain.plan_model`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import plan_model as _module

sys.modules[__name__] = _module
