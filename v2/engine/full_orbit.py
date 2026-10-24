"""The full orbit (docs/v2.md 「궤도 9칸」, on top of the existing single-
attempt engine): run one job all the way from a plain-text request to a
usable app with a hand-off report, or stop cleanly for the owner (a
design to approve, an install/approval a builder station asked for) and
resume later.

Stations, in order, each writing only its own files:
  facts    (role "facts")    -> .gongbang/<job>/facts.md
  probe    (role "prober", only if facts.md's own "## 외부 대상"
           section lists at least one external API/CLI/file format
           this job depends on) -> .gongbang/<job>/probe/ -- one real
           call per target, logged with "PROBE OK"/"PROBE FAIL"
           (engine.gates.check_probe_targets/check_probe). Skipped
           entirely (no attempt, just a "probe.skipped" ledger event)
           when facts.md names no such target.
  planner  (role "planner")  -> .gongbang/<job>/plan.json, tests/scenes/
  designer (role "designer", only if plan.ui) -> .gongbang/<job>/design/
  builder  (role "builder", once per plan.tasks entry, in dependency
           order) -> the task's own `owns` paths, narrowed from the
           project's configured build prefixes (engine.policy,
           engine.steps.station) -- never tests/scenes/. A UI task
           additionally gets write access to this one job's own
           .gongbang/<job>/screens/ (see _config_for_build_task below)
           to save screenshots of what it built, without widening any
           other builder-writable path.
  reviewer (role "reviewer") judges each builder task's change, same as
           a plain single-task job (engine.steps.review)

Facts/probe/planner/(designer) share ONE attempt (run_station -> merge
-> a gate-specific check -- engine.gates / engine.plan -- for each),
closed once the plan is validated and (if plan.ui) the design is
approved. facts.md/probe logs/plan.json/design.md are committed
directly (see
_commit_job_trail) right after each one's merge, rather than waiting
for a worker-gated commit -- nothing a worker wrote is in that diff,
only files the orchestrator itself just wrote, so there is nothing for
a reviewer or the test suite to re-judge.

EACH build task (and each acceptance-fix round) then runs in its OWN
FRESH attempt: open_job -> run_station(build) -> merge -> review ->
bench -> commit -> report -> close (the same seven-stage shape
engine.orbit.default_stages() already uses for a plain single-task
job). A fresh attempt per task, rather than reusing one across all of
them, is required by engine.steps.merge's own conflict check, which
compares a copy's changes against the ATTEMPT's base_head (fixed at
open_job time), never the project's live HEAD -- reusing one attempt
across several tasks' own commits would make every task after the
first look like it conflicts with "someone else's committed change"
(that change being its OWN earlier sibling task, in the very same
job). engine.fixup.py already handles this exact base_head fact for a
fix/resume attempt the same way: close, then open_job() again, never
continue on the old attempt.

A design that is not yet approved, or a run_station that asked the
owner for something (e.g. an install), stops the job with
FullOutcome(status="needs_owner", ...) -- `continue_full_job` resumes
it later. Progress needed to resume (which stage, which task, which
attempt) is journaled as a "full_orbit.progress" ledger event; nothing
about it lives only in this process's memory. Which attempt to resume
is always "the latest attempt for this job_name" (engine.ledger.
Ledger.attempts_for_job_name) -- the same attempt the progress event
itself was journaled against, since a needs_owner stop never closes
the attempt it stopped in.

`processor_factory`, on both entry points, is the same optional seam
engine.orbit.run_job/continue_job already expose (a callable building
the engine.processor.Processor each stage list runs against) -- plain
production callers (tools/gb2) never pass it (defaults to a real
Processor with register_defaults()); it exists so a test can register
a deterministic run_station/review stub instead of spawning a real
worker/reviewer process, the same way tests/unit/test_orbit.py already
does for engine.orbit itself. It is NOT part of the fixed full-orbit
interface contract -- gb2 never needs to pass it -- but adding an
optional keyword-only parameter does not change the documented
positional/keyword call shape either.

Known limitation: if the project has a `release` track configured
(EngineConfig.projects[...].release), engine.orbit's own commit-stage
special case (engine.orbit._run_stages) stops EVERY task's own attempt
at needs_owner right after its commit, not only the last -- this
module does not special-case that away. A release-configured project
driven through the full orbit today needs an owner to approve/skip-ship
after each task, same as a plain single-task job would."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from typing import Callable

from engine import context as context_mod
from engine import orbit
from engine.config import EngineConfig
from engine.gates import _facts_path as _facts_md_path
from engine.gates import check_design_approved, check_facts, check_probe, check_probe_targets
from engine.gitutil import git_argv
from engine.ledger import Event, Ledger
from engine.plan import (
    Plan, PlanError, Task, classify_skill_tools, load_plan, ordered_tasks, skill_catalog_text,
    tools_for_station,
    validate_plan, validate_tools,
)
from engine.processor import Fakes
from engine.station_prompts import (
    builder_task_text, designer_prompt, facts_prompt, fix_task_text, planner_prompt,
    prober_prompt,
)
from engine.steps.open_job import copy_root, open_job

FACTS_ROLE = "facts"
PROBER_ROLE = "prober"
PLANNER_ROLE = "planner"
DESIGNER_ROLE = "designer"
BUILDER_ROLE = "builder"
REVIEWER_ROLE = "reviewer"

# Characters of one probe log kept for the planner prompt's own "실측
# 결과" section -- bounds a single huge/looping log from blowing up the
# planner's prompt budget (same spirit as engine.context's PER_ITEM_CAP).
PROBE_LOG_CAP = 4000

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class FullOutcome:
    status: str  # "done" | "needs_owner" | "failed"
    reason: str | None
    attempt_id: str | None
    report_path: str | None


class FullOrbitError(ValueError):
    """Nothing to resume (continue_full_job called with no pending stop
    recorded for this job) or the job's attempt cannot be found."""


