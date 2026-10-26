"""Compatibility alias: `engine.steps.merge` now lives at `engine.application.steps.merge`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import merge as _module

sys.modules[__name__] = _module
