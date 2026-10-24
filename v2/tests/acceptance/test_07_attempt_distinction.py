"""Acceptance 07: re-entering the same job_name (re-submit / small->full
promotion / recovery) produces a new attempt_id and a separate archive;
an old attempt's archive is never touched or skipped for the new one."""
from __future__ import annotations

from engine import ids
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.processor import Fakes, Processor, submit
from engine.steps.open_job import open_job, recover_job
from tests.conftest import Workspace


def _proc(workspace: Workspace) -> Processor:
    proc = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()))
    proc.register_defaults()
    return proc


def _close_dir(workspace: Workspace, attempt_id: str):
    archive_root = workspace.config.home / "archive" / "alpha" / ids.attempt_dirname(attempt_id)
    return [p for p in archive_root.iterdir() if not p.name.startswith(".tmp-")]


def test_reentering_job_name_makes_a_new_attempt_and_archive(workspace: Workspace) -> None:
    attempt1 = open_job(workspace.config, workspace.ledger, "alpha", "fix-a",
                        "full", roles=("builder",))
    assert attempt1.endswith("#1")
    (workspace.alpha / "src" / "app.py").write_text("print('first')\n", encoding="utf-8")
    submit(workspace.ledger, "alpha", attempt1, "close", {"mode": "close"}, "owner")
    n1 = _proc(workspace).run_once("alpha")
    assert n1 == 1

    first_dirs = _close_dir(workspace, attempt1)
    assert len(first_dirs) == 1
    assert (first_dirs[0] / "src" / "app.py").read_text(encoding="utf-8") == "print('first')\n"

    attempt2 = open_job(workspace.config, workspace.ledger, "alpha", "fix-a",
                        "full", roles=("builder",))
    assert attempt2.endswith("#2")
    assert attempt2 != attempt1
    (workspace.alpha / "src" / "app.py").write_text("print('second')\n", encoding="utf-8")
    submit(workspace.ledger, "alpha", attempt2, "close", {"mode": "close"}, "owner")
    n2 = _proc(workspace).run_once("alpha")
    assert n2 == 1

    second_dirs = _close_dir(workspace, attempt2)
    assert len(second_dirs) == 1
    assert (second_dirs[0] / "src" / "app.py").read_text(encoding="utf-8") == "print('second')\n"

    # The first attempt's archive is untouched by the second close.
    first_dirs_again = _close_dir(workspace, attempt1)
    assert len(first_dirs_again) == 1
    assert first_dirs_again[0] == first_dirs[0]
    assert (first_dirs_again[0] / "src" / "app.py").read_text(encoding="utf-8") == "print('first')\n"


def test_third_attempt_archive_not_skipped_when_earlier_archives_exist(workspace: Workspace) -> None:
    attempt1 = open_job(workspace.config, workspace.ledger, "alpha", "fix-b",
                        "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('r1')\n", encoding="utf-8")
    submit(workspace.ledger, "alpha", attempt1, "close", {"mode": "close"}, "owner")
    _proc(workspace).run_once("alpha")

    attempt2 = open_job(workspace.config, workspace.ledger, "alpha", "fix-b",
                        "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('r2')\n", encoding="utf-8")
    submit(workspace.ledger, "alpha", attempt2, "close", {"mode": "close"}, "owner")
    _proc(workspace).run_once("alpha")

    attempt3 = open_job(workspace.config, workspace.ledger, "alpha", "fix-b",
                        "full", roles=("builder",))
    assert attempt3.endswith("#3")
    (workspace.alpha / "src" / "app.py").write_text("print('r3')\n", encoding="utf-8")
    submit(workspace.ledger, "alpha", attempt3, "close", {"mode": "close"}, "owner")
    n3 = _proc(workspace).run_once("alpha")
    assert n3 == 1

    dirs3 = _close_dir(workspace, attempt3)
    assert len(dirs3) == 1
    assert (dirs3[0] / "src" / "app.py").read_text(encoding="utf-8") == "print('r3')\n"
    for earlier, content in ((attempt1, "r1"), (attempt2, "r2")):
        dirs = _close_dir(workspace, earlier)
        assert len(dirs) == 1
        assert f"print('{content}')" in (dirs[0] / "src" / "app.py").read_text(encoding="utf-8")


def test_actual_recovery_makes_new_attempt_and_separate_archive(workspace: Workspace) -> None:
    """Actual recovery driven end to end: open -> worker change ->
    recover_job (interrupt) -> report -> close produces a NEW attempt
    with its OWN archive
    directory, never the interrupted attempt's (which is left exactly as
    recover_job found it -- no archive of its own, since it was never
    closed). The worker's change was made on the ORIGINAL (now
    interrupted) attempt, before recovery -- recover_job inherits
    dirty_paths/base_head from that attempt's own job.opened rather than
    recomputing them, so this change is still this job's own work, not
    pre-existing owner dirt, and close must still archive + reset it."""
    attempt1 = open_job(workspace.config, workspace.ledger, "alpha", "fix-recover",
                        "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('recovered')\n", encoding="utf-8")

    attempt2 = recover_job(workspace.config, workspace.ledger, "alpha", "owner")
    assert attempt2 != attempt1
    assert attempt2.endswith("#2")
    assert workspace.ledger.attempt(attempt1).status == "interrupted"
    assert workspace.ledger.project("alpha").state == "RECOVERING"

    proc = _proc(workspace)
    submit(workspace.ledger, "alpha", attempt2, "report", {"summary": "recovered"}, "owner")
    n1 = proc.run_once("alpha")
    assert n1 == 1

    submit(workspace.ledger, "alpha", attempt2, "close", {"mode": "close"}, "owner")
    n2 = proc.run_once("alpha")
    assert n2 == 1
    assert workspace.ledger.project("alpha").state == "CLOSED"

    archive2 = _close_dir(workspace, attempt2)
    assert len(archive2) == 1
    assert (archive2[0] / "src" / "app.py").read_text(encoding="utf-8") == "print('recovered')\n"
    # reset_to_head put the working tree back at HEAD.
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"

    archive1_root = workspace.config.home / "archive" / "alpha" / attempt1
    assert not archive1_root.exists()
