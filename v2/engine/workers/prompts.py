"""Compatibility alias: `engine.workers.prompts` now lives at `engine.infrastructure.workers.prompts`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure.workers import prompts as _module

sys.modules[__name__] = _module
