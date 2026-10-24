#!/usr/bin/env python3
"""Fake offline `npm audit --json` stand-in reporting exactly one
vulnerability (test fixture, not engine code) -- the counterpart to
fake_audit_clean.py, for tests/acceptance/test_t3_gb2_node_e2e.py's own
second run, proving engine.app_security._run_audit_cmd's
"vulnerabilities" state actually fails the app-wide security gate
(exits nonzero -> engine/app_security.py's own
_dependency_audit_state returns "vulnerabilities", which ALWAYS fails
the check -- no allow_* flag ever suppresses a real finding)."""
import json
import sys

print(json.dumps({
    "auditReportVersion": 2,
    "vulnerabilities": {
        "fake-vulnerable-pkg": {
            "name": "fake-vulnerable-pkg",
            "severity": "high",
            "via": ["Prototype Pollution (fake, test fixture only)"],
            "range": "<9.9.9",
            "fixAvailable": True,
        },
    },
    "metadata": {
        "vulnerabilities": {
            "info": 0, "low": 0, "moderate": 0, "high": 1, "critical": 0, "total": 1,
        },
        "dependencies": {"prod": 0, "dev": 1, "optional": 0, "peer": 0, "peerOptional": 0, "total": 1},
    },
}))
sys.exit(1)
