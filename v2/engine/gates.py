"""Compatibility alias: `engine.gates` now lives at `engine.domain.stage_checks`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import stage_checks as _module

sys.modules[__name__] = _module
