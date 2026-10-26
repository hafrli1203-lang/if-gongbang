"""Compatibility alias: `engine.bench` now lives at `engine.domain.suite_trust`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import suite_trust as _module

sys.modules[__name__] = _module
