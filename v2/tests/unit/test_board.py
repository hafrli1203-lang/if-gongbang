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
    # handoff_written alone is never 완료 (see
    # test_handoff_written_alone_is_not_yet_완료 below) -- a real
    # completion also needs the release decision engine.full_orbit
    # always records right after security passes (here: a project
    # with no release track configured, "not_configured").
    ledger.append(Event(
        project=project, kind="full_orbit.release_decision", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "decision": "not_configured"},
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


def test_handoff_written_alone_is_not_yet_완료(tmp_path):
    """A crash right after handoff.write_handoff appends
    'job.handoff_written' but before engine.full_orbit._finish_after_
    handoff gets to record ANYTHING else (the app-wide security check
    crashed, or never even started) must never read as 완료 -- security
    and the release decision both still have to happen, and neither one
    has, per the ledger's own evidence. See board._derive_job_status's
    own docstring."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="job.handoff_written", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1", payload={"job": "add-login"},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status != "완료"
    assert board.jobs[0].status == "진행 중"


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


def test_load_app_board_needs_owner_status_includes_stop_reason(tmp_path):
    """a needs_owner stop recorded with its own
    stop_reason shows it on the board, not just the generic label."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "attempt_id": "job-1.1",
                 "stage": "awaiting_probe_retry", "stop_reason": "probe_failed:stripe"},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "사람 확인 대기: probe_failed:stripe"


def test_load_app_board_failed_status_from_job_metrics_outcome(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="job.metrics", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"durations_ms": {}, "test_runs": {}, "git_calls": 0,
                 "outcome": "failed", "failed_stage": "bench"},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "실패"


def test_render_board_html_shows_failed_badge(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="job.metrics", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"durations_ms": {}, "test_runs": {}, "git_calls": 0, "outcome": "failed"},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)
    page = render_board_html([board], generated_ts=1_700_000_000.0)

    assert "badge-neg" in page
    assert "실패" in page


