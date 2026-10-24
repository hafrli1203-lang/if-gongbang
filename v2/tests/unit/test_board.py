"""Unit tests for engine.board: a synthetic ledger (no repo, no
EngineConfig needed) in, a static HTML page out. Covers the plan's own
test list for W3: each job appears, day headings appear, and untrusted
text (a lesson title / event payload) is HTML-escaped rather than
injected raw."""
from __future__ import annotations

import json

import pytest

from engine.board import (
    build_board, group_lessons, load_app_board, render_board_html,
)
from engine.ledger import Event, Ledger


def _make_ledger(path, project: str) -> Ledger:
    ledger = Ledger.create(path)
    ledger.register_project(project)
    return ledger


def _open_job(ledger: Ledger, project: str, job_id: str, job_name: str,
              attempt_id: str, ts_shift: float = 0.0) -> None:
    ledger.append(Event(
        project=project, kind="job.opened", actor_kind="owner", actor_id="owner",
        attempt_id=attempt_id,
        payload={"job_id": job_id, "job_name": job_name, "attempt_id": attempt_id,
                 "n": 1, "kind": "full", "generation": 1, "dirty_paths": []},
    ))


def test_load_app_board_builds_job_rows_with_status_stage_cost(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"request_id": "r1", "station": "build", "role": "builder",
                 "ok": True, "summary": "ok", "cost_usd": 1.25, "turns": 3,
                 "changed_paths": [], "requests": []},
    ))
    ledger.append(Event(
        project=project, kind="job.handoff_written", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1", payload={"job": "add-login"},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.error is None
    assert len(board.jobs) == 1
    job = board.jobs[0]
    assert job.job_name == "add-login"
    assert job.status == "완료"
    assert job.stage == "build"
    assert job.cost_usd == pytest.approx(1.25)


def test_a_per_attempt_report_written_alone_does_not_mark_the_job_완료(tmp_path):
    """engine.steps.report.write_report appends a generic 'report.written'
    event for EVERY attempt's own "report" stage -- facts/plan/design
    and each build task each open their own attempt and each get one.
    Only engine.handoff.write_handoff's own 'job.handoff_written' event
    (appended once, for the whole job) may mark 완료; a per-task
    report.written with no matching handoff event must leave the job
    reading as still in progress."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="report.written", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1", payload={},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "진행 중"


def test_load_app_board_needs_owner_status(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "attempt_id": "job-1.1",
                 "stage": "awaiting_design_approval"},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert len(board.jobs) == 1
    assert board.jobs[0].status == "사람 확인 대기"
    assert board.jobs[0].cost_usd is None


def test_missing_ledger_file_is_reported_not_raised(tmp_path):
    board = load_app_board("ghost", tmp_path / "no-such-ledger.sqlite", "ghost")
    assert board.jobs == ()
    assert board.error is not None


def test_render_board_html_lists_each_job_and_day_heading(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    _open_job(ledger, project, "job-2", "fix-nav", "job-2.1")
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)
    page = render_board_html([board], generated_ts=1_700_000_000.0)

    assert "add-login" in page
    assert "fix-nav" in page
    assert '<div class="day">' in page  # at least one day heading rendered


def test_render_board_html_escapes_event_payload_text(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "<script>alert(1)</script>", "job-1.1")
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)
    page = render_board_html([board], generated_ts=1_700_000_000.0)

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_render_board_html_escapes_lessons_and_queue_text():
    lessons = [{"title": "<img src=x onerror=alert(1)> 로그인 버그"}]
    page = render_board_html([], lessons=lessons, queue_text="<b>TASK</b> 할일",
                             generated_ts=1_700_000_000.0)
    assert "<img src=x" not in page
    assert "&lt;img" in page
    assert "<b>TASK</b>" not in page
    assert "&lt;b&gt;TASK&lt;/b&gt;" in page


def test_group_lessons_is_deterministic_keyword_grouping():
    lessons = [
        {"title": "로그인 버그 재발"},
        {"title": "로그인 안 되는 문제"},
        {"title": "결제 실패 원인"},
    ]
    groups = group_lessons(lessons)
    # Same input, called twice, must give identical grouping (no
    # randomness / no model call).
    assert group_lessons(lessons) == groups
    assert sum(len(v) for v in groups.values()) == 3


def test_build_board_handles_missing_optional_files(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.close()

    page = build_board(
        {"alpha": (ledger_path, project)},
        lessons_path=tmp_path / "missing-lessons.json",
        queue_path=tmp_path / "missing-queue.md",
        generated_ts=1_700_000_000.0,
    )
    assert "add-login" in page
    assert "<html" in page


def test_build_board_reads_lessons_and_queue_when_present(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    ledger.close()

    lessons_path = tmp_path / "lessons.json"
    lessons_path.write_text(json.dumps([{"title": "배포 실패 원인"}]), encoding="utf-8")
    queue_path = tmp_path / "queue.md"
    queue_path.write_text("# WORK_QUEUE\n- TASK 1\n", encoding="utf-8")

    page = build_board(
        {"alpha": (ledger_path, project)},
        lessons_path=lessons_path, queue_path=queue_path,
        generated_ts=1_700_000_000.0,
    )
    assert "배포 실패 원인" in page
    assert "WORK_QUEUE" in page


def test_load_app_board_works_when_ledger_path_contains_a_question_mark(tmp_path):
    """Path.as_posix() does not percent-encode a sqlite URI's reserved
    characters -- a ledger path containing '?' would otherwise either
    inject an extra (bogus) URI query parameter after 'mode=ro' or
    fail to open outright. Path.as_uri() percent-encodes it correctly."""
    project = "alpha"
    odd_dir = tmp_path / "weird?dir"
    odd_dir.mkdir()
    ledger_path = odd_dir / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.error is None
    assert len(board.jobs) == 1
