"""Compatibility alias: `engine.ledger_backup` now lives at `engine.infrastructure.db.ledger_backup`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure.db import ledger_backup as _module

sys.modules[__name__] = _module
