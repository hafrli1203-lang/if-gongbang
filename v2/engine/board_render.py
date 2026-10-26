"""Compatibility alias: `engine.board_render` now lives at `engine.interface.board_render`
(docs/standards/engine-layout.md)."""
import sys

from engine.interface import board_render as _module

sys.modules[__name__] = _module
