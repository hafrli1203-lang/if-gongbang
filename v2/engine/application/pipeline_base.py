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
from engine.domain.stage_checks import check_facts_trusted
from engine.application.processor import Fakes

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

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


__all__ = [
    "FACTS_ROLE", "PROBER_ROLE", "PLANNER_ROLE", "DESIGNER_ROLE", "BUILDER_ROLE",
    "REVIEWER_ROLE", "EARLY_ROLES", "PROBE_LOG_CAP", "ProgressCallback",
    "FullOutcome", "Run", "FullOrbitError", "checked_facts", "trusted_text", "fakes_for",
]
