#!/usr/bin/env python3
"""Fake offline `npm audit --json` stand-in (test fixture, not engine
code): prints a well-formed, empty npm-audit-shaped report and exits 0
-- engine.app_security parses the audit JSON: an empty "vulnerabilities"
object with exit 0 is the "passed" state, so both the exit code and the
JSON shape matter here.
Deliberately never touches the network -- the whole point of this
fixture is to replace the template's default `npm audit` (which does)
for tests/acceptance/test_t3_gb2_node_e2e.py's own offline run."""
import json
import sys

print(json.dumps({
    "auditReportVersion": 2,
    "vulnerabilities": {},
    "metadata": {
        "vulnerabilities": {
            "info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 0, "total": 0,
        },
        "dependencies": {"prod": 0, "dev": 1, "optional": 0, "peer": 0, "peerOptional": 0, "total": 1},
    },
}))
sys.exit(0)
