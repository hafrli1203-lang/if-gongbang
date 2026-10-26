"""engine.interface.board -- deterministic, read-only HTML dashboard over one or
more gb2 engine ledgers.

Borrowed idea (Hermes Memory Wiki): no LLM, no network, stdlib only --
a plain data report, not a generated summary. Everything on the page is
either a literal field from a ledger row / lessons export / queue file,
or a value computed by a fixed, documented rule from those fields. There
is no invented number here: cost/duration are "-" or "확인 필요" when the
ledger simply does not carry the data, rather than guessed.

The data-loading half (reading ledger sqlite files directly, deriving
each job's display row) lives in engine.interface.board_data; the HTML-rendering
half lives in engine.interface.board_render. This module re-exports both so
`from engine.interface.board import X` keeps working unchanged.
"""
from __future__ import annotations

from engine.interface.board_data import (
    AppBoard, AttemptRow, EventRow, JobBoard, build_board, group_lessons, load_app_board,
)
from engine.interface.board_render import render_board_html

__all__ = [
    "EventRow", "AttemptRow", "JobBoard", "AppBoard", "load_app_board",
    "group_lessons", "render_board_html", "build_board",
]
