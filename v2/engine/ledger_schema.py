"""Compatibility alias: `engine.ledger_schema` now lives at `engine.infrastructure.db.ledger_schema`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure.db import ledger_schema as _module

sys.modules[__name__] = _module
