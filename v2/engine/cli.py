"""Compatibility alias: `engine.cli` now lives at `engine.interface.cli`
(docs/standards/engine-layout.md)."""
import sys

from engine.interface import cli as _module

if __name__ == "__main__":
    sys.exit(_module.main())
else:
    sys.modules[__name__] = _module
