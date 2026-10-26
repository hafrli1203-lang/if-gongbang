"""Compatibility alias: `engine.steps.review` now lives at `engine.application.steps.review`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import review as _module

sys.modules[__name__] = _module
