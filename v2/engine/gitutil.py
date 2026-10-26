"""Compatibility alias: `engine.gitutil` now lives at `engine.infrastructure.gitutil`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import gitutil as _module

sys.modules[__name__] = _module
