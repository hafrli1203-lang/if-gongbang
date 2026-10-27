"""Shared attempt-running primitives for the full orbit: opening an
attempt with its own intent recorded first, running one station's
run_station -> merge, translating a plain orbit.JobOutcome into a
FullOutcome, closing an ungated attempt, the orchestrator's own direct
commits under .gongbang/<job>/, this job's one `job.position` record,
and the station-context/skill helpers every station (early, build task,
or a resumed one) builds its prompt through.

engine.application.pipeline_planning/_tasks/_resume all call into this module (never
the other way) -- see engine.application.pipeline's own module docstring for how
the whole job fits together."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from engine.infrastructure import context as context_mod
from engine.domain import faults
from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.application.pipeline_base import (
    BUILDER_ROLE, FullOutcome, ProgressCallback, Run, fakes_for,
)
from engine.infrastructure.gitutil import git_argv
from engine.domain.records import Event
from engine.domain.plan import Plan, Task, classify_skill_tools, tools_for_station
from engine.application.job_state import (
    STAGE_EARLY_STATION, STAGE_PLAN_REVIEW, STAGE_PROBE_RETRY, STAGE_TASK, STAGE_TASK_OWNER,
    JobPosition, record_position, task_stop_position,
)
from engine.application.processor import Fakes
from engine.application.steps.open_job import copy_root, open_job

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


def record_event(ledger: Ledger, project: str, job_name: str, stage: str, attempt_id: str | None,
            *, task_id: str | None = None, remaining_task_ids: tuple[str, ...] | list[str] = (),
            fixing: bool = False, pending_requests: tuple[dict, ...] | list[dict] = (),
            stop_reason: str | None = None, **extra: object) -> None:
    """Records this job's one position for the transition at hand."""
    record_position(ledger, project, JobPosition(
        job=job_name, stage=stage, attempt_id=attempt_id, task_id=task_id,
        remaining_task_ids=tuple(str(i) for i in remaining_task_ids), fixing=fixing,
        pending_requests=tuple(pending_requests), stop_reason=stop_reason, extra=dict(extra),
    ))



def record_early_station_stop(ledger: Ledger, project: str, attempt_id: str, job_name: str,
                               station: str, reason: str | None,
                               pending_requests: tuple[dict, ...] | list[dict], *,
                               request: str, max_fix_rounds: int, probe_text: str = "") -> None:
    """The one shape of a facts/probe/plan/design station stop. `station`
    names which station the STAGE_EARLY_STATION resume reruns;
    `probe_text` is only non-empty for a "plan" stop (probe already
    succeeded and committed, so a resume must not rerun it)."""
    record_event(ledger, project, job_name, STAGE_EARLY_STATION, attempt_id,
            pending_requests=pending_requests, stop_reason=reason, station=station,
            request=request, max_fix_rounds=max_fix_rounds, probe_text=probe_text)



def task_position(job_name: str, attempt_id: str | None, task_id: str, task_text: str,
                   base_task_text: str, *, fixing: bool, remaining_task_ids: tuple[str, ...],
                   max_fix_rounds: int) -> JobPosition:
    return JobPosition(
        job=job_name, stage=STAGE_TASK, attempt_id=attempt_id, task_id=task_id,
        remaining_task_ids=tuple(remaining_task_ids), fixing=fixing,
        extra={"task_text": task_text, "base_task_text": base_task_text,
               "max_fix_rounds": max_fix_rounds, "next_action": "run_station"},
    )



def record_task_stop(ledger: Ledger, project: str, task_pos: JobPosition,
                      outcome: orbit.JobOutcome, **extra: object) -> None:
    """A build task's attempt stopped for the owner (an owner request, or
    engine.application.task_pipeline's own worker_stuck continuation limit): the task
    position, moved to STAGE_TASK_OWNER with the station's own raw
    proposals, which the resume gate checks are fulfilled first."""
    record_position(ledger, project, task_stop_position(
        task_pos, outcome.attempt_id, outcome.reason, list(outcome.pending_requests), **extra))



