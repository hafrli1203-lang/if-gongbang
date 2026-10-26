"""Compatibility package: the request-kind step lists now live in
`engine.application.steps` (docs/standards/engine-layout.md). Each module
here is an alias of its new module; KINDS is the same dict object."""
from engine.application.steps import KINDS  # noqa: F401
from engine.steps import (  # noqa: F401
    bench_run, close, commit, install, merge, publish, report, review, station,
)
