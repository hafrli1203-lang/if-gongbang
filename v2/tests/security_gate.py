"""Shared gate for the engine's own sandbox-isolation tests (F4,
: test_sandbox.py, test_procs.py, test_acceptance.py,
test_probe_sandbox_isolation.py, test_probe_runtime_clis.py,
test_t9_real_conditions.py, test_t9b_probe_sandbox.py all define their
own `_needs_bwrap = pytest.mark.skipif(not sandbox.available(), ...)`.
These are security tests -- they prove the engine's own bwrap
isolation actually holds, not a mere convenience check -- so a CI
runner where bwrap silently stops working (a distro image change, an
apparmor/userns restriction regressing) must never let them just skip
into a quietly-green job the way a plain skipif does.

test_t3_gb2_node_e2e.py / test_t4_gb2_new_clean.py / test_t5_small_change.py
already solved this exact problem for the Node e2e path with their own
GB2_REQUIRE_T3=1 env flag: under that flag, their skipif condition
drops the "missing prerequisite" half, so the test runs for real and
fails on its own merits instead of skipping. GB2_REQUIRE_SANDBOX=1 is
the same pattern, generalized to every bwrap-gated security test
module, read once here so every caller stays consistent.
"""
from __future__ import annotations

import os

import pytest

GB2_REQUIRE_SANDBOX = os.environ.get("GB2_REQUIRE_SANDBOX") == "1"


def bwrap_skipif(available: bool, reason: str) -> pytest.MarkDecorator:
    """Same contract as `pytest.mark.skipif(not available, reason=reason)`,
    except under GB2_REQUIRE_SANDBOX=1 the "not available" half of the
    condition is dropped: the test then runs for real (and fails on
    its own merits) instead of skipping, exactly like GB2_REQUIRE_T3=1
    does for the Node e2e tests above."""
    return pytest.mark.skipif(not GB2_REQUIRE_SANDBOX and not available, reason=reason)
