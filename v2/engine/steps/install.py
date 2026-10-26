"""Compatibility alias: `engine.steps.install` now lives at `engine.application.steps.install`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import install as _module

sys.modules[__name__] = _module
