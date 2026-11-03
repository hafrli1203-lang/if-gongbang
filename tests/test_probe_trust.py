# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from probe_trust import check_probe_log, check_probe_log_file

REAL_LOGS = [
    Path("/apps/exampleapp/.gongbang/chunkA-planning-loop/probe/t1.log"),
    Path("/apps/exampleapp/docs/probe/memo-classify.log"),
]


def test_bare_echo_forgery_rejected() -> None:
    # exact shape if-gongbang-v2's test_target_without_an_address_cannot_pass
    # uses to prove a forgery must fail
    result = check_probe_log("echo 'PROBE OK pdftotext'\n")
    assert result.ok is False
    assert result.reasons == ["no_response_evidence"]


def test_no_marker_at_all_rejected() -> None:
    result = check_probe_log("just some unrelated text\n")
    assert result.ok is False
    assert result.reasons == ["no_probe_ok_marker"]


def test_marker_with_evidence_but_no_date_rejected() -> None:
    text = "PROBE OK https://api.example.com\nreturncode: 0\n"
    result = check_probe_log(text)
    assert result.ok is False
    assert result.reasons == ["no_date"]


def test_fresh_log_with_evidence_passes() -> None:
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    text = "2026-11-02 10:00 PROBE OK https://api.example.com\nreturncode: 0\n응답: ok\n"
    result = check_probe_log(text, now=now)
    assert result.ok is True
    assert result.age_hours == 2.0


def test_stale_log_rejected_by_default_24h() -> None:
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    text = "2026-09-01 PROBE OK https://api.example.com\nreturncode: 0\n"
    result = check_probe_log(text, now=now)
    assert result.ok is False
    assert result.reasons == ["stale"]


def test_stale_log_accepted_with_wider_max_age() -> None:
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    text = "2026-09-01 PROBE OK https://api.example.com\nreturncode: 0\n"
    result = check_probe_log(text, now=now, max_age_hours=999999)
    assert result.ok is True