def stops_for_owner(outcome: orbit.JobOutcome) -> bool:
    return outcome.status == "needs_owner" or bool(outcome.pending_requests)



ORCHESTRATOR_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "processor", "GIT_AUTHOR_EMAIL": "processor@local",
    "GIT_COMMITTER_NAME": "processor", "GIT_COMMITTER_EMAIL": "processor@local",
}



def latest_merge_paths(ledger: Ledger, project: str, attempt_id: str) -> tuple[str, ...]:
    """The exact relative paths engine.application.steps.merge's own "record" step
    applied in the MOST RECENT merge.recorded event on this attempt
    (that event's own "paths" payload, engine.application.steps.merge.record_event) --
    not every path under some owned prefix that happens to exist on
    disk. facts/probe/plan/design all share one attempt (see this
    module's own docstring), each running its own run_station -> merge
    in turn, so "most recent" at the moment commit_job_trail is called
    right after a merge always means THAT station's own merge, never an
    earlier sibling's."""
    latest: tuple[str, ...] = ()
    for _seq, event in ledger.events(project, "merge.recorded"):
        if event.attempt_id == attempt_id:
            latest = tuple(str(p) for p in event.payload.get("paths", ()))
    return latest



def commit_job_trail(config: EngineConfig, ledger: Ledger, project: str, attempt_id: str | None,
                      job_name: str, message: str,
                      station: str | None = None, role: str | None = None) -> None:
    """Commit whatever the orchestrator itself (never a worker) just
    wrote under .gongbang/<job>/ -- a merge's own apply_allowed_paths
    step already landed facts.md/plan.json/design.md/report.md there
    as plain, uncommitted files in the real repo, and leaving them
    uncommitted would both (a) make the NEXT station's own base_head
    stale (see this module's docstring) and (b) let engine.application.task_pipeline's own
    "close" stage (git checkout/clean to HEAD) wipe them right back
    out. This is a plain, UNGATED engine-side commit -- unlike
    engine.application.steps.commit, it never checks a review/bench fingerprint,
    because nothing a worker wrote is in this diff; only files the
    processor itself just wrote (plus, when `station`/`role` is given,
    exactly the paths THIS station's own merge just applied outside
    .gongbang/ -- see latest_merge_paths; never every path that
    happens to exist under the role's whole owned prefix, which could
    sweep in some unrelated pre-existing uncommitted change under that
    same prefix, e.g. from the owner's own working tree or a different
    job's leftover file). A no-op (nothing actually changed under
    .gongbang/<job>/) commits nothing.

    The commit itself is scoped to exactly `paths` via a trailing `--
    <paths>` pathspec, so files the owner staged are never absorbed: a bare
    `git commit` with no pathspec commits EVERY currently staged
    change, not just what the `git add` right above this staged --
    anything the owner (or anything else) had already `git add`ed
    under some OTHER path before this ran would be swept into this
    engine-authored, ungated commit right alongside it. Passing the
    same `paths` list to `commit` that `add` just used means only
    those paths' content is committed; any other already-staged path
    is left exactly as staged as it was, untouched, for whatever
    later, real commit (engine.application.steps.commit's gated one, or the owner's
    own) is actually supposed to include it."""
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
            merged = latest_merge_paths(ledger, project, attempt_id)
            paths += [
                p for p in merged
                if any(p.startswith(prefix) for prefix in extra_prefixes) and (repo / p).exists()
            ]
    commit_paths(repo, paths, message)



def commit_paths(repo: Path, paths: list[str], message: str) -> None:
    """An ungated orchestrator commit of exactly `paths` (a no-op when
    none of them changed)."""
    env = {**os.environ, **ORCHESTRATOR_GIT_IDENTITY}
    # Engine records under .gongbang/ are added with -f so an app's
    # .gitignore (e.g. "*.log") cannot silently drop them; nothing else
    # is ever force-added.
    records = [p for p in paths if p == ".gongbang" or p.startswith(".gongbang/")]
    others = [p for p in paths if p not in records]
    if others:
        subprocess.run(git_argv(repo, "add", "--", *others), check=True, capture_output=True, env=env)
    if records:
        subprocess.run(git_argv(repo, "add", "-f", "--", *records), check=True, capture_output=True, env=env)
    status = subprocess.run(
        git_argv(repo, "status", "--porcelain", "--", *paths),
        capture_output=True, text=True, env=env,
    ).stdout
    if not status.strip():
        return
    subprocess.run(git_argv(repo, "commit", "-q", "-m", message, "--", *paths),
                   check=True, capture_output=True, env=env)



