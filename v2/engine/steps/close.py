"""Compatibility alias: `engine.steps.close` now lives at `engine.application.steps.close`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import close as _module

sys.modules[__name__] = _module
