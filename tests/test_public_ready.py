"""Thin test wrapper around harness/public_ready_scan.py (the single public-readiness scan
logic — see that module's docstring for why specific identifiers are never listed here).
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from harness.public_ready_scan import GENERIC, load_list, scan  # noqa: E402

THIS_FILE = os.path.abspath(__file__)


def test_no_generic_server_identifiers_anywhere():
    hits = scan(REPO_ROOT, extra_skip={THIS_FILE})
    # the generic patterns alone (no local list) must already be clean
    generic_only = [h for h in hits if any(h.split(": ", 1)[1].startswith(p[:30]) for p in GENERIC)]
    assert not generic_only, "forbidden strings found:\n" + "\n".join(generic_only)


def test_no_local_extra_identifiers_anywhere():
    patterns = load_list(REPO_ROOT)
    if not patterns:
        pytest.skip("no local identifier list (tests/public_ready.local.txt) - nothing extra to check")
    hits = scan(REPO_ROOT, extra_skip={THIS_FILE})
    local_only = [h for h in hits if not any(h.split(": ", 1)[1].startswith(p[:30]) for p in GENERIC)]
    assert not local_only, "forbidden strings found:\n" + "\n".join(local_only)
