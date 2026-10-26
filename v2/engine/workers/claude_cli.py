"""Compatibility alias: `engine.workers.claude_cli` now lives at `engine.infrastructure.workers.claude_cli`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure.workers import claude_cli as _module

sys.modules[__name__] = _module
