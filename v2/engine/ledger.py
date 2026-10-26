"""Compatibility alias: `engine.ledger` now lives at `engine.infrastructure.db.ledger`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure.db import ledger as _module

sys.modules[__name__] = _module