def _facts_md_text(repo, job_name: str) -> str:
    try:
        return _facts_md_path(repo, job_name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _record_progress(ledger: Ledger, project: str, attempt_id: str, job_name: str,
                     **fields: object) -> None:
    ledger.append(Event(
        project=project, kind="full_orbit.progress", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job_name, "attempt_id": attempt_id, **fields},
    ))


def _load_progress(ledger: Ledger, project: str, attempt_id: str,
                   job_name: str) -> dict[str, object] | None:
    latest: dict[str, object] | None = None
    for _seq, event in ledger.events(project, "full_orbit.progress"):
        if event.payload.get("attempt_id") == attempt_id and event.payload.get("job") == job_name:
            latest = dict(event.payload)
    return latest


_ORCHESTRATOR_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "processor", "GIT_AUTHOR_EMAIL": "processor@local",
    "GIT_COMMITTER_NAME": "processor", "GIT_COMMITTER_EMAIL": "processor@local",
}


def _latest_merge_paths(ledger: Ledger, project: str, attempt_id: str) -> tuple[str, ...]:
    """The exact relative paths engine.steps.merge's own "record" step
    applied in the MOST RECENT merge.recorded event on this attempt
    (that event's own "paths" payload, engine.steps.merge._record) --
    not every path under some owned prefix that happens to exist on
    disk. facts/probe/plan/design all share one attempt (see this
    module's own docstring), each running its own run_station -> merge
    in turn, so "most recent" at the moment _commit_job_trail is called
    right after a merge always means THAT station's own merge, never an
    earlier sibling's."""
    latest: tuple[str, ...] = ()
    for _seq, event in ledger.events(project, "merge.recorded"):
        if event.attempt_id == attempt_id:
            latest = tuple(str(p) for p in event.payload.get("paths", ()))
    return latest


