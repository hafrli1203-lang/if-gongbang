"""Compatibility alias: `engine.fixup_target` now lives at `engine.application.repair_target`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import repair_target as _module

sys.modules[__name__] = _module
