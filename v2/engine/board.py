"""Compatibility alias: `engine.board` now lives at `engine.interface.board`
(docs/standards/engine-layout.md)."""
import sys

from engine.interface import board as _module

sys.modules[__name__] = _module