def _commit_job_trail(config: EngineConfig, ledger: Ledger, project: str, attempt_id: str | None,
                      job_name: str, message: str,
                      station: str | None = None, role: str | None = None) -> None:
    """Commit whatever the orchestrator itself (never a worker) just
    wrote under .gongbang/<job>/ -- a merge's own apply_allowed_paths
    step already landed facts.md/plan.json/design.md/report.md there
    as plain, uncommitted files in the real repo, and leaving them
    uncommitted would both (a) make the NEXT station's own base_head
    stale (see this module's docstring) and (b) let engine.orbit's own
    "close" stage (git checkout/clean to HEAD) wipe them right back
    out. This is a plain, UNGATED engine-side commit -- unlike
    engine.steps.commit, it never checks a review/bench fingerprint,
    because nothing a worker wrote is in this diff; only files the
    processor itself just wrote (plus, when `station`/`role` is given,
    exactly the paths THIS station's own merge just applied outside
    .gongbang/ -- see _latest_merge_paths; never every path that
    happens to exist under the role's whole owned prefix, which could
    sweep in some unrelated pre-existing uncommitted change under that
    same prefix, e.g. from the owner's own working tree or a different
    job's leftover file). A no-op (nothing actually changed under
    .gongbang/<job>/) commits nothing."""
    repo = config.projects[project].repo
    paths = [f".gongbang/{job_name}"]
    if station and role:
        # Other paths this station's role owns (e.g. the planner's
        # tests/scenes/) were merged by the same gated step; leaving them
        # uncommitted would let the close stage wipe them -- but only the
        # paths that merge actually touched, not the whole owned prefix.
        owned = config.station_owners.get(station, {}).get(role, ())
        extra_prefixes = tuple(p for p in owned if not p.startswith(".gongbang"))
        if extra_prefixes:
            merged = _latest_merge_paths(ledger, project, attempt_id)
            paths += [
                p for p in merged
                if any(p.startswith(prefix) for prefix in extra_prefixes) and (repo / p).exists()
            ]
    env = {**os.environ, **_ORCHESTRATOR_GIT_IDENTITY}
    subprocess.run(git_argv(repo, "add", "--", *paths), check=True, capture_output=True, env=env)
    status = subprocess.run(
        git_argv(repo, "status", "--porcelain", "--", *paths),
        capture_output=True, text=True, env=env,
    ).stdout
    if not status.strip():
        return
    subprocess.run(git_argv(repo, "commit", "-q", "-m", message), check=True,
                   capture_output=True, env=env)


def _build_prefixes(config: EngineConfig) -> tuple[str, ...]:
    return tuple(config.station_owners.get("build", {}).get(BUILDER_ROLE, ()))


def _probe_logs_text(repo, job_name: str, targets: list[tuple[str, str]]) -> str:
    """Every target's own probe log (capped at PROBE_LOG_CAP chars
    each), under one "## 실측 결과" heading -- "" if `targets` is empty
    or none of the logs can be read (check_probe already gated on
    them existing and saying PROBE OK before this is ever called, so
    a read failure here would mean the file vanished in between)."""
    parts: list[str] = []
    for name, _url in targets:
        path = repo / ".gongbang" / job_name / "probe" / f"{name}.log"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parts.append(f"### {name}\n{text[:PROBE_LOG_CAP]}")
    if not parts:
        return ""
    return "## 실측 결과\n" + "\n\n".join(parts)


def _config_for_build_task(config: EngineConfig, job_name: str, plan: Plan) -> EngineConfig:
    """`config`, unchanged, except (only when plan.ui) the build
    station's builder role additionally owns THIS job's own
    .gongbang/<job_name>/screens/ -- never the rest of
    .gongbang/<job_name>/ (facts.md, plan.json, report.md stay out of
    reach) and never another job's screens/, so a UI task's build
    attempt can save its own screenshots there without widening any
    other builder-writable path. Used only for the run_station/merge
    stages of that one task's own attempt (see _run_task_attempt) --
    every other stage (review/bench/commit/...) only reads fields this
    never touches, so passing it there too would be harmless but is
    avoided for clarity."""
    if not plan.ui:
        return config
    screens_prefix = f".gongbang/{job_name}/screens/"
    build_owners = dict(config.station_owners.get("build", {}))
    build_owners[BUILDER_ROLE] = tuple(build_owners.get(BUILDER_ROLE, ())) + (screens_prefix,)
    station_owners = {**config.station_owners, "build": build_owners}
    return replace(config, station_owners=station_owners)


def _record_context(ledger: Ledger, project: str, attempt_id: str | None, station: str,
                    sc: "context_mod.StationContext") -> None:
    """Appends the context.injected ledger event for this station's
    prompt (and, if the library lookup failed, context.failed right
    after it) -- see engine.context.build's own docstring for what each
    field means. `attempt_id` may be None (a build/fix task's attempt
    is only opened AFTER its prompt text is built -- see
    engine.full_orbit's own module docstring on fresh-attempt-per-task)."""
    ledger.append(Event(
        project=project, kind="context.injected", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={
            "station": station,
            "items": [
                {"kind": i.kind, "title": i.title, "source": i.source, "trust": i.trust}
                for i in sc.injected
            ],
            "skills": list(sc.skills), "missing": list(sc.missing),
            "base": list(sc.base), "planned": list(sc.planned),
        },
    ))
    if sc.failed:
        ledger.append(Event(
            project=project, kind="context.failed", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id, payload={"station": station},
        ))


