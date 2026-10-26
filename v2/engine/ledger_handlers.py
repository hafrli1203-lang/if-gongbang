"""Compatibility alias: `engine.ledger_handlers` now lives at `engine.infrastructure.db.ledger_handlers`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure.db import ledger_handlers as _module

sys.modules[__name__] = _module
