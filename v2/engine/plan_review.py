"""Compatibility alias: `engine.plan_review` now lives at `engine.domain.plan_review`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import plan_review as _module

sys.modules[__name__] = _module
