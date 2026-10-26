"""Compatibility alias: `engine.release_git` now lives at `engine.infrastructure.release_git`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import release_git as _module

sys.modules[__name__] = _module