def _planned_skills(config: EngineConfig, plan: Plan, station: str) -> tuple[str, ...]:
    """The skills this job's planner picked for `station` that the engine
    can actually load (needs_install ones are never injected)."""
    usable, _needs_install = classify_skill_tools(plan, config)
    return tuple(tools_for_station(plan, station, usable_skills=usable)["skill"])


def _prompt_with_context(config: EngineConfig, ledger: Ledger, project: str,
                         attempt_id: str | None, station: str, query: str,
                         prompt: str, planned_skills: tuple[str, ...] = ()) -> str:
    """Builds this station's injected-context appendix (engine.context.
    build), records it on the ledger, and returns `prompt` with that
    appendix appended (unchanged if there is nothing to inject)."""
    sc = context_mod.build(config, project, station, query, planned_skills)
    _record_context(ledger, project, attempt_id, station, sc)
    if not sc.text:
        return prompt
    return f"{prompt}\n\n{sc.text}"



def _fakes_for(fakes: "Fakes | Callable[[str], Fakes]", attempt_id: str) -> Fakes:
    """`fakes` is either a ready Fakes or a `fakes_for(attempt_id)` factory (the real
    installer's marker directory depends on the attempt, which is only known once
    each attempt is opened)."""
    if isinstance(fakes, Fakes):
        return fakes
    return fakes(attempt_id)

def _run_and_merge(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                   attempt_id: str, station: str, role: str, task_text: str,
                   on_progress: ProgressCallback | None,
                   processor_factory: orbit.ProcessorFactory | None, *,
                   extra_payload: dict[str, object] | None = None) -> orbit.JobOutcome:
    """run_station(station, role, task_text) -> merge(role's own copy).
    Never commits/report/close -- the caller does, once it knows this
    station's own gate passed (see _commit_job_trail). `extra_payload`
    (the design call site's own plan.tools table, see
    engine.plan.tools_for_station) is merged into the run_station
    payload on top of the fixed station/role/task keys."""
    payload: dict[str, object] = {"station": station, "role": role, "task": task_text}
    if extra_payload:
        payload.update(extra_payload)
    copy = str(copy_root(config, attempt_id, role))
    stages = [
        orbit.Stage("run_station", payload),
        orbit.Stage("merge", {"copies": [{"role": role, "station": station, "path": copy}]}),
    ]
    return orbit.continue_job(config, ledger, _fakes_for(fakes, attempt_id), project, attempt_id, stages,
                              processor_factory=processor_factory, on_progress=on_progress)


def _station_outcome_to_full(outcome: orbit.JobOutcome, station: str) -> FullOutcome | None:
    """None iff `outcome` is "done" (the caller should keep going).
    Otherwise the terminal/needs_owner FullOutcome to return right
    now."""
    if outcome.status == "done":
        return None
    if outcome.pending_requests or outcome.status == "needs_owner":
        return FullOutcome("needs_owner", "owner_request", outcome.attempt_id, None)
    return FullOutcome("failed", f"{station}:{outcome.reason}", outcome.attempt_id, None)


def _close_attempt(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                   attempt_id: str, job_name: str, on_progress: ProgressCallback | None,
                   processor_factory: orbit.ProcessorFactory | None) -> orbit.JobOutcome:
    """report -> close for an attempt that made no gated commit of its
    own (the facts/planner/designer attempt -- every file it touched
    was already committed directly via _commit_job_trail, so close's
    own archive/reset has nothing left to archive or reset)."""
    return orbit.continue_job(config, ledger, _fakes_for(fakes, attempt_id), project, attempt_id,
                              orbit.release_followup_stages(job_name),
                              processor_factory=processor_factory, on_progress=on_progress)


def _builder_run_station_payload(task: Task, task_text: str, plan: Plan,
                                 config: EngineConfig, job_name: str) -> dict[str, object]:
    usable_skills, _needs_install = classify_skill_tools(plan, config)
    owns = list(task.owns)
    if plan.ui:
        # The engine's own extra allowance (see _config_for_build_task),
        # not part of the plan's own owns -- the planner never has to
        # know or ask for this path.
        owns.append(f".gongbang/{job_name}/screens/")
    return {"station": "build", "role": BUILDER_ROLE, "task": task_text,
            "owns": owns,
            "tools": tools_for_station(plan, "build", usable_skills=usable_skills)}


