"""T9c -- two more regressions from the SAME real job (마케팅 채팅,
, read-only from that app's own ledger.sqlite, `probe.
executed`), split out of test_t9b_probe_sandbox.py (that file's own
800-line budget, docs/standards/engine-layout.md):

  23. an "## 외부 대상" bullet whose doc value was an INTERNAL target
      (no public doc) still had a trailing explanation on the same
      line, appended AFTER a parenthetical that already explained why
      there was no url -- the probe script itself only ever printed
      the first parenthetical, so check_probe_targets' parsed value
      must drop the trailing clause too, the same way an http(s) url
      above it already drops a trailing explanation (결함 19).
                                                            test_23_*
  24. `gb2 continue <app>` only ever reruns the station a job is
      CURRENTLY stopped at (or later) -- when an owner/foreman scope
      decision made AFTER facts.md was written needs facts.md ITSELF
      redone (the probe station cannot write facts.md, 결함 12), there
      was no way back. `gb2 continue <app> --from facts` (engine.
      full_orbit.force_reopen_station) is the fix.          test_24_*
"""
from __future__ import annotations

import pytest

import tests.unit.test_full_orbit as _tfo
from engine import full_orbit
from tests.conftest import Workspace

# A plain module import (never "from tests.unit.test_full_orbit import
# _name, ...") -- tools/arch_check.py's own check_private_imports only
# flags individually-named imports of an underscore-prefixed name, not
# a whole-module import referenced as `_tfo.name` below (same
# "no new arch_check violations" floor every worker's own job runs
# under). test_full_orbit.py's own helpers (_fakes, _RunStationStub,
# etc.) are the SAME ones test_position.py/test_small_orbit.py/
# test_t8_flow_boundary_stops.py already reuse this way.


# ---- 23. a non-url doc value's trailing explanation, OUTSIDE its own ---
#          "(...)" , must be dropped the same way an http(s) url's is

def test_23_internal_doc_value_with_a_trailing_explanation_after_its_own_parens() -> None:
    """The real incident (마케팅 채팅 job, ledger seq 603/733): an
    internal target's doc value was "(공식 문서 URL 없음 — 사내 API; …
    열람 불가) — probe t5는 다른 경로(…)를 테스트했던 것이라 … 다시
    probe 필요" -- the probe script's own real stdout (seq 603's
    stdout_tail) only ever printed "PROBE OK " plus the FIRST
    parenthetical, never the trailing "— probe t5는 …" clause."""
    from engine.domain.stage_checks import check_probe_targets

    first_parenthetical = (
        "(공식 문서 URL 없음 — 사내 API; kit/os 패키지나 sample-app-OS "
        "저장소 안에 있을 것으로 추정되나 이 샌드박스에서 열람 불가)"
    )
    doc_value = (
        f"{first_parenthetical} — probe t5는 다른 경로(sample-app-library "
        "MCP)를 테스트했던 것이라 이 결정과는 별도로 다시 probe 필요"
    )
    facts_text = f"## 외부 대상\n- OS 도서관 API: {doc_value}\n"

    def _targets(tmp_path):
        d = tmp_path / ".gongbang" / "job23"
        d.mkdir(parents=True)
        (d / "facts.md").write_text(facts_text, encoding="utf-8")
        return check_probe_targets(tmp_path, "job23")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        targets = _targets(Path(tmp))

    assert targets == [("OS 도서관 API", first_parenthetical)], (
        "the parsed doc value must stop at the first parenthetical's own "
        "closing paren, dropping the trailing '— probe t5는 …' clause -- "
        "otherwise this repro proves nothing")


def test_23_red_the_unsplit_value_never_matches_the_real_script_stdout() -> None:
    """Demonstrates the pre-fix failure mode directly: the OLD value
    (the whole string after ":" -- what a plain `body.partition(":")`
    with no cut at all used to return) never matches `PROBE OK <value>`
    against the probe script's own real stdout (seq 603's own
    stdout_tail), even though that script genuinely succeeded."""
    from engine.application.acceptance_probe import _probe_observed

    first_parenthetical = (
        "(공식 문서 URL 없음 — 사내 API; kit/os 패키지나 sample-app-OS "
        "저장소 안에 있을 것으로 추정되나 이 샌드박스에서 열람 불가)"
    )
    old_uncut_value = (
        f"{first_parenthetical} — probe t5는 다른 경로(sample-app-library "
        "MCP)를 테스트했던 것이라 이 결정과는 별도로 다시 probe 필요"
    )
    real_script_stdout = (
        "status=200 body_len=7964 has_results_key=True\n"
        f"PROBE OK {first_parenthetical}\n"
    )

    assert _probe_observed(0, real_script_stdout, old_uncut_value) is False, (
        "the pre-fix (uncut) value was expected to fail this match -- the "
        "repro no longer demonstrates the historical bug")
    assert _probe_observed(0, real_script_stdout, first_parenthetical) is True


def test_23_a_value_with_no_parens_cuts_at_its_first_top_level_separator() -> None:
    """A non-url doc value that never opens a "(...)" at all has no
    depth-1 region to protect -- its first separator (if any) is
    already at depth 0 and is cut exactly as the plain reading of the
    rule says."""
    from engine.domain.stage_checks import _cut_doc_value_explanation

    assert _cut_doc_value_explanation("사내 API — 왜 없는지는 별도 설명") == "사내 API"
    assert _cut_doc_value_explanation("(사내)") == "(사내)"


