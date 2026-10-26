"""Compatibility alias: `engine.plan_tools` now lives at `engine.domain.plan_tools`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import plan_tools as _module

sys.modules[__name__] = _module
