"""Compatibility alias: `engine.evidence_graph` now lives at `engine.domain.evidence_graph`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import evidence_graph as _module

sys.modules[__name__] = _module
