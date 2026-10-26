"""Compatibility alias: `engine.procs` now lives at `engine.infrastructure.procs`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import procs as _module

sys.modules[__name__] = _module