def build_prefixes(config: EngineConfig) -> tuple[str, ...]:
    return tuple(config.station_owners.get("build", {}).get(BUILDER_ROLE, ()))



def record_context(ledger: Ledger, project: str, attempt_id: str | None, station: str,
                    sc: "context_mod.StationContext") -> None:
    """Appends the context.injected ledger event for this station's
    prompt (and, if the library lookup failed, context.failed right
    after it) -- see engine.infrastructure.context.build's own docstring for what each
    field means. `attempt_id` may be None (a build/fix task's attempt
    is only opened AFTER its prompt text is built -- see
    engine.application.pipeline's own module docstring on fresh-attempt-per-task)."""
    faults.flow_boundary("full_orbit.context_injected")
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
        faults.flow_boundary("full_orbit.context_failed")
        ledger.append(Event(
            project=project, kind="context.failed", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id, payload={"station": station},
        ))



def planned_skills(config: EngineConfig, plan: Plan, station: str) -> tuple[str, ...]:
    """The skills this job's planner picked for `station` that the engine
    can actually load (needs_install ones are never injected)."""
    usable, _needs_install = classify_skill_tools(plan, config)
    return tuple(tools_for_station(plan, station, usable_skills=usable)["skill"])



def build_station_context(config: EngineConfig, ledger: Ledger, project: str,
                           attempt_id: str | None, station: str, query: str,
                           planned_skills: tuple[str, ...] = ()) -> "context_mod.StationContext":
    """engine.infrastructure.context.build for this station's prompt, recorded on the
    ledger (context.injected/context.failed) exactly once -- the
    shared first half of prompt_with_context, broken out so a caller
    that also needs to check `.unmet_required()` (ContextConfig.
    required_skills/require_library -- see that module's own
    docstring) BEFORE ever submitting run_station can do so without
    building/recording the context a second time."""
    sc = context_mod.build(config, project, station, query, planned_skills,
                           attempt_id=attempt_id)
    record_context(ledger, project, attempt_id, station, sc)
    return sc



def prompt_with_context(config: EngineConfig, ledger: Ledger, project: str,
                         attempt_id: str | None, station: str, query: str,
                         prompt: str, planned_skills: tuple[str, ...] = ()) -> str:
    """Builds this station's injected-context appendix (engine.infrastructure.context.
    build), records it on the ledger, and returns `prompt` with that
    appendix appended (unchanged if there is nothing to inject). Used
    by every call site that never needs to stop on `.unmet_required()`
    itself (review's own context, which never gates a needs_owner stop
    -- a review failure is an ordinary rejection, not an owner-request)."""
    sc = build_station_context(config, ledger, project, attempt_id, station, query,
                                planned_skills)
    if not sc.text:
        return prompt
    return f"{prompt}\n\n{sc.text}"



def unmet_context_reason(sc: "context_mod.StationContext") -> str:
    """A short, stable reason code for a `.unmet_required()` stop --
    read back by nothing (this job always needs the SAME config fix
    either way), but kept informative for `gb2 status`/`gb2 board`."""
    if sc.required_missing:
        return f"context_required_missing:{','.join(sc.required_missing)}"[:200]
    return "context_required_library_failed"



@dataclass(frozen=True)
class ContextGate:
    """A station's required context, checked before its worker runs.
    `context` is the station's own (recorded on the ledger); `unmet` is
    the stop reason when the station, or the review that will judge its
    output, lacks a required skill or company-library lookup."""
    context: "context_mod.StationContext"
    unmet: str | None

    def prompt(self, text: str) -> str:
        """`text` with this station's injected context appended."""
        return f"{text}\n\n{self.context.text}" if self.context.text else text


