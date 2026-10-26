"""Compatibility alias: `engine.steps.publish` now lives at `engine.application.steps.publish`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import publish as _module

sys.modules[__name__] = _module