def _review_payload(config: EngineConfig, ledger: Ledger, project: str, plan: Plan,
                    task: str) -> dict[str, object]:
    """The review stage judges against the task itself (without the
    build station's injected skills) and gets its own station context:
    the review base skills plus the skills the planner picked for review."""
    review_context = _prompt_with_context(config, ledger, project, None, "review",
                                          task, "", _planned_skills(config, plan, "review"))
    payload: dict[str, object] = {"role": REVIEWER_ROLE, "task": task}
    if review_context.strip():
        payload["context"] = review_context.strip()
    return payload


def _run_task_attempt(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, task: Task, task_text: str, plan: Plan,
                      on_progress: ProgressCallback | None,
                      processor_factory: orbit.ProcessorFactory | None,
                      base_task_text: str | None = None) -> orbit.JobOutcome:
    """Open a FRESH attempt for this one task (see module docstring for
    why) and run its full run_station(build) -> merge -> review ->
    bench -> commit -> report -> close sequence -- exactly
    engine.orbit.default_stages()'s own seven stages, just built
    directly here so the run_station payload can carry this task's own
    `owns`, which default_stages() has no parameter for."""
    attempt_id = open_job(config, ledger, project, job_name, "full", (BUILDER_ROLE,))
    build_copy = str(copy_root(config, attempt_id, BUILDER_ROLE))
    build_config = _config_for_build_task(config, job_name, plan)
    stages = [
        orbit.Stage("run_station", _builder_run_station_payload(task, task_text, plan,
                                                                 build_config, job_name)),
        orbit.Stage("merge", {"copies": [
            {"role": BUILDER_ROLE, "station": "build", "path": build_copy},
        ]}),
        orbit.Stage("review", _review_payload(config, ledger, project, plan,
                                              base_task_text or task_text)),
        orbit.Stage("bench", {}),
        orbit.Stage("commit", {"message": f"gongbang: {job_name} - {task.id}"}),
        orbit.Stage("report", {"summary": f"gongbang: {job_name} - {task.id}"}),
        orbit.Stage("close", {}),
    ]
    return orbit.continue_job(build_config, ledger, _fakes_for(fakes, attempt_id), project, attempt_id,
                              stages, processor_factory=processor_factory, on_progress=on_progress)


def _run_tasks(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
              job_name: str, plan: Plan, tasks: list[Task],
              on_progress: ProgressCallback | None,
              processor_factory: orbit.ProcessorFactory | None,
              ) -> tuple[FullOutcome | None, str | None]:
    """Run every task in `tasks` (already in dependency order), each in
    its own fresh attempt, to completion. (None, last_attempt_id) iff
    all of them finished "done" -- otherwise (the needs_owner/failed
    FullOutcome to return right now, its own attempt_id), having
    already journaled enough progress (remaining task ids) for
    continue_full_job to pick up at the task that stopped."""
    last_attempt_id: str | None = None
    for index, task in enumerate(tasks):
        base_task_text = builder_task_text(task, plan.goal, job_name, plan.ui)
        task_text = _prompt_with_context(config, ledger, project, None, "build",
                                         base_task_text, base_task_text,
                                         _planned_skills(config, plan, "build"))
        outcome = _run_task_attempt(config, ledger, fakes, project, job_name, task,
                                    task_text, plan, on_progress, processor_factory,
                                    base_task_text)
        last_attempt_id = outcome.attempt_id
        if outcome.status != "done" or outcome.pending_requests:
            full = _station_outcome_to_full(outcome, "build")
            if outcome.status == "needs_owner" or outcome.pending_requests:
                remaining = [t.id for t in tasks[index:]]
                _record_progress(
                    ledger, project, outcome.attempt_id, job_name,
                    stage="awaiting_task_owner", remaining_task_ids=remaining,
                    current_task_id=task.id, current_task_text=task_text,
                    current_base_task_text=base_task_text, stop_reason=outcome.reason,
                )
            if full is not None:
                return full, last_attempt_id
    return None, last_attempt_id