def station_context_gate(config: EngineConfig, ledger: Ledger, project: str,
                         attempt_id: str | None, station: str, query: str,
                         planned_skills: tuple[str, ...] = (), *,
                         review_query: str | None = None,
                         review_skills: tuple[str, ...] = ()) -> ContextGate:
    """The one required-context gate every station runs before
    dispatching a worker, in a full job and in small mode alike. Builds
    and records `station`'s context; with `review_query`, also checks the
    review station that will judge this station's output (its context is
    recorded later, when the review stage itself is built), so a missing
    review requirement stops the job before any worker runs."""
    sc = build_station_context(config, ledger, project, attempt_id, station, query,
                               planned_skills)
    if sc.unmet_required():
        return ContextGate(sc, unmet_context_reason(sc))
    if review_query is not None:
        review_sc = context_mod.build(config, project, "review", review_query, review_skills,
                                      attempt_id=attempt_id)
        if review_sc.unmet_required():
            return ContextGate(sc, unmet_context_reason(review_sc))
    return ContextGate(sc, None)


def stop_early_station_for_unmet_context(ledger: Ledger, project: str, attempt_id: str,
                                          job_name: str, station: str, request: str,
                                          max_fix_rounds: int, reason: str,
                                          probe_text: str = "") -> FullOutcome:
    """A facts/probe/plan/design station's own required-context gap --
    stopped needs_owner on the SAME shared early attempt, in the exact
    STAGE_EARLY_STATION shape record_early_station_stop already uses
    for a run_station owner-request, but WITHOUT ever calling
    run_station at all: there is nothing a worker could do about a
    missing skill/company-library lookup the engine itself could not
    load. No pending requests (nothing to approve) means the
    STAGE_EARLY_STATION resume reruns this station fresh once `gb2
    continue` is called -- the owner fixing the
    underlying config/skill root between stops is what actually
    changes anything, not an approval."""
    record_early_station_stop(ledger, project, attempt_id, job_name, station, reason, (),
                               request=request, max_fix_rounds=max_fix_rounds,
                               probe_text=probe_text)
    return FullOutcome("needs_owner", reason, attempt_id, None)



def stop_build_task_for_unmet_context(config: EngineConfig, ledger: Ledger, project: str,
                                       job_name: str, task: Task, task_text: str,
                                       base_task_text: str, reason: str,
                                       *, fixing: bool, remaining_task_ids: tuple[str, ...],
                                       max_fix_rounds: int) -> FullOutcome:
    """A build task's own required-context gap -- opens this task's own
    fresh attempt (the same roles run_task_attempt would, recording the
    same task position) but never
    submits run_station at all, going straight to a STAGE_TASK_OWNER
    needs_owner stop -- the same shape a run_station owner-request stop
    already uses, so `gb2 continue` reruns this task's build fresh
    (never a second, wasted worker call for a gap the engine itself,
    not the worker, must resolve)."""
    task_pos = task_position(job_name, None, task.id, task_text, base_task_text,
                              fixing=fixing, remaining_task_ids=remaining_task_ids,
                              max_fix_rounds=max_fix_rounds)
    # context_gap: the engine, not the worker, stopped this task -- the
    # resume rebuilds and rechecks the context instead of treating it as
    # an owner-request fulfillment or a plain rerun.
    stop = task_stop_position(task_pos, None, reason, (), context_gap=True)
    attempt_id = open_with_intent(config, ledger, project, stop, (BUILDER_ROLE,))
    record_position(ledger, project, replace(stop, attempt_id=attempt_id))
    return FullOutcome("needs_owner", reason, attempt_id, None)



