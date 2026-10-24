"""Unit tests for engine.steps.report: report.md is bound to both the
attempt_id AND the request_id that wrote it (E5)."""
from __future__ import annotations

from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import RequestRow
from engine.processor import Fakes, StepContext
from engine.steps.open_job import open_job
from engine.steps.report import exists, report_path, write_report
from tests.conftest import Workspace


def _request(request_id: str, attempt_id: str, kind: str = "commit") -> RequestRow:
    return RequestRow(
        request_id=request_id, project="alpha", attempt_id=attempt_id,
        kind=kind, content_hash="h", payload={}, status="running",
        result={}, submitted_seq=1,
    )


def _ctx(workspace: Workspace, attempt_id: str, request: RequestRow) -> StepContext:
    return StepContext(
        config=workspace.config, ledger=workspace.ledger,
        fakes=Fakes(FakePublisher(), FakeInstaller()), request=request,
        project="alpha", attempt_id=attempt_id,
    )


def test_write_report_then_exists_for_same_request(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job1",
                          "full", roles=("builder",))
    request_a = _request("R-aaaa", attempt_id)
    ctx_a = _ctx(workspace, attempt_id, request_a)

    result = write_report(ctx_a)
    assert result["request_id"] == "R-aaaa"
    assert exists(ctx_a) is True


def test_report_from_one_request_does_not_satisfy_another(workspace: Workspace) -> None:
    """E5: report.exists used to match ANY report of the attempt. A report
    written for request A must not satisfy request B's reconcile check,
    even though both share the same attempt_id and report.md path."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job1",
                          "full", roles=("builder",))
    request_a = _request("R-aaaa", attempt_id)
    request_b = _request("R-bbbb", attempt_id)
    ctx_a = _ctx(workspace, attempt_id, request_a)
    ctx_b = _ctx(workspace, attempt_id, request_b)

    write_report(ctx_a)

    assert exists(ctx_a) is True
    assert exists(ctx_b) is False


def test_exists_false_when_no_report_written(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job1",
                          "full", roles=("builder",))
    ctx = _ctx(workspace, attempt_id, _request("R-cccc", attempt_id))
    assert exists(ctx) is False


def test_exists_false_when_file_present_without_event(workspace: Workspace) -> None:
    """#25b: write_report now appends report.written BEFORE writing the
    file, so a crash in between leaves the event with no file (reads as
    "not done" — correct). This test covers the inverse, defense-in-depth
    case directly: a report.md that matches content-wise but has no
    report.written event for this request_id (e.g. written by some other
    means, or left over from before this fix) must still read as "not
    done" — exists() must require the event, not just the file."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job1",
                          "full", roles=("builder",))
    request = _request("R-dddd", attempt_id)
    ctx = _ctx(workspace, attempt_id, request)

    path = report_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Report\n\nattempt: {attempt_id}\nrequest: R-dddd\nkind: commit\n",
        encoding="utf-8",
    )

    assert exists(ctx) is False