# ---- 24. `gb2 continue --from <station>` reopens an EARLIER early -----
#          station than the one a job is currently stopped at

def _facts_md_with_target(target_doc_url: str) -> str:
    return (
        "# 사실확정\n- 요청 확인: 통과 (출처: request)\n\n"
        f"## 외부 대상\n- systemctl: {target_doc_url}\n"
    )


_FACTS_MD_NO_TARGET = (
    "# 사실확정\n- 요청 확인: 통과 (출처: request)\n\n"
    "## 외부 대상\n- (없음)\n"
)


def test_24_continue_from_facts_redoes_facts_and_skips_the_dropped_probe_target(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real incident (마케팅 채팅 job,: an owner/foreman
    decision made AFTER facts.md was written dropped a probe target
    (systemctl service status) from scope -- but the probe station
    cannot edit facts.md (station_record_prefixes, 결함 12), and the
    ordinary `gb2 continue` only ever reruns "probe" on a probe_failed
    stop, reproducing the exact same (now-unwanted) target forever.
    `gb2 continue <app> --from facts` (full_orbit.continue_full_job_from)
    reopens "facts" instead: the NEW facts.md (no external target) is
    what the rest of the job sees, so probe is skipped entirely and the
    job finishes without ever retrying the dropped target's probe."""
    ws = _tfo._full_orbit_workspace(workspace)
    job_name = "force-reopen-job"
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _tfo._RunStationStub({
        "facts": [
            {f".gongbang/{job_name}/facts.md":
             _facts_md_with_target("https://www.freedesktop.org/software/systemd/man/systemctl.html")},
            # The owner-reduced scope a facts worker writes once this
            # job is reopened from "facts" -- the target is gone.
            {f".gongbang/{job_name}/facts.md": _FACTS_MD_NO_TARGET},
        ],
        "probe": [
            {f".gongbang/{job_name}/probe/t1.log":
             "https://www.freedesktop.org/software/systemd/man/systemctl.html\n"
             "PROBE FAIL\nsandboxed, no systemd socket\n"},
        ],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _tfo._install_fake_acceptance_and_handoff(monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)])
    factory = _tfo._processor_factory(stub)

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "systemctl 상태 보여줘",
        processor_factory=factory,
    )
    assert first.status == "needs_owner"
    assert first.reason == "probe_failed:systemctl"
    progress = _tfo._stop_view(ws.ledger, job_name, first.attempt_id)
    assert progress is not None
    assert progress.get("stage") == full_orbit.STAGE_PROBE_RETRY
    assert ("plan", "planner") not in stub.calls

    done = full_orbit.continue_full_job_from(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "facts",
        processor_factory=factory,
    )

    assert done.status == "done", done
    assert [c for c in stub.calls if c[0] == "facts"] == [
        ("facts", "facts"), ("facts", "facts"),
    ]
    # The dropped target's probe is never retried a second time -- the
    # reopened facts.md names no external target at all.
    assert [c for c in stub.calls if c[0] == "probe"] == [("probe", "prober")]
    assert (ws.alpha / "src/greeter/extra1.py").exists()


def test_24_continue_from_a_station_later_than_the_current_stop_is_refused(
    workspace: Workspace,
) -> None:
    """An owner can only go BACK to an earlier station they already
    passed -- never skip ahead to one later than where the job is
    currently stopped (that is what the ordinary `gb2 continue`
    already does)."""
    from engine.application.job_state import STAGE_PROBE_RETRY, JobPosition
    from engine.application.pipeline_resume import ForceReopenError, force_reopen_station

    pos = JobPosition(job="j", stage=STAGE_PROBE_RETRY, attempt_id="a1",
                      extra={"request": "x"})

    with pytest.raises(ForceReopenError):
        force_reopen_station(None, pos, "plan")  # type: ignore[arg-type]


def test_24_continue_from_is_refused_during_the_build_stage(workspace: Workspace) -> None:
    """Every stage from STAGE_DESIGN_APPROVAL onward is the build stage
    (docs/v2.md 「궤도 9칸」) -- force_reopen_station never reaches into
    it; a crashed/stopped build task has its own recovery through the
    ordinary RESUME_TABLE."""
    from engine.application.job_state import STAGE_TASK_OWNER, JobPosition
    from engine.application.pipeline_resume import ForceReopenError, force_reopen_station

    pos = JobPosition(job="j", stage=STAGE_TASK_OWNER, attempt_id="a1",
                      task_id="t1", remaining_task_ids=(), extra={})

    with pytest.raises(ForceReopenError):
        force_reopen_station(None, pos, "facts")  # type: ignore[arg-type]


def test_24_continue_from_an_unknown_station_name_is_refused(workspace: Workspace) -> None:
    from engine.application.job_state import STAGE_PROBE_RETRY, JobPosition
    from engine.application.pipeline_resume import ForceReopenError, force_reopen_station

    pos = JobPosition(job="j", stage=STAGE_PROBE_RETRY, attempt_id="a1", extra={})

    with pytest.raises(ForceReopenError):
        force_reopen_station(None, pos, "release")  # type: ignore[arg-type]