def open_with_intent(config: EngineConfig, ledger: Ledger, project: str,
                      intent: JobPosition, roles: tuple[str, ...]) -> str:
    """Opens an attempt with the position it is opened for recorded
    first: `intent` without an attempt id, carrying the roles to open
    with (engine.application.job_state.opening_roles). A crash between that record
    and the caller's own position on the attempt resumes by adopting the
    attempt open_job made, or opening it (reconcile_opening)."""
    record_position(ledger, project, replace(
        intent, attempt_id=None, extra={**intent.extra, "roles": list(roles)}))
    attempt_id = open_job(config, ledger, project, intent.job, "full", roles)
    faults.flow_boundary("full_orbit.attempt_opened")
    return attempt_id



def attempt_opened_after(ledger: Ledger, project: str, job_name: str,
                          after_seq: int) -> str | None:
    """The project's current attempt, if it is an active attempt of
    `job_name` whose job.opened is newer than `after_seq`."""
    row = ledger.project(project)
    if row.state != "OPEN" or row.current_attempt_id is None:
        return None
    for seq, event in ledger.events(project, "job.opened"):
        if (event.payload.get("attempt_id") == row.current_attempt_id and seq > after_seq
                and event.payload.get("job_name") == job_name):
            return row.current_attempt_id
    return None



def reconcile_opening(run: "Run", pos: JobPosition, roles: tuple[str, ...]) -> JobPosition:
    """`pos` is an opening intent (no attempt yet): adopt the attempt
    open_job made after it was recorded, or open it now, and record the
    same position on that attempt."""
    marker_seq = max((seq for seq, event in run.ledger.events(run.project, "job.position")
                      if event.payload.get("job") == run.job), default=0)
    attempt_id = attempt_opened_after(run.ledger, run.project, run.job, marker_seq)
    if attempt_id is None:
        attempt_id = open_job(run.config, run.ledger, run.project, run.job, "full", roles)
    opened = replace(pos, attempt_id=attempt_id,
                     extra={k: v for k, v in pos.extra.items() if k != "roles"})
    record_position(run.ledger, run.project, opened)
    return opened



def finish_inflight_close(run: "Run") -> FullOutcome | None:
    """A close of this job's current attempt that started and never
    finished (the project is still CLOSING -- e.g. a crash between
    attempt.finished and the CLOSED transition) is run to its end before
    anything advances, the same request, never a new one. None when
    there is nothing in flight or it finished; the failed outcome
    otherwise."""
    row = run.ledger.project(run.project)
    attempt_id = row.current_attempt_id
    if row.state != "CLOSING" or attempt_id is None:
        return None
    if attempt_id not in {a.attempt_id for a in run.ledger.attempts_for_job_name(
            run.project, run.job)}:
        return None
    pending = [event.payload.get("request_id")
               for _seq, event in run.ledger.events(run.project, "request.submitted")
               if event.attempt_id == attempt_id
               and event.payload.get("kind") in ("close", "abandon")]
    pending = [rid for rid in pending if isinstance(rid, str)
               and run.ledger.request(rid).status in ("queued", "running")]
    if not pending:
        return FullOutcome("failed", "close:closing_without_request", attempt_id, None)
    factory = run.processor_factory or orbit.default_processor_factory
    processor = factory(run.config, run.ledger, run.fakes_for(attempt_id))
    status = processor.run_request(run.project, pending[-1])
    if status != "done" or run.ledger.project(run.project).state != "CLOSED":
        return FullOutcome("failed", f"close:{status}", attempt_id, None)
    return None



def attempt_has_requests(ledger: Ledger, project: str, attempt_id: str) -> bool:
    return any(event.attempt_id == attempt_id
               for _seq, event in ledger.events(project, "request.submitted"))



