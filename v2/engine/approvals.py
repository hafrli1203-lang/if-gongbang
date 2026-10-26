"""Compatibility alias: `engine.approvals` now lives at `engine.domain.approvals`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import approvals as _module

sys.modules[__name__] = _module
