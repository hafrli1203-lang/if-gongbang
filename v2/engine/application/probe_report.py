"""Turns the engine's own probe records into prompt text.

The planner's "## 실측 결과" section and the prober's retry note are
both built ONLY from what the engine recorded when it ran each probe
script itself (engine.application.acceptance_probe: the probe.executed
payload -- exit code, engine-captured output, run timestamps, script
hash, and the recording proxy's contact list). Log files the probe
worker wrote under .gongbang/<job>/probe/ are never read here: they
are the worker's own account of what happened, not evidence.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from engine.application.pipeline_base import PROBE_LOG_CAP

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

_HEADING = "## 실측 결과 (엔진이 직접 실행한 기록)"


def evidence_record(evidence: object) -> dict[str, object]:
    """A run_probe_scripts result as the same mapping shape its
    probe.executed payload has."""
    keys = ("id", "name", "url", "returncode", "stdout_tail", "sha256",
            "started_at", "finished_at", "contacts", "reached")
    return {k: getattr(evidence, k, "") for k in keys}


def _contact_line(contact: Mapping[str, object]) -> str:
    state = "연결됨" if contact.get("connected") is True else "연결 실패"
    digest = contact.get("response_sha256") or "-"
    return (f"  - {contact.get('method', '?')} {contact.get('host', '?')}:"
            f"{contact.get('port', '?')} {state}, 보냄 {contact.get('bytes_up', 0)}B, "
            f"받음 {contact.get('bytes_down', 0)}B, 응답 sha256 {digest}")


def _record_text(record: Mapping[str, object]) -> str:
    contacts = record.get("contacts")
    contact_list = [c for c in contacts if isinstance(c, Mapping)] if isinstance(
        contacts, (list, tuple)) else []
    contact_text = ("\n".join(_contact_line(c) for c in contact_list)
                    if contact_list else "  - (엔진 프록시를 거친 접속 없음)")
    reached = "예" if record.get("reached") is True else "아니오"
    output = str(record.get("stdout_tail") or "")[:PROBE_LOG_CAP]
    return (
        f"### {record.get('name', '?')} ({record.get('id', '?')})\n"
        f"- 문서: {record.get('url', '')}\n"
        f"- 종료 코드: {record.get('returncode', '?')}, 실행: "
        f"{record.get('started_at') or '-'} ~ {record.get('finished_at') or '-'}\n"
        f"- 스크립트 sha256: {record.get('sha256') or '-'}\n"
        f"- 대상 호스트에 닿음: {reached}\n"
        f"- 엔진 프록시가 본 접속:\n{contact_text}\n"
        f"- 엔진이 잡은 출력:\n```\n{output}\n```"
    )


def probe_records_text(records: Iterable[Mapping[str, object]]) -> str:
    parts = [_record_text(r) for r in records]
    if not parts:
        return ""
    return f"{_HEADING}\n" + "\n\n".join(parts)


def previous_probe_runs_text(ledger: Ledger, project: str, attempt_id: str) -> str:
    """The prober's retry note: the newest engine record per target for
    this attempt, plus how many engine probe runs came before (so each
    retry's prompt differs from the last even when the failure output
    repeats). "" when the engine never ran a probe for this attempt."""
    latest: dict[str, Mapping[str, object]] = {}
    runs = 0
    for _seq, event in ledger.events(project, "probe.executed"):
        if event.attempt_id != attempt_id:
            continue
        runs += 1
        latest[str(event.payload.get("id", ""))] = event.payload
    if not latest:
        return ""
    body = probe_records_text(latest.values())
    return f"엔진이 이 시도에서 실측 스크립트를 실행한 기록 {runs}건 중 대상별 최신:\n{body}"


__all__ = ["evidence_record", "probe_records_text", "previous_probe_runs_text"]
