"""Compatibility alias: `engine.policy` now lives at `engine.domain.policy`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import policy as _module

sys.modules[__name__] = _module