def _run_acceptance_and_handoff(config: EngineConfig, ledger: Ledger, fakes: Fakes,
                                project: str, job_name: str, plan: Plan,
                                max_fix_rounds: int, on_progress: ProgressCallback | None,
                                processor_factory: orbit.ProcessorFactory | None,
                                last_attempt_id: str | None) -> FullOutcome:
    from engine import acceptance as acceptance_mod  # worker C's module; lazy import
    from engine import handoff as handoff_mod  # worker C's module; lazy import

    acc = acceptance_mod.run_acceptance(config, ledger, project, last_attempt_id, plan)
    rounds = 0
    while not acc.passed and rounds < max_fix_rounds:
        failing = tuple(s.id for s in acc.scenes if not s.passed)
        all_owns = tuple(sorted({entry for t in plan.tasks for entry in t.owns}))
        fix_task = Task(id=f"fix-{rounds + 1}", title="수락 실패 수정", detail="",
                        owns=all_owns, depends_on=())
        base_task_text = fix_task_text(fix_task, plan.goal, failing, job_name, plan.ui)
        task_text = _prompt_with_context(config, ledger, project, None, "build",
                                         base_task_text, base_task_text,
                                         _planned_skills(config, plan, "build"))
        outcome = _run_task_attempt(config, ledger, fakes, project, job_name, fix_task,
                                    task_text, plan, on_progress, processor_factory,
                                    base_task_text)
        last_attempt_id = outcome.attempt_id
        if outcome.status != "done" or outcome.pending_requests:
            full = _station_outcome_to_full(outcome, "build")
            if outcome.status == "needs_owner" or outcome.pending_requests:
                _record_progress(
                    ledger, project, outcome.attempt_id, job_name,
                    stage="awaiting_task_owner", remaining_task_ids=[],
                    current_task_id=fix_task.id, current_task_text=task_text,
                    current_base_task_text=base_task_text, stop_reason=outcome.reason,
                    fix_round=rounds, fixing=True, max_fix_rounds=max_fix_rounds,
                )
            if full is not None:
                return full
        rounds += 1
        acc = acceptance_mod.run_acceptance(config, ledger, project, last_attempt_id, plan)

    if not acc.passed:
        return FullOutcome("failed", "max_fix_rounds_exceeded", last_attempt_id, None)

    report_path = handoff_mod.write_handoff(config, ledger, project, last_attempt_id,
                                            job_name, plan, acc)
    _commit_job_trail(config, ledger, project, last_attempt_id, job_name,
                      f"gongbang: {job_name} - handoff")
    return FullOutcome("done", None, last_attempt_id, str(report_path))


def _finish_from_tasks(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                       job_name: str, plan: Plan, tasks: list[Task], max_fix_rounds: int,
                       on_progress: ProgressCallback | None,
                       processor_factory: orbit.ProcessorFactory | None,
                       last_attempt_id: str | None = None) -> FullOutcome:
    stopped, last_attempt_id = _run_tasks(config, ledger, fakes, project, job_name, plan,
                                         tasks, on_progress, processor_factory)
    if stopped is not None:
        return stopped
    return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name, plan,
                                       max_fix_rounds, on_progress, processor_factory,
                                       last_attempt_id)


