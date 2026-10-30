"""결함 64b: `open_with_intent` (every full/small-job opener goes
through it -- `engine.full_orbit.run_full_job`, `engine.small_orbit.
run_small_job`, every build-task attempt) used to append this job's
own `job.position` event (stage e.g. "facts"/"small_plan", attempt_id
None) BEFORE calling `open_job`, which is the function that actually
refuses anything other than a CLOSED project. A caller against a
project with another job already OPEN (e.g. `gb2 change` run twice
without closing the first) hit exactly that refusal -- but only AFTER
a stray job.position row for a job that was never actually opened had
already been written. This file pins the fix: the CLOSED check now
runs first, so a refused open leaves no trace at all."""
from __future__ import annotations

from dataclasses import replace

import pytest

from engine.application.job_state import STAGE_FACTS, JobPosition
from engine.application.pipeline_stages import open_with_intent
from engine.domain import state
from engine.steps.open_job import open_job
from tests.conftest import Workspace

EARLY_ROLES = ("fact_checker",)


def _intent(job_name: str) -> JobPosition:
    return JobPosition(job=job_name, stage=STAGE_FACTS, attempt_id=None,
                       extra={"request": "x", "max_fix_rounds": 2,
                              "next_action": "run_station"})


def test_open_with_intent_refuses_a_second_job_while_the_project_is_open_without_writing_anything(
    workspace: Workspace,
) -> None:
    open_job(workspace.config, workspace.ledger, "alpha", "job-one", "full", EARLY_ROLES)
    assert workspace.ledger.project("alpha").state == "OPEN"

    before = workspace.ledger.events("alpha", "job.position")

    with pytest.raises(state.ProjectOpenError):
        open_with_intent(workspace.config, workspace.ledger, "alpha",
                         _intent("job-two"), EARLY_ROLES)

    after = workspace.ledger.events("alpha", "job.position")
    assert after == before, (
        "a refused open_with_intent must leave no job.position event behind -- "
        "결함 64b regression"
    )
    assert not any(
        event.payload.get("job") == "job-two" for _seq, event in after
    )


def test_open_with_intent_still_succeeds_against_a_closed_project(
    workspace: Workspace,
) -> None:
    attempt_id = open_with_intent(workspace.config, workspace.ledger, "alpha",
                                  _intent("job-solo"), EARLY_ROLES)
    assert attempt_id is not None
    positions = [
        event for _seq, event in workspace.ledger.events("alpha", "job.position")
        if event.payload.get("job") == "job-solo"
    ]
    assert len(positions) == 1
