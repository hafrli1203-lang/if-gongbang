"""Compatibility alias: `engine.workers.codex_cli` now lives at `engine.infrastructure.workers.codex_cli`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure.workers import codex_cli as _module

sys.modules[__name__] = _module
