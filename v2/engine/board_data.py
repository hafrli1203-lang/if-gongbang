"""Compatibility alias: `engine.board_data` now lives at `engine.interface.board_data`
(docs/standards/engine-layout.md)."""
import sys

from engine.interface import board_data as _module

sys.modules[__name__] = _module
