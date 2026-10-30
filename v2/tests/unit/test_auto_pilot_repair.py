"""auto_pilot: a failed build review starts a repair round automatically
and stops for the foreman at the round cap."""
import argparse
from pathlib import Path
from types import SimpleNamespace

from engine.application import auto_pilot
from engine.application.pipeline_base import FullOutcome


def _config(tmp_path: Path):
    return SimpleNamespace(projects={"bakery": SimpleNamespace(repo=tmp_path)})


class _Ledger:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def test_failed_build_review_runs_one_repair_round_then_returns_its_outcome(monkeypatch, tmp_path):
    calls = []
    fixed = FullOutcome("done", None, "J-bakery#2", None)

    def fake_fix(config, ledger, project, extra, fakes_for, on_progress=None, processor_factory=None):
        calls.append(("fix", project, extra))
        return SimpleNamespace(old_attempt_id="J-bakery#1", new_attempt_id="J-bakery#2",
                               job_name="menu-card", task="t", outcome=None, full_job=True)

    def fake_resume(config, ledger, fakes_for, project, result, on_progress=None, processor_factory=None):
        calls.append(("resume", result.new_attempt_id))
        return fixed

    import engine.application.repair as repair_mod
    import engine.application.job_state as job_state_mod
    monkeypatch.setattr(repair_mod, "fix", fake_fix)
    monkeypatch.setattr(job_state_mod, "resume_after_fix", fake_resume)
    monkeypatch.setattr(auto_pilot, "load_position", lambda *a, **k: None)

    failed = FullOutcome("failed", "build:review_not_passed", "J-bakery#1", None)
    out = auto_pilot.auto_settle(_config(tmp_path), _Ledger(), lambda aid: None, "bakery", "menu-card", failed)
    assert out is fixed
    assert calls == [("fix", "bakery", ""), ("resume", "J-bakery#2")]


def test_round_cap_writes_foreman_note_and_ends_failed(monkeypatch, tmp_path):
    import engine.application.repair as repair_mod

    def capped(*a, **k):
        raise repair_mod.FixupError(f"{repair_mod.FIX_ROUNDS_EXHAUSTED_PREFIX}2")

    monkeypatch.setattr(repair_mod, "fix", capped)
    monkeypatch.setattr("engine.application.repair_target.review_findings_for_attempt",
                        lambda ledger, project, attempt: [{"severity": "P1", "file": "src/a.ts", "line": 3, "note": "학원 상담 폼 검증 누락"}])
    ledger = _Ledger()
    failed = FullOutcome("failed", "build:bench_not_passed", "J-bakery#3", None)
    out = auto_pilot.auto_settle(_config(tmp_path), ledger, lambda aid: None, "bakery", "menu-card", failed)
    assert out.status == "failed" and out.reason.startswith("build_review_capped:")
    assert [e.kind for e in ledger.events] == ["build.review_capped"]
    note = (tmp_path / ".gongbang" / "menu-card" / "foreman-handoff.md").read_text(encoding="utf-8")
    assert "구현 검토 상한" in note and "src/a.ts:3" in note and "gb2 close bakery" in note


def test_other_failures_are_handed_back_unchanged(tmp_path):
    failed = FullOutcome("failed", "plan:task T1 owns reserved path", "J-bakery#1", None)
    out = auto_pilot.auto_settle(_config(tmp_path), _Ledger(), lambda aid: None, "bakery", "menu-card", failed)
    assert out is failed


def test_auto_settle_follows_the_full_fallback_twin(monkeypatch, tmp_path):
    """Small job 'lint-setup' fell back to 'lint-setup-full'; the project's
    current attempt is the twin's, so settle must read the twin's position."""
    from engine.application.quick_pipeline import fallback_job_name
    twin = fallback_job_name("lint-setup")
    positions = {
        "lint-setup": SimpleNamespace(stage="small_plan", attempt_id="J-cafe#2", pending_requests=()),
        twin: SimpleNamespace(stage="awaiting_plan_review", attempt_id="J-cafe#9", pending_requests=()),
    }
    monkeypatch.setattr(auto_pilot, "load_position", lambda ledger, project, job: positions.get(job))
    seen = []
    monkeypatch.setattr(auto_pilot, "_advance",
                        lambda config, ledger, fakes, project, job_name, pos, reason, *rest: seen.append((job_name, pos.stage)) or None)
    ledger = _Ledger()
    ledger.project = lambda project: SimpleNamespace(current_attempt_id="J-cafe#9")
    stop = FullOutcome("needs_owner", "plan_review_failed", "J-cafe#9", None)
    out = auto_pilot.auto_settle(_config(tmp_path), ledger, lambda aid: None, "cafe", "lint-setup", stop)
    assert out is stop
    assert seen == [(twin, "awaiting_plan_review")]