def run_full_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                 job_name: str, request: str, *, max_fix_rounds: int = 2,
                 on_progress: ProgressCallback | None = None,
                 processor_factory: orbit.ProcessorFactory | None = None) -> FullOutcome:
    repo = config.projects[project].repo
    attempt_id = open_job(config, ledger, project, job_name, "full",
                          (FACTS_ROLE, PROBER_ROLE, PLANNER_ROLE, DESIGNER_ROLE))

    facts_task_text = _prompt_with_context(config, ledger, project, attempt_id, "facts",
                                           request, facts_prompt(request, job_name))
    outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "facts",
                             FACTS_ROLE, facts_task_text, on_progress,
                             processor_factory)
    full = _station_outcome_to_full(outcome, "facts")
    if full is not None:
        return full
    _commit_job_trail(config, ledger, project, attempt_id, job_name, f"gongbang: {job_name} - facts")
    problems = check_facts(repo, job_name)
    if problems:
        return FullOutcome("failed", "facts:" + "; ".join(problems), attempt_id, None)

    facts_md = _facts_md_text(repo, job_name)
    targets = check_probe_targets(repo, job_name)
    probe_text = ""
    if not targets:
        ledger.append(Event(
            project=project, kind="probe.skipped", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id, payload={"job": job_name},
        ))
    else:
        probe_task_text = _prompt_with_context(config, ledger, project, attempt_id, "probe",
                                               request, prober_prompt(request, job_name, targets))
        outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "probe",
                                 PROBER_ROLE, probe_task_text, on_progress,
                                 processor_factory)
        full = _station_outcome_to_full(outcome, "probe")
        if full is not None:
            return full
        _commit_job_trail(config, ledger, project, attempt_id, job_name,
                          f"gongbang: {job_name} - probe")
        probe_problems = check_probe(repo, job_name, targets)
        if probe_problems:
            failed_names = ",".join(p.split(":", 1)[0] for p in probe_problems)
            return FullOutcome("needs_owner", f"probe_failed:{failed_names}", attempt_id, None)
        probe_text = _probe_logs_text(repo, job_name, targets)

    plan_task_text = _prompt_with_context(config, ledger, project, attempt_id, "plan",
                                          request, planner_prompt(request, job_name, facts_md,
                                                                   skill_catalog_text(config),
                                                                   probe_text))
    outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "plan",
                             PLANNER_ROLE, plan_task_text,
                             on_progress, processor_factory)
    full = _station_outcome_to_full(outcome, "plan")
    if full is not None:
        return full
    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None)
    problems = validate_plan(plan, repo, _build_prefixes(config))
    problems += validate_tools(plan, config)
    if problems:
        return FullOutcome("failed", "plan:" + "; ".join(problems), attempt_id, None)

    # Review the planner's own merged-but-uncommitted change (plan.json +
    # tests/scenes/) BEFORE it is committed as the orchestrator's own plan
    # trail -- otherwise it becomes every later reviewer's base_head
    # (nothing to diff against) and is never judged by anyone (Codex
    # finding). One plain review Stage on this same attempt; the diff it
    # sees is exactly base_head..current (this merge's own files, nothing
    # else has touched the tree yet).
    plan_review_outcome = orbit.continue_job(
        config, ledger, _fakes_for(fakes, attempt_id), project, attempt_id,
        [orbit.Stage("review", _review_payload(config, ledger, project, plan, request))],
        processor_factory=processor_factory, on_progress=on_progress,
    )
    if plan_review_outcome.status != "done":
        return FullOutcome("needs_owner", "plan_review_failed", attempt_id, None)

    _commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - plan", station="plan", role=PLANNER_ROLE)

    if plan.ui:
        usable_skills, _needs_install = classify_skill_tools(plan, config)
        design_task_text = _prompt_with_context(config, ledger, project, attempt_id, "design",
                                                request, designer_prompt(request, job_name, plan.goal),
                                                _planned_skills(config, plan, "design"))
        outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "design",
                                 DESIGNER_ROLE, design_task_text,
                                 on_progress, processor_factory,
                                 extra_payload={"tools": tools_for_station(
                                     plan, "design", usable_skills=usable_skills)})
        full = _station_outcome_to_full(outcome, "design")
        if full is not None:
            return full
        _commit_job_trail(config, ledger, project, attempt_id, job_name,
                          f"gongbang: {job_name} - design")
        if not check_design_approved(ledger, project, job_name):
            _record_progress(ledger, project, attempt_id, job_name,
                             stage="awaiting_design_approval", max_fix_rounds=max_fix_rounds)
            return FullOutcome("needs_owner", "design_approval", attempt_id, None)

    close_outcome = _close_attempt(config, ledger, fakes, project, attempt_id, job_name,
                                   on_progress, processor_factory)
    if close_outcome.status != "done":
        return FullOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None)

    return _finish_from_tasks(config, ledger, fakes, project, job_name, plan,
                              ordered_tasks(plan), max_fix_rounds, on_progress,
                              processor_factory)