def test_ship_clears_the_stale_release_approval_waiting_status(tmp_path):
    """`gb2 ship` records a full_orbit.release_decision
    "shipped" event strictly AFTER the "awaiting_release_approval"
    progress event that first parked the job -- the board must show the
    later, real outcome (배포됨), never the now-stale "사람 확인 대기"
    just because that progress event still exists earlier in the
    log."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "attempt_id": "job-1.1",
                 "stage": "awaiting_release_approval", "stop_reason": "release_approval"},
    ))
    ledger.append(Event(
        project=project, kind="job.handoff_written", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1", payload={"job": "add-login"},
    ))
    ledger.append(Event(
        project=project, kind="full_orbit.release_decision", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "decision": "shipped"},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "배포됨"


def test_skip_ship_clears_the_stale_release_approval_waiting_status(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "attempt_id": "job-1.1",
                 "stage": "awaiting_release_approval", "stop_reason": "release_approval"},
    ))
    ledger.append(Event(
        project=project, kind="full_orbit.release_decision", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "decision": "skipped"},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "건너뜀"


def test_release_followup_failure_clears_the_stale_release_approval_waiting_status(tmp_path):
    """If `gb2 ship`'s own report/close followup fails after approval (no
    full_orbit.release_decision is ever recorded in that case -- see
    cmd_ship's own docstring), the job.metrics "failed" event it leaves
    behind must still win over the earlier "awaiting_release_approval"
    progress event -- a job must never show "사람 확인 대기" once it has
    plainly already failed."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "attempt_id": "job-1.1",
                 "stage": "awaiting_release_approval", "stop_reason": "release_approval"},
    ))
    ledger.append(Event(
        project=project, kind="job.metrics", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"durations_ms": {}, "test_runs": {}, "git_calls": 0,
                 "outcome": "failed", "failed_stage": "close"},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "실패"


def test_ship_publish_failure_clears_the_stale_release_approval_waiting_status(tmp_path):
    """P2: `tools/gb2`'s `ship` drives the pending publish request all
    the way to a terminal "failed"/"unknown" status and then returns
    (exit 3) WITHOUT ever journaling report/close, a job.metrics
    "failed" outcome, or a full_orbit.release_decision -- nothing else
    on the events table ever supersedes the earlier
    "awaiting_release_approval" progress event. The board must still
    derive 실패 straight from that SAME request_id's own CURRENT status
    in the ledger's requests table, never keep showing "사람 확인 대기"
    for a publish that will never be approved again."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "attempt_id": "job-1.1",
                 "stage": "awaiting_release_approval", "stop_reason": "release_approval",
                 "request_id": "r-publish-1"},
    ))
    ledger.append(Event(
        project=project, kind="request.submitted", actor_kind="owner",
        actor_id="owner", attempt_id="job-1.1",
        payload={"request_id": "r-publish-1", "kind": "publish",
                 "content_hash": "h1", "payload": {}},
    ))
    ledger.append(Event(
        project=project, kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"request_id": "r-publish-1", "status": "failed",
                 "result": {"reason": "deploy_script_failed"}},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "실패"


def test_ship_approval_refusal_still_shows_waiting_not_failed(tmp_path):
    """The counterpart to the test above: a request that is still
    "queued" (e.g. ship's own approval-refused/wrong-approval-text
    retry path, which never touches the request at all) must keep
    showing "사람 확인 대기" -- only a REAL terminal failed/unknown
    request status ever overrides that label."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "attempt_id": "job-1.1",
                 "stage": "awaiting_release_approval", "stop_reason": "release_approval",
                 "request_id": "r-publish-2"},
    ))
    ledger.append(Event(
        project=project, kind="request.submitted", actor_kind="owner",
        actor_id="owner", attempt_id="job-1.1",
        payload={"request_id": "r-publish-2", "kind": "publish",
                 "content_hash": "h2", "payload": {}},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "사람 확인 대기: release_approval"


def test_security_fix_stop_after_handoff_shows_waiting_not_완료(tmp_path):
    """The exact real-flow case: handoff is written, THEN the app-wide
    security check actually runs and fails, recording its own
    "awaiting_security_fix" progress stop. That later event's seq must
    win -- the job is correctly still waiting on the owner to fix a
    security finding, not 완료."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="job.handoff_written", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1", payload={"job": "add-login"},
    ))
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "attempt_id": "job-1.1",
                 "stage": "awaiting_security_fix", "stop_reason": "security_failed"},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "사람 확인 대기: security_failed"


# ---------------------------------------------------------------------
# P1 cutover: 'job.position' (engine.position.record_position) replaces
# 'full_orbit.cursor'/'full_orbit.progress' -- this module must read
# BOTH kinds (old ledgers only ever have the old one) and pick whichever
# has the newest seq, same as it already does across every other event
# kind pair here. See board._POSITION_EVENT_KINDS' own docstring.
# ---------------------------------------------------------------------

def test_job_position_awaiting_stage_shows_waiting(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="job.position", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "stage": "awaiting_design_approval",
                 "attempt_id": "job-1.1", "task_id": None, "remaining_task_ids": [],
                 "fixing": False, "fix": None, "pending_requests": [],
                 "stop_reason": None, "extra": {}},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "사람 확인 대기"


def test_job_position_stop_reason_shown(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="job.position", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "stage": "awaiting_probe_retry",
                 "attempt_id": "job-1.1", "task_id": None, "remaining_task_ids": [],
                 "fixing": False, "fix": None, "pending_requests": [],
                 "stop_reason": "probe_failed:stripe", "extra": {}},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "사람 확인 대기: probe_failed:stripe"


def test_job_position_request_id_lives_under_extra(tmp_path):
    """The P1 payload shape moved request_id under payload["extra"] --
    a request that already ended failed/unknown in the requests table
    must still override "사람 확인 대기" with 실패, the same as the old
    full_orbit.progress shape (top-level request_id) already did."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="job.position", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "stage": "awaiting_release_approval",
                 "attempt_id": "job-1.1", "task_id": None, "remaining_task_ids": [],
                 "fixing": False, "fix": None, "pending_requests": [],
                 "stop_reason": "release_approval", "extra": {"request_id": "r-pub-9"}},
    ))
    ledger.append(Event(
        project=project, kind="request.submitted", actor_kind="owner",
        actor_id="owner", attempt_id="job-1.1",
        payload={"request_id": "r-pub-9", "kind": "publish",
                 "content_hash": "h1", "payload": {}},
    ))
    ledger.append(Event(
        project=project, kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"request_id": "r-pub-9", "status": "failed",
                 "result": {"reason": "deploy_script_failed"}},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "실패"


def test_job_board_counts_ai_requests_and_fix_rounds(tmp_path):
    """G16 (gongbang_directives.md): the board must show, per job, how
    many requests the engine actually sent to an AI worker, and how
    many fix rounds the job went through -- not just the raw attempt
    count. 'AI 요청' is every request.submitted event whose own
    payload["kind"] is one of the two request kinds that spawn a
    worker runner (engine.application.processor.KNOWN_KINDS +
    engine.application.steps.KINDS: "run_station" -> worker_step.py,
    "review" -> review.py); "close"/"commit"/"bench"/... do not spawn a
    worker and must not be counted. 'fix 회차' is attempt_count - 1 (n=1
    is the job's own first attempt, every attempt after that is a fix
    round) -- see this job opening TWO attempts below."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="request.submitted", actor_kind="owner",
        actor_id="owner", attempt_id="job-1.1",
        payload={"request_id": "r-build", "kind": "run_station",
                 "content_hash": "h1", "payload": {"station": "build"}},
    ))
    ledger.append(Event(
        project=project, kind="request.submitted", actor_kind="owner",
        actor_id="owner", attempt_id="job-1.1",
        payload={"request_id": "r-review", "kind": "review",
                 "content_hash": "h2", "payload": {}},
    ))
    ledger.append(Event(
        project=project, kind="request.submitted", actor_kind="owner",
        actor_id="owner", attempt_id="job-1.1",
        payload={"request_id": "r-close", "kind": "close",
                 "content_hash": "h3", "payload": {}},
    ))
    ledger.append(Event(
        project=project, kind="job.opened", actor_kind="owner", actor_id="owner",
        attempt_id="job-1.2",
        payload={"job_id": "job-1", "job_name": "add-login", "attempt_id": "job-1.2",
                 "n": 2, "kind": "full", "generation": 2, "dirty_paths": []},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert len(board.jobs) == 1
    job = board.jobs[0]
    assert job.attempt_count == 2
    assert job.ai_request_count == 2
    assert job.fix_round_count == 1


def test_job_board_tokens_out_is_none_when_ledger_never_records_it(tmp_path):
    """No station.finished event today ever carries an output-token
    count (engine.infrastructure.workers.base.parse_cli_shaped_result only
    pulls total_cost_usd/num_turns off the CLI's raw JSON, never
    usage.output_tokens) -- the board must show "기록 없음", never a
    guessed number, until that wiring exists."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"request_id": "r1", "station": "build", "role": "builder",
                 "ok": True, "summary": "ok", "cost_usd": 1.0, "turns": 2,
                 "changed_paths": [], "requests": []},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].tokens_out is None