def run_and_merge(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                   attempt_id: str, station: str, role: str, task_text: str,
                   on_progress: ProgressCallback | None,
                   processor_factory: orbit.ProcessorFactory | None, *,
                   extra_payload: dict[str, object] | None = None) -> orbit.JobOutcome:
    """run_station(station, role, task_text) -> merge(role's own copy).
    Never commits/report/close -- the caller does, once it knows this
    station's own gate passed (see commit_job_trail). `extra_payload`
    (the design call site's own plan.tools table, see
    engine.domain.plan.tools_for_station -- or an early station's own
    explicit "owns", see quick_pipeline's combined facts+plan call)
    is merged into the run_station payload on top of the fixed
    station/role/task keys; an "owns" entry in it is ALSO carried onto
    the merge stage's own copies entry below (not just the run_station
    payload) -- engine.application.steps.merge._owner_prefixes/
    _deny_if_other_station_record both read the merge request's OWN
    "owns", never the run_station payload's, so an owns that only
    reached run_station would narrow the live worker's write access
    but never narrow (or, for a deliberate cross-station grant, declare)
    what merge itself actually re-checks and lands. `task_text`
    already differs on a STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW resume
    (the caller appends the previous attempt's own log/plan.json as
    context -- see _run_probe/_run_plan_and_review), so run_station's
    own deterministic request_id is never the problem; the merge
    stage's payload, on its own, names only role/station/path (fixed
    for this attempt), so a SECOND merge of the same role here would
    otherwise collide with the first merge's own already-"done" request
    and never actually reapply anything (engine.application.processor.submit's
    idempotency) -- `merge_retry_n` (how many merges this attempt has
    already recorded) makes each retry's merge payload genuinely new."""
    payload: dict[str, object] = {"station": station, "role": role, "task": task_text}
    if extra_payload:
        payload.update(extra_payload)
    copy = str(copy_root(config, attempt_id, role))
    merge_retry_n = sum(1 for _seq, event in ledger.events(project, "merge.recorded")
                        if event.attempt_id == attempt_id)
    copy_entry: dict[str, object] = {"role": role, "station": station, "path": copy}
    if extra_payload and "owns" in extra_payload:
        copy_entry["owns"] = extra_payload["owns"]
    merge_payload: dict[str, object] = {
        "copies": [copy_entry],
    }
    if merge_retry_n:
        merge_payload["merge_retry_n"] = merge_retry_n
    stages = [
        orbit.Stage("run_station", payload),
        orbit.Stage("merge", merge_payload),
    ]
    return orbit.continue_job(config, ledger, fakes_for(fakes, attempt_id), project, attempt_id, stages,
                              processor_factory=processor_factory, on_progress=on_progress)



def station_outcome_to_full(outcome: orbit.JobOutcome, station: str) -> FullOutcome | None:
    """None iff `outcome` is "done" (the caller should keep going).
    Otherwise the terminal/needs_owner FullOutcome to return right
    now."""
    if outcome.status == "done":
        return None
    if outcome.pending_requests or outcome.status == "needs_owner":
        return FullOutcome("needs_owner", "owner_request", outcome.attempt_id, None)
    return FullOutcome("failed", f"{station}:{outcome.reason}", outcome.attempt_id, None)



def close_attempt(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                   attempt_id: str, job_name: str, on_progress: ProgressCallback | None,
                   processor_factory: orbit.ProcessorFactory | None) -> orbit.JobOutcome:
    """report -> close for an attempt that made no gated commit of its
    own (the facts/planner/designer attempt -- every file it touched
    was already committed directly via commit_job_trail, so close's
    own archive/reset has nothing left to archive or reset)."""
    return orbit.continue_job(config, ledger, fakes_for(fakes, attempt_id), project, attempt_id,
                              orbit.release_followup_stages(job_name),
                              processor_factory=processor_factory, on_progress=on_progress)


__all__ = [
    "record_event", "record_early_station_stop", "task_position", "record_task_stop",
    "stops_for_owner", "latest_merge_paths", "commit_job_trail", "commit_paths",
    "build_prefixes", "record_context", "planned_skills", "build_station_context",
    "ContextGate", "station_context_gate",
    "prompt_with_context", "unmet_context_reason", "stop_early_station_for_unmet_context",
    "stop_build_task_for_unmet_context", "open_with_intent", "attempt_opened_after",
    "reconcile_opening", "finish_inflight_close", "attempt_has_requests", "run_and_merge",
    "station_outcome_to_full", "close_attempt",
]