def continue_full_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, *, on_progress: ProgressCallback | None = None,
                      processor_factory: orbit.ProcessorFactory | None = None) -> FullOutcome:
    repo = config.projects[project].repo
    attempts = ledger.attempts_for_job_name(project, job_name)
    if not attempts:
        raise FullOrbitError(f"no attempt for job {job_name!r}")
    attempt_id = attempts[-1].attempt_id

    progress = _load_progress(ledger, project, attempt_id, job_name)
    if progress is None:
        raise FullOrbitError(f"no pending stop recorded for job {job_name!r}")

    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None)

    max_fix_rounds = int(progress.get("max_fix_rounds", 2))
    stage = progress.get("stage")

    if stage == "awaiting_design_approval":
        if not check_design_approved(ledger, project, job_name):
            return FullOutcome("needs_owner", "design_approval", attempt_id, None)
        close_outcome = _close_attempt(config, ledger, fakes, project, attempt_id, job_name,
                                       on_progress, processor_factory)
        if close_outcome.status != "done":
            return FullOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None)
        return _finish_from_tasks(config, ledger, fakes, project, job_name, plan,
                                  ordered_tasks(plan), max_fix_rounds, on_progress,
                                  processor_factory)

    if stage == "awaiting_task_owner":
        # The reviewer is always shown the plain base task text (no build-
        # station skill appendix) -- current_base_task_text falls back to
        # current_task_text only for a progress event recorded before this
        # field existed.
        base_task_text = str(progress.get("current_base_task_text",
                                          progress.get("current_task_text", "")))
        current_task_id = str(progress.get("current_task_id", ""))
        fixing = bool(progress.get("fixing"))
        if fixing:
            all_owns = tuple(sorted({entry for t in plan.tasks for entry in t.owns}))
            task_obj: Task | None = Task(id=current_task_id, title="수락 실패 수정", detail="",
                                         owns=all_owns, depends_on=())
        else:
            by_id = {t.id: t for t in plan.tasks}
            task_obj = by_id.get(current_task_id)
            if task_obj is None:
                raise FullOrbitError(f"unknown task id for resume: {current_task_id!r}")

        run_config = config
        if progress.get("stop_reason") == "worker_stuck":
            # run_station itself stopped (engine.orbit's own continuation
            # limit, see CONTINUABLE_REASONS/MAX_STATION_CONTINUES) BEFORE
            # the merge stage after it ever ran -- unlike a station's own
            # pending owner-request stop (the case stages_after_install's
            # own docstring describes, where merge already landed the
            # worker's edits), there is nothing merged yet to resume
            # reviewing. Rerun this task's build from scratch -- continue_n
            # reset, since this is a fresh run_station request, not a
            # continuation of the exhausted one -- on the SAME attempt/copy,
            # then merge, then review onward.
            task_text = str(progress.get("current_task_text", base_task_text))
            run_config = _config_for_build_task(config, job_name, plan)
            build_copy = str(copy_root(run_config, attempt_id, BUILDER_ROLE))
            resume_stages = [
                orbit.Stage("run_station", _builder_run_station_payload(
                    task_obj, task_text, plan, run_config, job_name)),
                orbit.Stage("merge", {"copies": [
                    {"role": BUILDER_ROLE, "station": "build", "path": build_copy},
                ]}),
                *orbit.stages_after_install(config, attempt_id, BUILDER_ROLE,
                                            REVIEWER_ROLE, job_name, base_task_text),
            ]
        else:
            resume_stages = orbit.stages_after_install(config, attempt_id, BUILDER_ROLE,
                                                        REVIEWER_ROLE, job_name, base_task_text)
        outcome = orbit.continue_job(run_config, ledger, _fakes_for(fakes, attempt_id), project,
                                     attempt_id, resume_stages, processor_factory=processor_factory,
                                     on_progress=on_progress)
        if outcome.status != "done" or outcome.pending_requests:
            full = _station_outcome_to_full(outcome, "build")
            if full is not None:
                return full
        if progress.get("fixing"):
            return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name,
                                               plan, max_fix_rounds, on_progress,
                                               processor_factory, attempt_id)
        remaining_ids = [str(i) for i in progress.get("remaining_task_ids", [])]
        by_id = {t.id: t for t in plan.tasks}
        remaining_tasks = [by_id[i] for i in remaining_ids[1:] if i in by_id]
        return _finish_from_tasks(config, ledger, fakes, project, job_name, plan,
                                  remaining_tasks, max_fix_rounds, on_progress,
                                  processor_factory, attempt_id)

    raise FullOrbitError(f"unknown progress stage: {stage!r}")


__all__ = [
    "FullOutcome", "FullOrbitError", "run_full_job", "continue_full_job",
    "FACTS_ROLE", "PROBER_ROLE", "PLANNER_ROLE", "DESIGNER_ROLE", "BUILDER_ROLE",
    "REVIEWER_ROLE",
]
