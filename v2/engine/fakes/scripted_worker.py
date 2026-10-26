"""Compatibility alias: `engine.fakes.scripted_worker` now lives at `tests.fakes.scripted_worker`
(docs/standards/engine-layout.md)."""
import sys

from tests.fakes import scripted_worker as _module

if __name__ == "__main__":
    sys.exit(_module.run(sys.argv[1:]))
else:
    sys.modules[__name__] = _module