def test_job_board_tokens_out_sums_when_station_finished_does_carry_it(tmp_path):
    """Once a caller DOES record tokens_out on station.finished (see
    this module's own TODO note on parse_cli_shaped_result), the board
    must add it up across the job's stations rather than ignore it."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"request_id": "r1", "station": "build", "role": "builder",
                 "ok": True, "summary": "ok", "cost_usd": 1.0, "turns": 2,
                 "changed_paths": [], "requests": [], "tokens_out": 1200},
    ))
    ledger.append(Event(
        project=project, kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"request_id": "r2", "station": "review", "role": "reviewer",
                 "ok": True, "summary": "ok", "cost_usd": 0.5, "turns": 1,
                 "changed_paths": [], "requests": [], "tokens_out": 300},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].tokens_out == 1500


def test_board_html_shows_ai_requests_fix_rounds_and_tokens_columns(tmp_path):
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="request.submitted", actor_kind="owner",
        actor_id="owner", attempt_id="job-1.1",
        payload={"request_id": "r-build", "kind": "run_station",
                 "content_hash": "h1", "payload": {}},
    ))
    ledger.append(Event(
        project=project, kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"request_id": "r-build", "station": "build", "role": "builder",
                 "ok": True, "summary": "ok", "cost_usd": 1.0, "turns": 2,
                 "changed_paths": [], "requests": []},
    ))
    ledger.close()

    apps = [load_app_board("alpha", ledger_path, project)]
    html_out = render_board_html(apps, generated_ts=0.0)

    assert "요청" in html_out
    assert "수정 회차" in html_out
    assert "출력 토큰" in html_out
    assert "기록 없음" in html_out  # this job's tokens_out is None


def test_job_position_newest_seq_wins_over_an_older_full_orbit_progress(tmp_path):
    """A ledger that straddles the P1 cutover (old full_orbit.progress
    events from before, new job.position events from after) must still
    pick whichever event has the newest seq, regardless of which of
    the two kinds it is."""
    project = "alpha"
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(ledger_path, project)
    _open_job(ledger, project, "job-1", "add-login", "job-1.1")
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "attempt_id": "job-1.1",
                 "stage": "awaiting_release_approval", "stop_reason": "release_approval"},
    ))
    ledger.append(Event(
        project=project, kind="job.position", actor_kind="processor",
        actor_id="processor", attempt_id="job-1.1",
        payload={"job": "add-login", "stage": "awaiting_security_fix",
                 "attempt_id": "job-1.1", "task_id": None, "remaining_task_ids": [],
                 "fixing": False, "fix": None, "pending_requests": [],
                 "stop_reason": "security_failed", "extra": {}},
    ))
    ledger.close()

    board = load_app_board("alpha", ledger_path, project)

    assert board.jobs[0].status == "사람 확인 대기: security_failed"
    assert board.jobs[0].stage == "awaiting_security_fix"
