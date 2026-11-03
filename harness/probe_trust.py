# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""Probe-log trust checks (묶음 6 — 실측 신뢰성).

실측자(probe agent) writes probe/<name>.log by hand after an actual CLI/API
call; nothing here re-runs the call or proxies the network (that is the
heavier, deferred 175번 작업 -- "실제 네트워크 접촉 독립 재확인 도구"). This
is the lighter check the gap-verdict table (58/176번) asked for instead:
catch the two cheap forgeries of a "PROBE OK" marker --

  1. a bare echo/print of the marker with no actual response evidence
     (exactly what if-gongbang-v2/tests/unit/test_probe_contact_observed.py
     ::test_probe_that_only_prints_probe_ok_fails and ::test_target_without_
     an_address_cannot_pass exercise against the real recording-proxy
     engine -- `echo 'PROBE OK pdftotext'` with nothing else must fail
     here the same way)
  2. a stale log (default >24h old) reused for a NEW change without
     re-running the call

A real probe log on this host (e.g.
/apps/exampleapp/.gongbang/chunkA-planning-loop/probe/t1.log,
/apps/exampleapp/docs/probe/memo-classify.log) always carries a dated
header line, the actual command, and concrete response evidence (return
code, byte/char counts, status, or a quoted response body) -- both read
during this module's design to fix what "real evidence" looks like.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROBE_OK_RE = re.compile(r"PROBE OK\b")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?")

# Evidence that an actual call happened and produced something, beyond the
# bare marker line itself: a return/exit code, byte/char counts, an HTTP
# status, a response body quoted back, or an explicit "응답"/"response"
# section header (Korean probe logs on this host use "응답:"/"response:").
_EVIDENCE_RE = re.compile(
    r"(returncode|return code|exit code|종료 코드)\s*[:=]?\s*-?\d+"
    r"|(status|상태)\s*[:=]?\s*\d{3}"
    r"|\b\d+\s*(bytes|byte|바이트)\b"
    r"|(응답|response)\s*[:：]",
    re.I,
)

REASON_NO_MARKER = "no_probe_ok_marker"
REASON_NO_EVIDENCE = "no_response_evidence"
REASON_NO_DATE = "no_date"
REASON_STALE = "stale"
REASON_OK = "ok"


@dataclass(frozen=True)
class ProbeTrustResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    log_date: datetime | None = None
    age_hours: float | None = None


def _earliest_date(text: str) -> datetime | None:
    """First `YYYY-MM-DD[ HH:MM[:SS]]` found in `text`, naive-UTC (probe
    logs on this host carry no timezone; treated as UTC for a stable,
    reproducible age calculation rather than guessing the host's local
    zone)."""
    m = _DATE_RE.search(text)
    if not m:
        return None
    year, month, day = (int(x) for x in m.group(1).split("-"))
    hour = int(m.group(2) or 0)
    minute = int(m.group(3) or 0)
    second = int(m.group(4) or 0)
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


def check_probe_log(text: str, *, now: datetime | None = None,
                     max_age_hours: float = 24.0) -> ProbeTrustResult:
    """Judges an already-read probe log's TEXT (callers read the file
    themselves -- this never touches the filesystem). ok=False on the
    first reason that applies, in this order: no PROBE OK marker at all;
    the marker with no response evidence (the echo-only forgery); no
    parsable date; the parsed date older than `max_age_hours`
    (default 24h) ahead of `now` (default: real current UTC time)."""
    if not PROBE_OK_RE.search(text):
        return ProbeTrustResult(False, [REASON_NO_MARKER])

    if not _EVIDENCE_RE.search(text):
        return ProbeTrustResult(False, [REASON_NO_EVIDENCE])

    log_date = _earliest_date(text)
    if log_date is None:
        return ProbeTrustResult(False, [REASON_NO_DATE])

    now = now or datetime.now(timezone.utc)
    age_hours = (now - log_date).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        return ProbeTrustResult(False, [REASON_STALE], log_date, age_hours)

    return ProbeTrustResult(True, [REASON_OK], log_date, age_hours)


def check_probe_log_file(path: Path, *, now: datetime | None = None,
                          max_age_hours: float = 24.0) -> ProbeTrustResult:
    return check_probe_log(path.read_text(encoding="utf-8"), now=now,
                            max_age_hours=max_age_hours)
