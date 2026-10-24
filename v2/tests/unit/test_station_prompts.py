"""facts_prompt must tell the facts worker about the "## 외부 대상"
section -- engine.gates.check_probe_targets (and engine.full_orbit's own
probe-skip decision) read ONLY that section of facts.md to decide which
external APIs/CLIs/file formats this job depends on and whether the
probe station runs at all."""
from __future__ import annotations

from engine.station_prompts import facts_prompt


def test_facts_prompt_tells_the_worker_to_write_외부_대상_section() -> None:
    text = facts_prompt("요청 내용", "job1")

    assert "## 외부 대상" in text
    # (없음) is the worker's way of saying "no external target" --
    # engine.gates.check_probe_targets treats a missing/empty section the
    # same way (probe skipped), but the worker must be told to write the
    # heading either way, never leave it out.
    assert "없음" in text
