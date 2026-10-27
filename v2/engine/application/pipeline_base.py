"""Shared types for the full orbit (see engine.application.pipeline's own module
docstring for the whole picture): the role names every station attempt
is opened with, the outcome/run types every station/resume module
passes around, and the handful of small trust-boundary helpers
(checked_facts/trusted_text/fakes_for) every other full_orbit_* module
needs before it can do anything else.

Nothing here calls into engine.application.pipeline_stages/_release/_early/
_tasks/_resume -- this module is the one every one of them may import
from, never the other way."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from engine.application import task_pipeline as orbit
from engine.infrastructure import trusted
from engine.config import EngineConfig
from engine.domain.plan import Plan, PlanError, Task, load_plan
from engine.domain.stage_checks import check_facts_trusted
from engine.application.processor import Fakes

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger
    from engine.application.job_state import JobPosition

FACTS_ROLE = "facts"


PROBER_ROLE = "prober"


PLANNER_ROLE = "planner"


DESIGNER_ROLE = "designer"


BUILDER_ROLE = "builder"


REVIEWER_ROLE = "reviewer"


# Every role the shared facts/probe/plan/design attempt is opened with.
EARLY_ROLES = (FACTS_ROLE, PROBER_ROLE, PLANNER_ROLE, DESIGNER_ROLE)


# Characters of one probe log kept for the planner prompt's own "실측
# 결과" section -- bounds a single huge/looping log from blowing up the
# planner's prompt budget (same spirit as engine.infrastructure.context's PER_ITEM_CAP).
PROBE_LOG_CAP = 4000


ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class FullOutcome:
    status: str  # "done" | "needs_owner" | "failed"
    reason: str | None
    attempt_id: str | None
    report_path: str | None


@dataclass(frozen=True)
class Run:
    """Everything a resume handler needs besides the position itself."""
    config: EngineConfig
    ledger: Ledger
    fakes: "Fakes | Callable[[str], Fakes]"
    project: str
    job: str
    on_progress: ProgressCallback | None
    processor_factory: "orbit.ProcessorFactory | None"

    def fakes_for(self, attempt_id: str) -> Fakes:
        return fakes_for(self.fakes, attempt_id)

    @property
    def repo(self) -> Path:
        return self.config.projects[self.project].repo


class FullOrbitError(ValueError):
    """Nothing to resume (continue_full_job called with no pending stop
    recorded for this job) or the job's attempt cannot be found."""


def checked_facts(repo: Path, job_name: str) -> tuple[list[str], trusted.Trusted | None]:
    """check_facts' problems and, when there are none, the exact facts.md
    bytes that passed (engine.domain.stage_checks.check_facts_trusted) -- the only
    bytes a later prompt may use."""
    problems, facts = check_facts_trusted(repo, job_name)
    if problems or facts is None:
        return problems or ["facts_missing"], None
    return [], facts


def trusted_text(t: trusted.Trusted, path: Path) -> str | None:
    """The checked bytes, if `path` still holds them; None otherwise."""
    try:
        return trusted.verify(t, path).decode("utf-8")
    except (trusted.TrustViolation, OSError, UnicodeDecodeError):
        return None


def fakes_for(fakes: "Fakes | Callable[[str], Fakes]", attempt_id: str) -> Fakes:
    """`fakes` is either a ready Fakes or a `fakes_for(attempt_id)` factory (the real
    installer's marker directory depends on the attempt, which is only known once
    each attempt is opened)."""
    if isinstance(fakes, Fakes):
        return fakes
    return fakes(attempt_id)


def max_fix_rounds(pos: "JobPosition") -> int:
    return pos.max_fix_rounds


def task_attempt_committed(ledger: "Ledger", project: str, attempt_id: str) -> bool:
    """True iff `attempt_id`'s own "commit" request reached "done" --
    the one durable signal that a build task actually finished. Needed
    because attempt.status alone cannot tell a GENUINELY finished task
    apart from one fixup.close_failed_attempt closed for a repair (the
    SAME "closed" status either way): a task whose review/bench failed
    never reaches the commit stage at all before fixup closes it to
    archive the work and open a replacement attempt."""
    for _seq, event in ledger.events(project, "request.submitted"):
        if event.attempt_id != attempt_id or event.payload.get("kind") != "commit":
            continue
        request_id = event.payload.get("request_id")
        if isinstance(request_id, str) and ledger.request(request_id).status == "done":
            return True
    return False


def task_for(plan: Plan, task_id: str, fixing: bool) -> Task:
    """An acceptance-fix round's synthesized task (owning every plan
    task's paths), or the plan's own task."""
    if fixing:
        all_owns = tuple(sorted({entry for t in plan.tasks for entry in t.owns}))
        return Task(id=task_id, title="수락 실패 수정", detail="", owns=all_owns, depends_on=())
    by_id = {t.id: t for t in plan.tasks}
    task = by_id.get(task_id)
    if task is None:
        raise FullOrbitError(f"unknown task id for resume: {task_id!r}")
    return task


def resumed_plan(run: Run, pos: "JobPosition") -> tuple[FullOutcome | None, Plan | None]:
    """The committed plan, or the failed outcome when it cannot be
    loaded. The plan was already checked against its review before the
    resume dispatched (engine.application.pipeline_resume.resumed_plan_stop)."""
    try:
        plan = load_plan(run.repo, run.job)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", pos.attempt_id, None), None
    return None, plan


def unblock_internal_crash(ledger: "Ledger", project: str, attempt_id: str) -> None:
    """Clears every request this attempt left 'unknown' from an engine-
    internal exception (engine.application.processor.Processor._run_one's own
    catch-all, reason f"internal:{type}") -- Processor._block_on_unknown
    refuses to run ANY request for the whole project while even one
    sits 'unknown' (not just this attempt's own), so a station/task
    that crashed this way can never resume on its own: run_and_merge's/
    run_task_attempt's own submit() reuses that SAME still-'unknown'
    request_id unchanged (submit()'s idempotency only auto-retries a
    'failed' request, never 'unknown'), so every later `gb2 continue`
    just re-blocks on it instead of retrying anything -- the exact bug
    this closes. retry_unknown puts each one back to 'running' so the
    next run_once resumes it through its own step journal (reconcile
    decides whether spawn_worker/await_worker/record_station already
    ran) -- the same safe-to-retry path a transient infra error (e.g.
    OSError) is designed to use. A no-op when the attempt left nothing
    'unknown' (an owner-actionable outcome, e.g. probe_failed/
    plan_review_failed/worker_stuck, never leaves a request 'unknown')."""
    from engine.application.processor import retry_unknown

    for row in ledger.unknown_requests(project):
        if (row.attempt_id == attempt_id
                and str(row.result.get("reason", "")).startswith("internal:")):
            retry_unknown(ledger, row.request_id, "engine_auto_retry")


__all__ = [
    "FACTS_ROLE", "PROBER_ROLE", "PLANNER_ROLE", "DESIGNER_ROLE", "BUILDER_ROLE",
    "REVIEWER_ROLE", "EARLY_ROLES", "PROBE_LOG_CAP", "ProgressCallback",
    "FullOutcome", "Run", "FullOrbitError", "checked_facts", "trusted_text", "fakes_for",
    "max_fix_rounds", "task_attempt_committed", "task_for", "resumed_plan",
    "unblock_internal_crash",
]
