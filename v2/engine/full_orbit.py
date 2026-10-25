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
FullOutcome(status="needs_owner", ...). Every stage/task transition and
every stop is recorded as one `job.position` event (engine.position);
`continue_full_job` is engine.position.resume_job, which reads the
newest position and dispatches on its stage through RESUME_TABLE below.
Nothing needed to resume lives only in this process's memory.

`processor_factory`, on every entry point, is the same optional seam
engine.orbit.run_job/continue_job already expose (a callable building
the engine.processor.Processor each stage list runs against) -- plain
production callers (tools/gb2) never pass it (defaults to a real
Processor with register_defaults()); it exists so a test can register
a deterministic run_station/review stub instead of spawning a real
worker/reviewer process.

Every per-task attempt runs with the project's own `release` removed
(engine.position.without_release): the whole job makes exactly one
publish request, at the very end (_maybe_release_or_done)."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from engine import context as context_mod
from engine import faults, orbit, trusted
from engine.config import EngineConfig
from engine.gates import (
    ProbeTargetsError, check_design_approved, check_facts_trusted, check_probe_targets,
    facts_path,
)
from engine.gitutil import git_argv
from engine.ledger import Event, Ledger
from engine.position import (
    STAGE_DESIGN, STAGE_DESIGN_APPROVAL, STAGE_EARLY_CLOSE, STAGE_EARLY_STATION, STAGE_FACTS,
    STAGE_PLAN,
    STAGE_PLAN_REVIEW, STAGE_POST_TASKS, STAGE_PROBE, STAGE_PROBE_RETRY,
    STAGE_RELEASE_APPROVAL, STAGE_REQUIREMENTS_REVIEW, STAGE_SECURITY_FIX, STAGE_SMALL_PLAN,
    STAGE_TASK, STAGE_TASK_OWNER, JobPosition, load_position, outstanding_requests,
    stages_from_spec, stages_spec,
    pending_owner_requests, record_position, resume_job, task_stop_position, without_release,
)
from engine.plan import (
    Plan, PlanError, Task, classify_skill_tools, load_plan, ordered_tasks, plan_path,
    skill_catalog_text, tools_for_station,
    validate_plan, validate_tools,
)
from engine.processor import Fakes
from engine.station_prompts import (
    builder_task_text, designer_prompt, facts_prompt, fix_task_text, planner_prompt,
    prober_prompt,
)
from engine.steps.open_job import copy_root, dirty_paths, open_job

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
# planner's prompt budget (same spirit as engine.context's PER_ITEM_CAP).
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
        return _fakes_for(self.fakes, attempt_id)

    @property
    def repo(self) -> Path:
        return self.config.projects[self.project].repo


class FullOrbitError(ValueError):
    """Nothing to resume (continue_full_job called with no pending stop
    recorded for this job) or the job's attempt cannot be found."""


def _checked_facts(repo: Path, job_name: str) -> tuple[list[str], trusted.Trusted | None]:
    """check_facts' problems and, when there are none, the exact facts.md
    bytes that passed (engine.gates.check_facts_trusted) -- the only
    bytes a later prompt may use."""
    problems, facts = check_facts_trusted(repo, job_name)
    if problems or facts is None:
        return problems or ["facts_missing"], None
    return [], facts


def _trusted_text(t: trusted.Trusted, path: Path) -> str | None:
    """The checked bytes, if `path` still holds them; None otherwise."""
    try:
        return trusted.verify(t, path).decode("utf-8")
    except (trusted.TrustViolation, OSError, UnicodeDecodeError):
        return None


def record_release_decision(ledger: Ledger, project: str, attempt_id: str, job_name: str,
                            decision: str) -> None:
    """Journals a `full_orbit.release_decision` event -- the one durable
    record of how this job's release track ended: "not_configured" (no
    `release` in this project's config -- _maybe_release_or_done's own
    straight-to-done path), "shipped" (the pending publish request
    reached "done"), "skipped" (the owner abandoned it -- `skip-ship`,
    a full-bench refusal, a cancel-before-dispatch, or a security fix
    superseding it -- see finalize_release's own _RELEASE_ABANDON_
    REASONS), or "failed" (the publish request reached a terminal
    "failed"/"unknown" status for any OTHER reason -- a real deploy/
    fast-forward failure). `finalize_release` is the one place that
    decides which of these four applies and is the only caller that
    should ever invoke this directly; `ship`/`skip-ship`/continue_full_
    job's own STAGE_RELEASE_APPROVAL resume all go through it rather
    than calling this themselves, so the decision logic lives in
    exactly one place. Idempotent in effect (not in storage): callers
    check `_release_decision_already_recorded` first, since this
    itself always appends a fresh event. Without this, a release-
    configured job's own completion was only ever visible as engine.
    orbit's own JobOutcome from that one `continue_job` call -- nothing
    on the ledger ever said the WHOLE full-orbit job (not just this one
    attempt) had reached a terminal state, and a project with no
    release configured reached "done" with no record at all of why
    there was nothing left to approve."""
    faults.flow_boundary("full_orbit.release_decision")
    ledger.append(Event(
        project=project, kind="full_orbit.release_decision", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job_name, "decision": decision},
    ))


# Every reason a publish request's own "result" dict carries when it was
# driven to a terminal non-"done" status by a deliberate owner/engine
# ABANDON -- `cancel_pending_publish`/`cancel_running_publish_before_
# dispatch` (engine.release), called with each of these exact strings --
# never a reason recorded by an actual dispatch failure (e.g.
# advance_master/call_publisher raising StepFailed with some OTHER
# reason, like "artifact_mismatch" or a real deploy exit code). Read by
# finalize_release to tell "the owner chose not to ship this" (decision
# "skipped") apart from "the release genuinely failed" (decision
# "failed") -- both reach the SAME terminal request status
# ("failed"/"unknown"), so the status alone can never tell them apart.
_RELEASE_ABANDON_REASONS = frozenset({
    "skip_ship", "full_bench_failed", "cancelled_before_dispatch", "superseded_by_fix",
})


def _write_final_report(config: EngineConfig, ledger: Ledger, project: str, attempt_id: str,
                        job_name: str, decision: str) -> None:
    """report.md's final section (security result, release decision,
    evidence completeness -- engine.handoff.write_final_section, which
    rewrites the same section when called again), committed to the job
    trail. A job with no handoff report (a plain `gb2 run` released by
    `gb2 ship`) has no report to finish."""
    from engine import handoff as handoff_mod

    if not handoff_mod.report_path(config, project, job_name).exists():
        return
    handoff_mod.write_final_section(config, ledger, project, job_name, decision)
    _commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - final report")


def _release_decision_for_request(request_row: "object") -> str:
    """"shipped" (status "done"), "skipped" (a terminal non-"done"
    status whose own result.reason is one of _RELEASE_ABANDON_REASONS),
    or "failed" (terminal non-"done" for any other reason -- a real
    deploy/fast-forward failure, e.g. "artifact_mismatch" or a
    nonzero-exit deploy script)."""
    if request_row.status == "done":  # type: ignore[attr-defined]
        return "shipped"
    result = request_row.result  # type: ignore[attr-defined]
    reason = result.get("reason") if isinstance(result, dict) else None
    if reason in _RELEASE_ABANDON_REASONS:
        return "skipped"
    return "failed"


def _release_decision_already_recorded(ledger: Ledger, project: str, job_name: str,
                                       attempt_id: str) -> bool:
    for _seq, event in ledger.events(project, "full_orbit.release_decision"):
        if event.attempt_id == attempt_id and event.payload.get("job") == job_name:
            return True
    return False


def finalize_release(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                     attempt_id: str, job_name: str, request_id: str, report_path: str,
                     on_progress: ProgressCallback | None = None,
                     processor_factory: "orbit.ProcessorFactory | None" = None) -> FullOutcome:
    """The ONE place a release-configured job's publish request --
    shipped (`gb2 ship` drove it to "done"), abandoned (`gb2 skip-ship`,
    a full-bench refusal, a cancel-before-dispatch, or a security fix
    superseding it), or genuinely failed (any other terminal non-"done"
    status) -- is turned into this job's final FullOutcome: report ->
    close (_close_attempt, idempotent -- engine.processor.submit/
    run_once's own content-hash idempotency makes a second call on an
    already-"done" report/close request a no-op) THEN record_release_
    decision (idempotent in effect: never a second event for the same
    (project, job_name, attempt_id), see _release_decision_already_
    recorded), in that fixed order, no matter which of the three ways
    the request actually ended. `gb2 ship`/`gb2 skip-ship` (tools/gb2)
    and the STAGE_RELEASE_APPROVAL resume (a crash between the publish
    request reaching a terminal status and this function ever running)
    all call this, so the decide-close-record sequence exists once and
    a crash in that window still ends with the project closed and the
    decision recorded.

    request_id's own status not yet terminal (still "queued"/
    "running") is a plain no-op: returns FullOutcome("needs_owner",
    "release_approval", attempt_id, None) without touching the ledger
    at all -- the owner has not finished approving/rejecting yet (a
    `gb2 ship` call that typed the wrong approval line, for instance,
    leaves the request exactly like this; calling this function again
    after the owner fixes that is always safe)."""
    request_row = ledger.request(request_id)
    if request_row.status not in ("done", "failed", "unknown"):
        return FullOutcome("needs_owner", "release_approval", attempt_id, None)
    decision = _release_decision_for_request(request_row)
    # _close_attempt's own report/close stages are each individually
    # idempotent (engine.processor.submit/run_once's content-hash
    # idempotency, which now also tolerates reusing an already-
    # submitted request on a no-longer-active attempt -- see
    # engine.processor.submit's own docstring), so calling it again on a
    # job this function (or a same-process caller's own earlier
    # _close_attempt) already closed is a safe no-op. What actually
    # needs skipping is the whole project already reaching CLOSED --
    # checking the ATTEMPT's own status instead (as this used to) is
    # wrong: engine.steps.close's set_closed flips the attempt to
    # "closed"/"abandoned" as its own FIRST effect, before the project's
    # own CLOSING -> CLOSED transition (its second effect) necessarily
    # ran -- a crash in exactly that window left the attempt non-active
    # but the project stuck CLOSING forever, and the old check here
    # silently treated that as "already closed", never finishing it.
    if ledger.project(project).state != "CLOSED":
        close_outcome = _close_attempt(config, ledger, fakes, project, attempt_id, job_name,
                                       on_progress, processor_factory)
        if close_outcome.status != "done":
            return FullOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None)
    if not _release_decision_already_recorded(ledger, project, job_name, attempt_id):
        record_release_decision(ledger, project, attempt_id, job_name, decision)
    _write_final_report(config, ledger, project, attempt_id, job_name, decision)
    if decision == "failed":
        return FullOutcome("failed", "release_rejected", attempt_id, None)
    return FullOutcome("done", None, attempt_id, report_path)


def _record(ledger: Ledger, project: str, job_name: str, stage: str, attempt_id: str | None,
            *, task_id: str | None = None, remaining_task_ids: tuple[str, ...] | list[str] = (),
            fixing: bool = False, pending_requests: tuple[dict, ...] | list[dict] = (),
            stop_reason: str | None = None, **extra: object) -> None:
    """Records this job's one position for the transition at hand."""
    record_position(ledger, project, JobPosition(
        job=job_name, stage=stage, attempt_id=attempt_id, task_id=task_id,
        remaining_task_ids=tuple(str(i) for i in remaining_task_ids), fixing=fixing,
        pending_requests=tuple(pending_requests), stop_reason=stop_reason, extra=dict(extra),
    ))


def _record_early_station_stop(ledger: Ledger, project: str, attempt_id: str, job_name: str,
                               station: str, reason: str | None,
                               pending_requests: tuple[dict, ...] | list[dict], *,
                               request: str, max_fix_rounds: int, probe_text: str = "") -> None:
    """The one shape of a facts/probe/plan/design station stop. `station`
    names which station the STAGE_EARLY_STATION resume reruns;
    `probe_text` is only non-empty for a "plan" stop (probe already
    succeeded and committed, so a resume must not rerun it)."""
    _record(ledger, project, job_name, STAGE_EARLY_STATION, attempt_id,
            pending_requests=pending_requests, stop_reason=reason, station=station,
            request=request, max_fix_rounds=max_fix_rounds, probe_text=probe_text)


def _task_position(job_name: str, attempt_id: str | None, task_id: str, task_text: str,
                   base_task_text: str, *, fixing: bool, remaining_task_ids: tuple[str, ...],
                   max_fix_rounds: int) -> JobPosition:
    return JobPosition(
        job=job_name, stage=STAGE_TASK, attempt_id=attempt_id, task_id=task_id,
        remaining_task_ids=tuple(remaining_task_ids), fixing=fixing,
        extra={"task_text": task_text, "base_task_text": base_task_text,
               "max_fix_rounds": max_fix_rounds, "next_action": "run_station"},
    )


def _record_task_stop(ledger: Ledger, project: str, task_pos: JobPosition,
                      outcome: orbit.JobOutcome, **extra: object) -> None:
    """A build task's attempt stopped for the owner (an owner request, or
    engine.orbit's own worker_stuck continuation limit): the task
    position, moved to STAGE_TASK_OWNER with the station's own raw
    proposals, which the resume gate checks are fulfilled first."""
    record_position(ledger, project, task_stop_position(
        task_pos, outcome.attempt_id, outcome.reason, list(outcome.pending_requests), **extra))


def _stops_for_owner(outcome: orbit.JobOutcome) -> bool:
    return outcome.status == "needs_owner" or bool(outcome.pending_requests)


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
    later, real commit (engine.steps.commit's gated one, or the owner's
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
            merged = _latest_merge_paths(ledger, project, attempt_id)
            paths += [
                p for p in merged
                if any(p.startswith(prefix) for prefix in extra_prefixes) and (repo / p).exists()
            ]
    _commit_paths(repo, paths, message)


def _commit_paths(repo: Path, paths: list[str], message: str) -> None:
    """An ungated orchestrator commit of exactly `paths` (a no-op when
    none of them changed)."""
    env = {**os.environ, **_ORCHESTRATOR_GIT_IDENTITY}
    subprocess.run(git_argv(repo, "add", "--", *paths), check=True, capture_output=True, env=env)
    status = subprocess.run(
        git_argv(repo, "status", "--porcelain", "--", *paths),
        capture_output=True, text=True, env=env,
    ).stdout
    if not status.strip():
        return
    subprocess.run(git_argv(repo, "commit", "-q", "-m", message, "--", *paths),
                   check=True, capture_output=True, env=env)


def _build_prefixes(config: EngineConfig) -> tuple[str, ...]:
    return tuple(config.station_owners.get("build", {}).get(BUILDER_ROLE, ()))


def _probe_log_path(repo: Path, job_name: str, index: int) -> Path:
    from engine.gates import probe_target_id

    return repo / ".gongbang" / job_name / "probe" / f"{probe_target_id(index)}.log"


def _capture_probe_logs(repo: Path, job_name: str, targets: list[tuple[str, str]],
                        ) -> list[tuple[str, Path, trusted.Trusted]]:
    """Every target's probe log as checked (engine.trusted.capture), right
    after the engine's own probe run passed; a log that cannot be read
    safely is left out, the same as an unreadable one in the prompt."""
    captured: list[tuple[str, Path, trusted.Trusted]] = []
    for i, (name, _url) in enumerate(targets):
        path = _probe_log_path(repo, job_name, i)
        try:
            captured.append((name, path, trusted.capture(path)))
        except OSError:
            continue
    return captured


def _verified_probe_text(captured: list[tuple[str, Path, trusted.Trusted]],
                         ) -> tuple[str, str | None]:
    """("## 실측 결과" text built from the captured logs, None), or ("",
    the first target whose log changed since it was checked)."""
    parts: list[str] = []
    for name, path, t in captured:
        text = _trusted_text(t, path)
        if text is None:
            return "", name
        parts.append(f"### {name}\n{text[:PROBE_LOG_CAP]}")
    if not parts:
        return "", None
    return "## 실측 결과\n" + "\n\n".join(parts), None


def _probe_logs_text(repo, job_name: str, targets: list[tuple[str, str]]) -> str:
    """Every target's own probe log (capped at PROBE_LOG_CAP chars
    each), under one "## 실측 결과" heading -- "" if `targets` is empty
    or none of the logs can be read (check_probe already gated on
    them existing and saying PROBE OK before this is ever called, so
    a read failure here would mean the file vanished in between)."""
    parts: list[str] = []
    from engine.gates import probe_target_id

    for i, (name, _url) in enumerate(targets):
        path = repo / ".gongbang" / job_name / "probe" / f"{probe_target_id(i)}.log"
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


def _requirements_without_evidence(ledger: Ledger, project: str, job_name: str,
                                   plan: Plan) -> tuple[str, ...]:
    """Requirement ids whose evidence chain (engine.evidence_graph: a
    covering scene that passed in the newest recorded acceptance run, or
    for a task-only requirement a commit) is incomplete. Only the
    per-requirement links are checked here; the job-wide acceptance and
    security certificates are checked at release."""
    from engine import evidence_graph

    graph = evidence_graph.build(ledger, project, job_name, plan)
    requirement_ids = {r.requirement_id for r in graph.requirements}
    out: list[str] = []
    for line in evidence_graph.missing(graph):
        requirement_id = line.split(":", 1)[0]
        if requirement_id in requirement_ids and requirement_id not in out:
            out.append(requirement_id)
    return tuple(out)


def _maybe_release_or_done(config: EngineConfig, ledger: Ledger, project: str, job_name: str,
                           attempt_id: str, report_path: Path, max_fix_rounds: int) -> FullOutcome:
    """The LAST step of a whole job (ENGINE.md release track, an earlier round
    contract item 6): a project with no `release` configured reaches
    "done" straight away, exactly as before. A project WITH `release`
    configured instead submits ONE publish request for the whole job
    (engine.release.prepare, bound to `attempt_id`'s own current repo
    HEAD -- by now that HEAD already carries every task's commits plus
    the handoff commit) and stops needs_owner "release_approval";
    gb2's ship/skip-ship flow (outside this module) drives that request
    to done/failed, after which the STAGE_RELEASE_APPROVAL resume turns
    it into this job's final FullOutcome."""
    _record(ledger, project, job_name, STAGE_POST_TASKS, attempt_id, next_action="release",
            max_fix_rounds=max_fix_rounds)
    release_config = config.projects[project].release
    if release_config is None:
        record_release_decision(ledger, project, attempt_id, job_name, "not_configured")
        _write_final_report(config, ledger, project, attempt_id, job_name, "not_configured")
        return FullOutcome("done", None, attempt_id, str(report_path))
    # engine.release.prepare's own submit() requires an ACTIVE attempt --
    # `attempt_id` (the last task/fix attempt) already closed by the time
    # this runs (its own "close" stage already ran). Open a fresh, roleless
    # attempt purely to carry this one publish request -- "a final attempt
    # bound to the final commit": its base_head is
    # this exact repo HEAD (every task's commit plus the handoff commit),
    # and it is left OPEN (never closed here) while the job waits for the
    # owner's approval -- the release-approval position names it.
    intent = JobPosition(job=job_name, stage=STAGE_RELEASE_APPROVAL, attempt_id=None,
                         stop_reason="release_approval",
                         extra={"report_path": str(report_path),
                                "max_fix_rounds": max_fix_rounds})
    release_attempt_id = _open_with_intent(config, ledger, project, intent, ())
    return _prepare_release(ledger, config, project, replace(intent, attempt_id=release_attempt_id))


def _prepare_release(ledger: Ledger, config: EngineConfig, project: str,
                     pos: JobPosition) -> FullOutcome:
    """Submits the job's one publish request on the release attempt
    (engine.release.prepare: the same HEAD always yields the same
    request) and records the release-approval stop naming it."""
    from engine import release as release_mod

    request_id, _summary = release_mod.prepare(config, ledger, project, pos.attempt_id or "")
    record_position(ledger, project, replace(pos, extra={**pos.extra, "request_id": request_id}))
    return FullOutcome("needs_owner", "release_approval", pos.attempt_id, None)


def _record_context(ledger: Ledger, project: str, attempt_id: str | None, station: str,
                    sc: "context_mod.StationContext") -> None:
    """Appends the context.injected ledger event for this station's
    prompt (and, if the library lookup failed, context.failed right
    after it) -- see engine.context.build's own docstring for what each
    field means. `attempt_id` may be None (a build/fix task's attempt
    is only opened AFTER its prompt text is built -- see
    engine.full_orbit's own module docstring on fresh-attempt-per-task)."""
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


def _planned_skills(config: EngineConfig, plan: Plan, station: str) -> tuple[str, ...]:
    """The skills this job's planner picked for `station` that the engine
    can actually load (needs_install ones are never injected)."""
    usable, _needs_install = classify_skill_tools(plan, config)
    return tuple(tools_for_station(plan, station, usable_skills=usable)["skill"])


def _build_station_context(config: EngineConfig, ledger: Ledger, project: str,
                           attempt_id: str | None, station: str, query: str,
                           planned_skills: tuple[str, ...] = ()) -> "context_mod.StationContext":
    """engine.context.build for this station's prompt, recorded on the
    ledger (context.injected/context.failed) exactly once -- the
    shared first half of _prompt_with_context, broken out so a caller
    that also needs to check `.unmet_required()` (ContextConfig.
    required_skills/require_library -- see that module's own
    docstring) BEFORE ever submitting run_station can do so without
    building/recording the context a second time."""
    sc = context_mod.build(config, project, station, query, planned_skills)
    _record_context(ledger, project, attempt_id, station, sc)
    return sc


def _prompt_with_context(config: EngineConfig, ledger: Ledger, project: str,
                         attempt_id: str | None, station: str, query: str,
                         prompt: str, planned_skills: tuple[str, ...] = ()) -> str:
    """Builds this station's injected-context appendix (engine.context.
    build), records it on the ledger, and returns `prompt` with that
    appendix appended (unchanged if there is nothing to inject). Used
    by every call site that never needs to stop on `.unmet_required()`
    itself (review's own context, which never gates a needs_owner stop
    -- a review failure is an ordinary rejection, not an owner-request)."""
    sc = _build_station_context(config, ledger, project, attempt_id, station, query,
                                planned_skills)
    if not sc.text:
        return prompt
    return f"{prompt}\n\n{sc.text}"


def _unmet_context_reason(sc: "context_mod.StationContext") -> str:
    """A short, stable reason code for a `.unmet_required()` stop --
    read back by nothing (this job always needs the SAME config fix
    either way), but kept informative for `gb2 status`/`gb2 board`."""
    if sc.required_missing:
        return f"context_required_missing:{','.join(sc.required_missing)}"[:200]
    return "context_required_library_failed"


def _stop_early_station_for_unmet_context(ledger: Ledger, project: str, attempt_id: str,
                                          job_name: str, station: str, request: str,
                                          max_fix_rounds: int,
                                          sc: "context_mod.StationContext",
                                          probe_text: str = "") -> FullOutcome:
    """A facts/probe/plan/design station's own required-context gap --
    stopped needs_owner on the SAME shared early attempt, in the exact
    STAGE_EARLY_STATION shape _record_early_station_stop already uses
    for a run_station owner-request, but WITHOUT ever calling
    run_station at all: there is nothing a worker could do about a
    missing skill/company-library lookup the engine itself could not
    load. No pending requests (nothing to approve) means the
    STAGE_EARLY_STATION resume reruns this station fresh once `gb2
    continue` is called -- the owner fixing the
    underlying config/skill root between stops is what actually
    changes anything, not an approval."""
    reason = _unmet_context_reason(sc)
    _record_early_station_stop(ledger, project, attempt_id, job_name, station, reason, (),
                               request=request, max_fix_rounds=max_fix_rounds,
                               probe_text=probe_text)
    return FullOutcome("needs_owner", reason, attempt_id, None)


def _stop_build_task_for_unmet_context(config: EngineConfig, ledger: Ledger, project: str,
                                       job_name: str, task: Task, task_text: str,
                                       base_task_text: str, sc: "context_mod.StationContext",
                                       *, fixing: bool, remaining_task_ids: tuple[str, ...],
                                       max_fix_rounds: int) -> FullOutcome:
    """A build task's own required-context gap -- opens this task's own
    fresh attempt (the same roles _run_task_attempt would, recording the
    same task position) but never
    submits run_station at all, going straight to a STAGE_TASK_OWNER
    needs_owner stop -- the same shape a run_station owner-request stop
    already uses, so `gb2 continue` reruns this task's build fresh
    (never a second, wasted worker call for a gap the engine itself,
    not the worker, must resolve)."""
    reason = _unmet_context_reason(sc)
    task_pos = _task_position(job_name, None, task.id, task_text, base_task_text,
                              fixing=fixing, remaining_task_ids=remaining_task_ids,
                              max_fix_rounds=max_fix_rounds)
    # context_gap: the engine, not the worker, stopped this task -- the
    # resume rebuilds and rechecks the context instead of treating it as
    # an owner-request fulfillment or a plain rerun.
    stop = task_stop_position(task_pos, None, reason, (), context_gap=True)
    attempt_id = _open_with_intent(config, ledger, project, stop, (BUILDER_ROLE,))
    record_position(ledger, project, replace(stop, attempt_id=attempt_id))
    return FullOutcome("needs_owner", reason, attempt_id, None)


def _open_with_intent(config: EngineConfig, ledger: Ledger, project: str,
                      intent: JobPosition, roles: tuple[str, ...]) -> str:
    """Opens an attempt with the position it is opened for recorded
    first: `intent` without an attempt id, carrying the roles to open
    with (engine.position.opening_roles). A crash between that record
    and the caller's own position on the attempt resumes by adopting the
    attempt open_job made, or opening it (reconcile_opening)."""
    record_position(ledger, project, replace(
        intent, attempt_id=None, extra={**intent.extra, "roles": list(roles)}))
    attempt_id = open_job(config, ledger, project, intent.job, "full", roles)
    faults.flow_boundary("full_orbit.attempt_opened")
    return attempt_id


def _attempt_opened_after(ledger: Ledger, project: str, job_name: str,
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
    attempt_id = _attempt_opened_after(run.ledger, run.project, run.job, marker_seq)
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
    factory = run.processor_factory or orbit._default_processor_factory
    processor = factory(run.config, run.ledger, run.fakes_for(attempt_id))
    status = processor.run_request(run.project, pending[-1])
    if status != "done" or run.ledger.project(run.project).state != "CLOSED":
        return FullOutcome("failed", f"close:{status}", attempt_id, None)
    return None


def _attempt_has_requests(ledger: Ledger, project: str, attempt_id: str) -> bool:
    return any(event.attempt_id == attempt_id
               for _seq, event in ledger.events(project, "request.submitted"))


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
    payload on top of the fixed station/role/task keys. `task_text`
    already differs on a STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW resume
    (the caller appends the previous attempt's own log/plan.json as
    context -- see _run_probe/_run_plan_and_review), so run_station's
    own deterministic request_id is never the problem; the merge
    stage's payload, on its own, names only role/station/path (fixed
    for this attempt), so a SECOND merge of the same role here would
    otherwise collide with the first merge's own already-"done" request
    and never actually reapply anything (engine.processor.submit's
    idempotency) -- `merge_retry_n` (how many merges this attempt has
    already recorded) makes each retry's merge payload genuinely new."""
    payload: dict[str, object] = {"station": station, "role": role, "task": task_text}
    if extra_payload:
        payload.update(extra_payload)
    copy = str(copy_root(config, attempt_id, role))
    merge_retry_n = sum(1 for _seq, event in ledger.events(project, "merge.recorded")
                        if event.attempt_id == attempt_id)
    merge_payload: dict[str, object] = {
        "copies": [{"role": role, "station": station, "path": copy}],
    }
    if merge_retry_n:
        merge_payload["merge_retry_n"] = merge_retry_n
    stages = [
        orbit.Stage("run_station", payload),
        orbit.Stage("merge", merge_payload),
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


def _task_build_owns(task: Task, plan: Plan, job_name: str) -> list[str]:
    """This task's own write prefixes (task.owns) plus, only when
    plan.ui, the engine's own extra screens/ allowance (see
    _config_for_build_task) -- never part of the plan's own owns, so
    the planner never has to know or ask for it. Shared by the
    run_station payload (_builder_run_station_payload) and the merge
    payload (_run_task_attempt/the STAGE_TASK_OWNER worker_stuck
    resume) so merge's own re-check (engine.steps.merge._owner_prefixes)
    is narrowed to exactly the same set run_station's own gate already
    enforced while the worker was running -- never the broader,
    unnarrowed station prefix (a per-task merge used to
    accept a write anywhere under the WHOLE build station's own
    prefixes, not just this task's own owns)."""
    owns = list(task.owns)
    if plan.ui:
        owns.append(f".gongbang/{job_name}/screens/")
    return owns


def _builder_run_station_payload(task: Task, task_text: str, plan: Plan,
                                 config: EngineConfig, job_name: str) -> dict[str, object]:
    usable_skills, _needs_install = classify_skill_tools(plan, config)
    return {"station": "build", "role": BUILDER_ROLE, "task": task_text,
            "owns": _task_build_owns(task, plan, job_name),
            "tools": tools_for_station(plan, "build", usable_skills=usable_skills)}


def _review_payload(config: EngineConfig, ledger: Ledger, project: str, plan: Plan,
                    task: str) -> dict[str, object]:
    """The review stage judges against the task itself (without the
    build station's injected skills) and gets its own station context:
    the review base skills plus the skills the planner picked for
    review. Also carries the planner's own `tools_for_station(plan,
    "review")` table -- without it, engine.steps.review's own mcp
    narrowing (_payload_mcp_names) always saw an empty "tools" key,
    so neither the codex reviewer's own "reviewer_mcp_unsupported"
    guard (which only fires when the payload actually asks for mcp)
    nor a claude reviewer's actual mcp access ever reflected what the
    plan asked for."""
    review_context = _prompt_with_context(config, ledger, project, None, "review",
                                          task, "", _planned_skills(config, plan, "review"))
    usable_skills, _needs_install = classify_skill_tools(plan, config)
    payload: dict[str, object] = {
        "role": REVIEWER_ROLE, "task": task,
        "tools": tools_for_station(plan, "review", usable_skills=usable_skills),
    }
    if review_context.strip():
        payload["context"] = review_context.strip()
    return payload


def _install_followup_task_text(task_text: str, build_copy: Path) -> str:
    """`task_text` (the builder's own task, with its skill-context
    appendix already attached) plus a note that the install it once
    asked for has since completed -- never start over, pick up from
    where it stopped. `build_copy`'s own dirty_paths (the copy's real
    git status, never the worker's self-report) lists what it had
    already changed before it asked for the install, same evidence
    engine.orbit._continuation_payload shows a stalled worker."""
    changed = dirty_paths(build_copy, ())
    listed = "\n".join(f"- {p}" for p in changed) or "- (아직 없음)"
    note = (
        "\n\n[의존성 설치 완료] 의존성이 설치됐다. 처음부터 다시 하지 말고, 멈춘 곳부터 "
        "이어서 끝내라. 이미 바뀐 아래 파일을 먼저 읽는다.\n"
        f"{listed}"
    )
    return f"{task_text}{note}"


def task_attempt_config(config: EngineConfig, project: str, job_name: str,
                         plan: Plan) -> EngineConfig:
    """The one config every stage of a build task's attempt runs with --
    first run and every resume alike: the plan's UI screens/ allowance
    added (_config_for_build_task) and the project's own `release`
    removed (engine.position.without_release)."""
    return without_release(_config_for_build_task(config, job_name, plan), project)


def _task_stages(config: EngineConfig, build_config: EngineConfig, ledger: Ledger,
                 project: str, job_name: str, plan: Plan, task: Task, task_text: str,
                 review_task_text: str, attempt_id: str) -> list[orbit.Stage]:
    """A build task's seven stages: run_station(build) -> merge (narrowed
    to the task's own owns) -> review -> bench -> commit -> report ->
    close. A crash resume submits exactly this list again, so every
    request that already finished reconciles by its own id and nothing
    runs twice."""
    build_copy = str(copy_root(build_config, attempt_id, BUILDER_ROLE))
    return [
        orbit.Stage("run_station", _builder_run_station_payload(task, task_text, plan,
                                                                 build_config, job_name)),
        orbit.Stage("merge", {"copies": [
            {"role": BUILDER_ROLE, "station": "build", "path": build_copy,
             "owns": _task_build_owns(task, plan, job_name)},
        ]}),
        orbit.Stage("review", _review_payload(config, ledger, project, plan, review_task_text)),
        orbit.Stage("bench", {}),
        orbit.Stage("commit", {"message": f"gongbang: {job_name} - {task.id}"}),
        orbit.Stage("report", {"summary": f"gongbang: {job_name} - {task.id}"}),
        orbit.Stage("close", {}),
    ]


def _run_task_attempt(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, task: Task, task_text: str, plan: Plan,
                      on_progress: ProgressCallback | None,
                      processor_factory: orbit.ProcessorFactory | None,
                      base_task_text: str | None = None, *,
                      fixing: bool = False, remaining_task_ids: tuple[str, ...] = (),
                      max_fix_rounds: int = 2, fix_round: int | None = None,
                      ) -> orbit.JobOutcome:
    """Open a FRESH attempt for this one task (see module docstring for
    why) and run its full run_station(build) -> merge -> review ->
    bench -> commit -> report -> close sequence -- exactly
    engine.orbit.default_stages()'s own seven stages, just built
    directly here so the run_station payload can carry this task's own
    `owns`, which default_stages() has no parameter for.

    `fixing`/`remaining_task_ids`/`max_fix_rounds` carry nothing this
    function itself needs -- they are recorded on the task position so a
    later resume knows what this task IS (an acceptance-fix round or a
    plain plan.tasks entry) and what is still queued behind it. If the
    attempt stops for the owner, that stop is recorded here too, as the
    task position moved to STAGE_TASK_OWNER."""
    task_pos = _task_position(job_name, None, task.id, task_text,
                              base_task_text or task_text, fixing=fixing,
                              remaining_task_ids=remaining_task_ids,
                              max_fix_rounds=max_fix_rounds)
    attempt_id = _open_with_intent(config, ledger, project, task_pos, (BUILDER_ROLE,))
    build_config = task_attempt_config(config, project, job_name, plan)
    stages = _task_stages(config, build_config, ledger, project, job_name, plan, task,
                          task_text, base_task_text or task_text, attempt_id)
    # The stage list is recorded once, with the task's position on its
    # attempt: every recovery and a fix of this task replay exactly it.
    task_pos = replace(task_pos, attempt_id=attempt_id,
                       extra={**task_pos.extra, "stages": stages_spec(stages)})
    record_position(ledger, project, task_pos)
    outcome = orbit.continue_job(build_config, ledger, _fakes_for(fakes, attempt_id), project,
                                 attempt_id, stages, processor_factory=processor_factory,
                                 on_progress=on_progress)
    if _stops_for_owner(outcome):
        stop_extra = {"fix_round": fix_round} if fix_round is not None else {}
        _record_task_stop(ledger, project, task_pos, outcome, **stop_extra)
    return outcome


def _run_tasks(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
              job_name: str, plan: Plan, tasks: list[Task],
              on_progress: ProgressCallback | None,
              processor_factory: orbit.ProcessorFactory | None,
              last_attempt_id: str | None = None, max_fix_rounds: int = 2,
              ) -> tuple[FullOutcome | None, str | None]:
    """Run every task in `tasks` (already in dependency order), each in
    its own fresh attempt, to completion. (None, last_attempt_id) iff
    all of them finished "done" -- otherwise (the needs_owner/failed
    FullOutcome to return right now, its own attempt_id), having
    already journaled enough progress (remaining task ids) for
    continue_full_job to pick up at the task that stopped.

    `last_attempt_id`, when given, is the attempt that already covers
    whatever `tasks` was narrowed down FROM (e.g. a STAGE_TASK_OWNER
    resume's own already-finished attempt, once the task that stopped
    is dropped off the front of `remaining_task_ids`) -- if `tasks` is
    now empty (that dropped task was the LAST one in the plan), the
    loop below never runs, and this value is returned UNCHANGED rather
    than silently falling back to None: the caller still needs a real
    attempt_id to pass acceptance (a None attempt_id used to crash
    engine.acceptance.run_acceptance on exactly this resume path)."""
    for index, task in enumerate(tasks):
        base_task_text = builder_task_text(task, plan.goal, job_name, plan.ui)
        remaining_now = tuple(t.id for t in tasks[index:])
        build_sc = _build_station_context(config, ledger, project, None, "build",
                                          base_task_text, _planned_skills(config, plan, "build"))
        if build_sc.unmet_required():
            return _stop_build_task_for_unmet_context(
                config, ledger, project, job_name, task, base_task_text, base_task_text,
                build_sc, fixing=False, remaining_task_ids=remaining_now,
                max_fix_rounds=max_fix_rounds,
            ), last_attempt_id
        task_text = (f"{base_task_text}\n\n{build_sc.text}" if build_sc.text else base_task_text)
        outcome = _run_task_attempt(config, ledger, fakes, project, job_name, task,
                                    task_text, plan, on_progress, processor_factory,
                                    base_task_text, remaining_task_ids=remaining_now,
                                    max_fix_rounds=max_fix_rounds)
        last_attempt_id = outcome.attempt_id
        full = _station_outcome_to_full(outcome, "build")
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

    _record(ledger, project, job_name, STAGE_POST_TASKS, last_attempt_id,
            next_action="acceptance", max_fix_rounds=max_fix_rounds)
    acc = acceptance_mod.run_acceptance(config, ledger, project, last_attempt_id, plan)
    rounds = 0
    while not acc.passed and rounds < max_fix_rounds:
        failing = tuple(s.id for s in acc.scenes if not s.passed)
        all_owns = tuple(sorted({entry for t in plan.tasks for entry in t.owns}))
        fix_task = Task(id=f"fix-{rounds + 1}", title="수락 실패 수정", detail="",
                        owns=all_owns, depends_on=())
        # getattr with a default: these two AcceptanceResult fields (an earlier round
        # contract item 2) may not exist yet in this worktree's own
        # engine.acceptance -- fix_task_text falls back to its own
        # "no failing scene" wording either way.
        base_task_text = fix_task_text(
            fix_task, plan.goal, failing, job_name, plan.ui,
            full_suite_returncode=getattr(acc, "full_suite_returncode", None),
            full_suite_tail=getattr(acc, "full_suite_tail", ""),
        )
        fix_sc = _build_station_context(config, ledger, project, None, "build", base_task_text,
                                        _planned_skills(config, plan, "build"))
        if fix_sc.unmet_required():
            return _stop_build_task_for_unmet_context(
                config, ledger, project, job_name, fix_task, base_task_text, base_task_text,
                fix_sc, fixing=True, remaining_task_ids=(), max_fix_rounds=max_fix_rounds,
            )
        task_text = (f"{base_task_text}\n\n{fix_sc.text}" if fix_sc.text else base_task_text)
        outcome = _run_task_attempt(config, ledger, fakes, project, job_name, fix_task,
                                    task_text, plan, on_progress, processor_factory,
                                    base_task_text, fixing=True,
                                    max_fix_rounds=max_fix_rounds, fix_round=rounds)
        last_attempt_id = outcome.attempt_id
        full = _station_outcome_to_full(outcome, "build")
        if full is not None:
            return full
        rounds += 1
        _record(ledger, project, job_name, STAGE_POST_TASKS, last_attempt_id,
                next_action="acceptance", max_fix_rounds=max_fix_rounds)
        acc = acceptance_mod.run_acceptance(config, ledger, project, last_attempt_id, plan)

    if not acc.passed:
        return FullOutcome("failed", "max_fix_rounds_exceeded", last_attempt_id, None)

    uncovered = _requirements_without_evidence(ledger, project, job_name, plan)
    if uncovered:
        reason = f"requirements_uncovered:{','.join(uncovered)}"
        _record(ledger, project, job_name, STAGE_REQUIREMENTS_REVIEW, last_attempt_id,
                stop_reason=reason, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, last_attempt_id, None)

    _record(ledger, project, job_name, STAGE_POST_TASKS, last_attempt_id,
            next_action="handoff", max_fix_rounds=max_fix_rounds)
    report_path = handoff_mod.write_handoff(config, ledger, project, last_attempt_id,
                                            job_name, plan, acc)
    _commit_job_trail(config, ledger, project, last_attempt_id, job_name,
                      f"gongbang: {job_name} - handoff")
    return _finish_after_handoff(config, ledger, project, job_name, plan, last_attempt_id,
                                 report_path, max_fix_rounds)


def _finish_after_handoff(config: EngineConfig, ledger: Ledger, project: str, job_name: str,
                          plan: Plan, attempt_id: str, report_path: Path,
                          max_fix_rounds: int) -> FullOutcome:
    """Everything after the handoff report is written and committed:
    the app-wide security check  and, only once that passes, the
    job's single release request (_maybe_release_or_done)."""
    from engine import app_security  # worker B's module; lazy import

    _record(ledger, project, job_name, STAGE_POST_TASKS, attempt_id,
            next_action="security", max_fix_rounds=max_fix_rounds)
    security = app_security.check(config, project, attempt_id, plan)
    # The result (findings carry kind:path:line only, never values) is
    # recorded for the release gate and for the person fixing it.
    app_security.record(ledger, project, attempt_id, security)
    if not security.passed:
        findings = "; ".join(list(security.findings)[:5])
        reason = f"security_failed:{findings}"[:300] if findings else "security_failed"
        # The repo's own content fingerprint (engine.evidence.
        # fingerprint, a tree hash covering the working tree -- tracked
        # AND dirty paths alike, same value engine.acceptance's own
        # before/after check uses) acceptance/handoff actually
        # certified -- read back on resume (STAGE_SECURITY_FIX) to tell
        # a plain security retry from one where the owner's own fix has
        # since changed the source, which must rerun acceptance/
        # coverage/handoff against the new source before security runs
        # again, not just security itself. A plain `git rev-parse HEAD`
        # (this field's own earlier implementation) missed exactly the
        # case this exists for: an owner fix that EDITS the source
        # WITHOUT committing (`gb2 fix` always commits on a fresh
        # attempt and so moves HEAD, but a direct owner edit to the
        # working tree does not) left HEAD unchanged, so that comparison
        # silently skipped rerunning acceptance/coverage/handoff and let
        # a build-only job report "done" against source acceptance never
        # actually certified.
        from engine import evidence  # worker C's module; lazy import

        excludes = config.projects[project].excludes
        certified_fingerprint = evidence.fingerprint(config.projects[project].repo, excludes)
        _record(ledger, project, job_name, STAGE_SECURITY_FIX, attempt_id,
                stop_reason=reason, report_path=str(report_path),
                max_fix_rounds=max_fix_rounds, certified_fingerprint=certified_fingerprint)
        return FullOutcome("needs_owner", reason, attempt_id, None)
    return _maybe_release_or_done(config, ledger, project, job_name, attempt_id,
                                  report_path, max_fix_rounds)


def _finish_from_tasks(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                       job_name: str, plan: Plan, tasks: list[Task], max_fix_rounds: int,
                       on_progress: ProgressCallback | None,
                       processor_factory: orbit.ProcessorFactory | None,
                       last_attempt_id: str | None = None) -> FullOutcome:
    stopped, last_attempt_id = _run_tasks(config, ledger, fakes, project, job_name, plan,
                                         tasks, on_progress, processor_factory,
                                         last_attempt_id, max_fix_rounds)
    if stopped is not None:
        return stopped
    return _run_acceptance_and_handoff(config, ledger, fakes, project, job_name, plan,
                                       max_fix_rounds, on_progress, processor_factory,
                                       last_attempt_id)


def _append_owner_request_resume_note(config: EngineConfig, attempt_id: str, role: str,
                                      task_text: str, needs_resume_note: bool) -> str:
    """`task_text` unchanged for a fresh run OR a "worker_stuck" resume
    (submit()'s own with_retry already busts a FAILED request's
    payload automatically, so an unchanged prompt still reruns for
    real -- and orbit never records "worker_stuck" WITH any pending
    requests at all, so `needs_resume_note` is always False there) --
    but with a continuation note appended for an owner-request resume
    (`needs_resume_note` True: this STAGE_EARLY_STATION stop had a
    non-empty, now-fulfilled `pending_requests`): that station's own
    run_station request ended "done", so resubmitting this SAME,
    unchanged prompt would just reconcile onto that already-"done"
    request -- and its already-recorded pending install -- forever,
    rather than ever actually giving the station a chance to finish
    (the exact infinite-stall bug a test caught). Reuses the same note
    engine.full_orbit's own build-task resume gives a builder after an
    install (_install_followup_task_text)."""
    if not needs_resume_note:
        return task_text
    return _install_followup_task_text(task_text, copy_root(config, attempt_id, role))


def _run_probe(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
              job_name: str, attempt_id: str, request: str,
              on_progress: ProgressCallback | None,
              processor_factory: orbit.ProcessorFactory | None,
              max_fix_rounds: int, needs_resume_note: bool = False,
              ) -> tuple[FullOutcome | None, str]:
    """Runs the probe station from scratch (a fresh run_station call --
    used for a job's very first probe attempt, a STAGE_PROBE_RETRY
    resume, AND a STAGE_EARLY_STATION owner-request resume -- see
    _probe_plan_design_close/_append_owner_request_resume_note).
    Returns (None, probe_text) to keep going, or (the needs_owner/
    failed FullOutcome to return right now, "")."""
    _record(ledger, project, job_name, STAGE_PROBE, attempt_id, next_action="run_station",
            request=request, max_fix_rounds=max_fix_rounds)
    repo = config.projects[project].repo
    try:
        targets = check_probe_targets(repo, job_name)
    except ProbeTargetsError as exc:
        # A malformed 외부 대상 line is never silently skipped: a person
        # fixes facts.md, then continue reruns this station.
        reason = f"probe_targets_invalid:{exc}"[:200]
        _record(ledger, project, job_name, STAGE_PROBE_RETRY, attempt_id, stop_reason=reason,
                request=request, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, attempt_id, None), ""
    if not targets:
        faults.flow_boundary("full_orbit.probe_skipped")
        ledger.append(Event(
            project=project, kind="probe.skipped", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id, payload={"job": job_name},
        ))
        return None, ""
    base_probe_text = prober_prompt(request, job_name, targets)
    # A STAGE_PROBE_RETRY resume reruns this on the SAME (never-closed)
    # attempt -- engine.ids/submit's own request_id is deterministic from
    # this payload, so an unchanged prompt would collide with the FIRST
    # run's already-"done" request and never actually run again (the
    # RunStationStub-shaped bug this fixed). The previous attempt's own
    # already-committed logs (if any -- "" on a job's first probe call)
    # make the retry's payload genuinely different, and give the worker
    # real context on what failed last time.
    previous_logs = _probe_logs_text(repo, job_name, targets)
    probe_prompt_text = (f"{base_probe_text}\n\n이전 시도 로그(다시 시도한다):\n{previous_logs}"
                         if previous_logs else base_probe_text)
    probe_sc = _build_station_context(config, ledger, project, attempt_id, "probe", request)
    if probe_sc.unmet_required():
        return _stop_early_station_for_unmet_context(
            ledger, project, attempt_id, job_name, "probe", request, max_fix_rounds, probe_sc,
        ), ""
    probe_task_text = (f"{probe_prompt_text}\n\n{probe_sc.text}" if probe_sc.text
                       else probe_prompt_text)
    probe_task_text = _append_owner_request_resume_note(
        config, attempt_id, PROBER_ROLE, probe_task_text, needs_resume_note)
    outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "probe",
                             PROBER_ROLE, probe_task_text, on_progress,
                             processor_factory)
    full = _station_outcome_to_full(outcome, "probe")
    if full is not None:
        _record_early_station_stop(ledger, project, attempt_id, job_name, "probe",
                                   outcome.reason, outcome.pending_requests, request=request,
                                   max_fix_rounds=max_fix_rounds)
        return full, ""
    _commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - probe")
    # Pass/fail comes from the engine's own run of each probe script
    # (probe.executed: script, return code, output); the worker's log
    # text ("PROBE OK") is supporting context only.
    from engine import acceptance as acceptance_mod

    evidence = acceptance_mod.run_probe_scripts(config, ledger, project, attempt_id,
                                                job_name, targets)
    failed = [e.name for e in evidence if not e.ok]
    if failed:
        failed_names = ",".join(failed)
        reason = f"probe_failed:{failed_names}"
        _record(ledger, project, job_name, STAGE_PROBE_RETRY, attempt_id, stop_reason=reason,
                request=request, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, attempt_id, None), ""
    probe_text, changed = _verified_probe_text(_capture_probe_logs(repo, job_name, targets))
    if changed is not None:
        reason = f"probe_log_changed_after_check:{changed}"[:200]
        _record(ledger, project, job_name, STAGE_PROBE_RETRY, attempt_id, stop_reason=reason,
                request=request, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, attempt_id, None), ""
    return None, probe_text


def _run_plan_and_review(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                         job_name: str, attempt_id: str, request: str, probe_text: str,
                         on_progress: ProgressCallback | None,
                         processor_factory: orbit.ProcessorFactory | None,
                         max_fix_rounds: int, needs_resume_note: bool = False,
                         facts: trusted.Trusted | None = None,
                         ) -> tuple[FullOutcome | None, Plan | None]:
    """Runs the plan station, then reviews that same change, from
    scratch -- used for a job's first plan attempt, a STAGE_PLAN_REVIEW
    resume, AND a STAGE_EARLY_STATION owner-request resume. Returns
    (None, plan) to keep going, or (the needs_owner/failed FullOutcome
    to return right now, None).

    `facts` is facts.md as captured when the facts gate passed in this
    same run; a resume that starts here checks and captures it again.
    The planner prompt gets those exact bytes, verified unchanged."""
    _record(ledger, project, job_name, STAGE_PLAN, attempt_id, next_action="run_station",
            request=request, probe_text=probe_text, max_fix_rounds=max_fix_rounds)
    repo = config.projects[project].repo
    if facts is None:
        problems, facts = _checked_facts(repo, job_name)
        if facts is None:
            return FullOutcome("failed", "facts:" + "; ".join(problems), attempt_id, None), None
    facts_md = _trusted_text(facts, facts_path(repo, job_name))
    if facts_md is None:
        # facts.md changed after its check: stop for the owner; the
        # resume reruns this station, which checks facts.md again.
        reason = "facts_changed_after_check"
        _record(ledger, project, job_name, STAGE_PLAN_REVIEW, attempt_id, stop_reason=reason,
                request=request, probe_text=probe_text, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, attempt_id, None), None
    base_plan_prompt = planner_prompt(request, job_name, facts_md, skill_catalog_text(config),
                                      probe_text)
    # A STAGE_PLAN_REVIEW resume reruns this on the SAME (never-closed)
    # attempt -- same idempotent-request_id reasoning as _run_probe's own
    # retry note: an unchanged prompt would collide with the first
    # attempt's own already-"done" run_station request. The previous
    # (review-rejected, never committed) plan.json, still sitting in the
    # working tree from that first attempt's own merge, makes the retry's
    # payload different and shows the worker what it wrote last time.
    try:
        previous_plan_json = plan_path(repo, job_name).read_text(encoding="utf-8")
    except OSError:
        previous_plan_json = ""
    plan_prompt_text = (
        f"{base_plan_prompt}\n\n이전 시도의 plan.json(검토를 통과하지 못했다. 고쳐 다시 "
        f"쓴다):\n{previous_plan_json}" if previous_plan_json else base_plan_prompt
    )
    plan_sc = _build_station_context(config, ledger, project, attempt_id, "plan", request)
    if plan_sc.unmet_required():
        return _stop_early_station_for_unmet_context(
            ledger, project, attempt_id, job_name, "plan", request, max_fix_rounds, plan_sc,
            probe_text=probe_text,
        ), None
    plan_task_text = (f"{plan_prompt_text}\n\n{plan_sc.text}" if plan_sc.text
                      else plan_prompt_text)
    plan_task_text = _append_owner_request_resume_note(
        config, attempt_id, PLANNER_ROLE, plan_task_text, needs_resume_note)
    outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "plan",
                             PLANNER_ROLE, plan_task_text,
                             on_progress, processor_factory)
    full = _station_outcome_to_full(outcome, "plan")
    if full is not None:
        _record_early_station_stop(ledger, project, attempt_id, job_name, "plan",
                                   outcome.reason, outcome.pending_requests, request=request,
                                   max_fix_rounds=max_fix_rounds,
                                   probe_text=probe_text)
        return full, None
    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None), None
    problems = validate_plan(plan, repo, _build_prefixes(config))
    problems += validate_tools(plan, config)
    if problems:
        return FullOutcome("failed", "plan:" + "; ".join(problems), attempt_id, None), None

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
        _record(ledger, project, job_name, STAGE_PLAN_REVIEW, attempt_id,
                stop_reason="plan_review_failed", request=request, probe_text=probe_text,
                max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", "plan_review_failed", attempt_id, None), None

    _commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - plan", station="plan", role=PLANNER_ROLE)
    _record_plan_reviewed(ledger, project, attempt_id, job_name, repo)
    return None, plan


def _record_plan_reviewed(ledger: Ledger, project: str, attempt_id: str,
                          job_name: str, repo: Path) -> None:
    """The one durable record of which plan.json + scene files a
    reviewer last approved (engine.plan.record_plan_reviewed), read
    back by _maybe_rerun_plan_review on a later resume."""
    from engine import plan as plan_mod

    faults.flow_boundary("full_orbit.plan_reviewed")
    plan_mod.record_plan_reviewed(ledger, project, attempt_id, repo, job_name)


def _maybe_rerun_plan_review(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                             job_name: str, attempt_id: str, plan: Plan,
                             on_progress: ProgressCallback | None,
                             processor_factory: orbit.ProcessorFactory | None,
                             ) -> FullOutcome | None:
    """None to keep going -- either no "plan.reviewed" event was ever
    recorded for this job, or plan.json's current digest still matches the one
    last reviewed. Otherwise the needs_owner FullOutcome to return
    right now: the owner edited plan.json (e.g. fixing a scene the
    planner got wrong) between a needs_owner stop and this `gb2
    continue` -- that edit was never looked at by a reviewer, so this
    reruns the SAME review stage _run_plan_and_review's own first pass
    already runs, on `attempt_id` (still the shared early attempt --
    only ever called from the STAGE_DESIGN_APPROVAL resume, before any
    task attempt opens), and records a fresh "plan.reviewed" once it
    passes."""
    from engine.plan import plan_digest

    repo = config.projects[project].repo
    current_digest = plan_digest(plan_path(repo, job_name))
    reviewed_digest: object = None
    for _seq, event in ledger.events(project, "plan.reviewed"):
        if event.payload.get("job") == job_name:
            reviewed_digest = event.payload.get("digest")
    if reviewed_digest is None or reviewed_digest == current_digest:
        return None
    plan_review_outcome = orbit.continue_job(
        config, ledger, _fakes_for(fakes, attempt_id), project, attempt_id,
        [orbit.Stage("review", _review_payload(config, ledger, project, plan, plan.goal))],
        processor_factory=processor_factory, on_progress=on_progress,
    )
    if plan_review_outcome.status != "done":
        return FullOutcome("needs_owner", "plan_review_failed", attempt_id, None)
    # Commit the re-reviewed plan.json/scene bytes to the job trail
    # BEFORE recording plan.reviewed -- same ordering _run_plan_and_
    # review's own first pass uses (commit, then record). Without
    # this, the owner's edit that triggered this re-review was never
    # committed anywhere: it only exists as an uncommitted change in
    # the real repo, which engine.orbit's own "close" stage (git
    # checkout/clean to HEAD) wipes right back out the moment this
    # attempt closes -- so a later build/merge/commit would run
    # against the OLD, pre-edit plan.json even though plan.reviewed
    # now names the new digest as approved.
    _commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - plan", station="plan", role=PLANNER_ROLE)
    _record_plan_reviewed(ledger, project, attempt_id, job_name, repo)
    return None


def _probe_plan_design_close(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                             job_name: str, attempt_id: str, request: str,
                             max_fix_rounds: int, on_progress: ProgressCallback | None,
                             processor_factory: orbit.ProcessorFactory | None, *,
                             start: str = "probe", probe_text: str = "",
                             needs_resume_note: bool = False,
                             facts: trusted.Trusted | None = None) -> FullOutcome:
    """probe -> plan+review -> (design, if plan.ui) -> close -> build
    loop. `start` is "probe" (a fresh job, or a STAGE_PROBE_RETRY
    resume), "plan" (a STAGE_PLAN_REVIEW resume -- probe already
    succeeded and stays as-is, `probe_text` is its already-committed
    logs, read by the resume caller), or "design" (a STAGE_EARLY_
    STATION resume for station=="design" -- plan.json is already
    committed by then, so the plan is reloaded from disk rather than
    rerunning the planner at all; `probe_text` is unused). `needs_
    resume_note`, only ever True for a STAGE_EARLY_STATION owner-
    request resume, is given ONLY to whichever station `start` names --
    the one THIS resume is actually for -- never to a later station in
    the same call, which runs fresh regardless."""
    if start == "probe":
        full, probe_text = _run_probe(config, ledger, fakes, project, job_name, attempt_id,
                                      request, on_progress, processor_factory, max_fix_rounds,
                                      needs_resume_note=needs_resume_note)
        if full is not None:
            return full

    if start == "design":
        repo = config.projects[project].repo
        try:
            plan = load_plan(repo, job_name)
        except PlanError as exc:
            return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None)
    else:
        full, plan = _run_plan_and_review(
            config, ledger, fakes, project, job_name, attempt_id, request, probe_text,
            on_progress, processor_factory, max_fix_rounds,
            needs_resume_note=(needs_resume_note if start == "plan" else False), facts=facts)
        if full is not None:
            return full
        assert plan is not None

    if plan.ui:
        _record(ledger, project, job_name, STAGE_DESIGN, attempt_id, next_action="run_station",
                request=request, max_fix_rounds=max_fix_rounds)
        usable_skills, _needs_install = classify_skill_tools(plan, config)
        design_prompt_text = designer_prompt(request, job_name, plan.goal)
        design_sc = _build_station_context(config, ledger, project, attempt_id, "design",
                                           request, _planned_skills(config, plan, "design"))
        if design_sc.unmet_required():
            return _stop_early_station_for_unmet_context(
                ledger, project, attempt_id, job_name, "design", request, max_fix_rounds,
                design_sc,
            )
        design_task_text = (f"{design_prompt_text}\n\n{design_sc.text}" if design_sc.text
                            else design_prompt_text)
        design_task_text = _append_owner_request_resume_note(
            config, attempt_id, DESIGNER_ROLE, design_task_text,
            needs_resume_note if start == "design" else False)
        outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "design",
                                 DESIGNER_ROLE, design_task_text,
                                 on_progress, processor_factory,
                                 extra_payload={"tools": tools_for_station(
                                     plan, "design", usable_skills=usable_skills)})
        full = _station_outcome_to_full(outcome, "design")
        if full is not None:
            _record_early_station_stop(ledger, project, attempt_id, job_name, "design",
                                       outcome.reason, outcome.pending_requests, request=request,
                                       max_fix_rounds=max_fix_rounds)
            return full
        _commit_job_trail(config, ledger, project, attempt_id, job_name,
                          f"gongbang: {job_name} - design")
        if not check_design_approved(ledger, project, job_name):
            _record(ledger, project, job_name, STAGE_DESIGN_APPROVAL, attempt_id,
                    stop_reason="design_approval", max_fix_rounds=max_fix_rounds)
            return FullOutcome("needs_owner", "design_approval", attempt_id, None)

    run = Run(config, ledger, fakes, project, job_name, on_progress, processor_factory)
    return _close_early_attempt_then_tasks(run, attempt_id, plan, max_fix_rounds)


def _close_early_attempt_then_tasks(run: "Run", attempt_id: str, plan: Plan,
                                    max_fix_rounds: int, *, record: bool = True) -> FullOutcome:
    """Planning (and design approval, for a UI plan) is finished: record
    that, close the shared early attempt, then run the plan's tasks. A
    crash anywhere in the close resumes here (STAGE_EARLY_CLOSE), so the
    same close request is finished, never a new one opened."""
    if record:
        _record(run.ledger, run.project, run.job, STAGE_EARLY_CLOSE, attempt_id,
                max_fix_rounds=max_fix_rounds)
    close_outcome = _close_attempt(run.config, run.ledger, run.fakes, run.project, attempt_id,
                                   run.job, run.on_progress, run.processor_factory)
    if close_outcome.status != "done":
        return FullOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None)
    return _finish_from_tasks(run.config, run.ledger, run.fakes, run.project, run.job, plan,
                              ordered_tasks(plan), max_fix_rounds, run.on_progress,
                              run.processor_factory)


def _run_facts(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
              job_name: str, attempt_id: str, request: str,
              on_progress: ProgressCallback | None,
              processor_factory: orbit.ProcessorFactory | None,
              max_fix_rounds: int, needs_resume_note: bool = False,
              ) -> FullOutcome | None:
    """Runs the facts station from scratch -- used both for a job's
    very first call (run_full_job) and a STAGE_EARLY_STATION resume.
    Returns None to keep going (facts merged and committed -- check_
    facts' own content validation is the caller's job, same as
    before), or the needs_owner/failed FullOutcome to return right
    now."""
    _record(ledger, project, job_name, STAGE_FACTS, attempt_id, next_action="run_station",
            request=request, max_fix_rounds=max_fix_rounds)
    facts_sc = _build_station_context(config, ledger, project, attempt_id, "facts", request)
    if facts_sc.unmet_required():
        return _stop_early_station_for_unmet_context(
            ledger, project, attempt_id, job_name, "facts", request, max_fix_rounds, facts_sc)
    base_facts_prompt = facts_prompt(request, job_name)
    facts_task_text = (f"{base_facts_prompt}\n\n{facts_sc.text}" if facts_sc.text
                       else base_facts_prompt)
    facts_task_text = _append_owner_request_resume_note(
        config, attempt_id, FACTS_ROLE, facts_task_text, needs_resume_note)
    outcome = _run_and_merge(config, ledger, fakes, project, attempt_id, "facts",
                             FACTS_ROLE, facts_task_text, on_progress,
                             processor_factory)
    full = _station_outcome_to_full(outcome, "facts")
    if full is not None:
        _record_early_station_stop(ledger, project, attempt_id, job_name, "facts",
                                   outcome.reason, outcome.pending_requests, request=request,
                                   max_fix_rounds=max_fix_rounds)
        return full
    _commit_job_trail(config, ledger, project, attempt_id, job_name, f"gongbang: {job_name} - facts")
    return None


def run_full_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                 job_name: str, request: str, *, max_fix_rounds: int = 2,
                 on_progress: ProgressCallback | None = None,
                 processor_factory: orbit.ProcessorFactory | None = None) -> FullOutcome:
    intent = JobPosition(job=job_name, stage=STAGE_FACTS, attempt_id=None,
                         extra={"request": request, "max_fix_rounds": max_fix_rounds,
                                "next_action": "run_station"})
    attempt_id = _open_with_intent(config, ledger, project, intent, EARLY_ROLES)
    run = Run(config, ledger, fakes, project, job_name, on_progress, processor_factory)
    return _facts_then_rest(run, attempt_id, request, max_fix_rounds)


def _facts_then_rest(run: "Run", attempt_id: str, request: str, max_fix_rounds: int, *,
                     needs_resume_note: bool = False) -> FullOutcome:
    """facts -> check_facts -> probe -> plan -> design -> tasks."""
    full = _run_facts(run.config, run.ledger, run.fakes, run.project, run.job, attempt_id,
                      request, run.on_progress, run.processor_factory, max_fix_rounds,
                      needs_resume_note=needs_resume_note)
    if full is not None:
        return full
    problems, facts = _checked_facts(run.repo, run.job)
    if facts is None:
        return FullOutcome("failed", "facts:" + "; ".join(problems), attempt_id, None)
    return _probe_plan_design_close(run.config, run.ledger, run.fakes, run.project, run.job,
                                    attempt_id, request, max_fix_rounds, run.on_progress,
                                    run.processor_factory, start="probe", facts=facts)


def _task_attempt_committed(ledger: Ledger, project: str, attempt_id: str) -> bool:
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


def _abandon_orphaned_requests(ledger: Ledger, project: str, attempt_id: str) -> None:
    """Reconciles every non-terminal request left over for `attempt_id`
    before this stale attempt is discarded (see _reopen_for_retry's own
    docstring) -- a genuine crash can leave one "queued" (submitted but
    never dispatched) or "running" (Processor.run_once's own
    `_set_status(..., "running", {})` runs BEFORE the step that
    actually does the work, so a process death mid-dispatch never gets
    a chance to set a terminal status at all).

    "queued" is always safe to cancel outright (engine.processor.
    cancel_request -- it never dispatched, so there is no external
    effect to reconcile). "running" is NOT: a request only reaches
    "running" once some processor's run_once claimed it, and that
    processor may still be alive and mid-step right now -- marking it
    "failed" out from under a LIVE processor would let two processors
    act on the same request's external effect at once. This function
    therefore only ever judges a "running" request orphaned while it
    itself holds this project's processor lock (the same lock run_once/
    run_request take) -- proof no other processor can be running
    anything for this project right this moment. If the lock is already
    held by another live processor (acquire_lock returns False), every
    "running" request is left untouched -- the caller, _reopen_for_
    retry, has nothing useful to do with that either way: trying again
    later is always safe, since this function runs again on the next
    attempt-retry."""
    from engine.processor import cancel_request

    holder = f"full-orbit-orphan-{attempt_id}"
    have_lock = ledger.acquire_lock(project, holder)
    try:
        for _seq, event in ledger.events(project, "request.submitted"):
            if event.attempt_id != attempt_id:
                continue
            request_id = event.payload.get("request_id")
            if not isinstance(request_id, str):
                continue
            request = ledger.request(request_id)
            if request.status == "queued":
                cancel_request(ledger, project, request_id, "crash_resume_abandoned")
            elif request.status == "running" and have_lock:
                faults.flow_boundary("full_orbit.orphan_abandoned")
                ledger.append(Event(
                    project=project, kind="request.status", actor_kind="processor",
                    actor_id="processor", attempt_id=attempt_id,
                    payload={"request_id": request_id, "status": "failed",
                             "result": {"reason": "crash_resume_abandoned"}},
                ))
    finally:
        if have_lock:
            ledger.release_lock(project, holder)


# Marker files close's own archive_tmp writes alongside the archived
# paths themselves (never part of the real diff) -- same constant
# engine.fixup._ARCHIVE_MARKER_FILES defines, duplicated here rather
# than imported: this module does not depend on fixup.py's internals,
# same reasoning fixup.py's own module docstring gives for duplicating
# engine.steps.open_job's equivalent helper instead of reusing it.
_ARCHIVE_MARKER_FILES = {".deleted", ".modes"}


def _archive_dir_for_attempt(ledger: Ledger, project: str, attempt_id: str) -> Path | None:
    """The archive directory close already created for this attempt (its
    "archive.done" event), or None if it was never closed yet. Mirrors
    engine.fixup._archive_dir_for_attempt."""
    path: Path | None = None
    for _seq, event in ledger.events(project, "archive.done"):
        if event.attempt_id == attempt_id:
            path = Path(str(event.payload["path"]))
    return path


def _archive_has_content(archive_dir: Path) -> bool:
    """True iff `archive_dir` actually holds a change -- mirrors
    engine.fixup._archive_has_content (see that function's own
    docstring for why an archive that exists but is empty, e.g. an
    attempt that never merged anything into the real repo, must never
    be mistaken for "owner work to restore")."""
    if not archive_dir.exists():
        return False
    deleted_file = archive_dir / ".deleted"
    if deleted_file.exists() and deleted_file.read_text(encoding="utf-8").strip():
        return True
    for path in archive_dir.rglob("*"):
        if path.is_file() and str(path.relative_to(archive_dir)) not in _ARCHIVE_MARKER_FILES:
            return True
    return False


def _overlay_archive_into_copy(config: EngineConfig, attempt_id: str, role: str,
                               archive_dir: Path) -> None:
    """Copies every archived file (an owner's own uncommitted edit to a
    facts/probe/plan/design station's files, made while the job sat
    needs_owner at STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW/STAGE_EARLY_
    STATION/a plain crash) into the FRESH attempt's own `role` copy, as
    plain filesystem writes -- mirrors engine.fixup.
    _overlay_archive_into_builder_copy exactly (see that function's own
    docstring), generalized to any role's copy, not just the builder's.
    Without this, _reopen_for_retry's own close (archive, THEN reset_to_
    head) preserved the owner's edit only in cold storage -- the real
    repo was wiped back to HEAD and the new attempt's copy was archived
    fresh from that same HEAD, so the edit was never actually seen
    again by anything that runs after this."""
    import shutil

    dest_root = copy_root(config, attempt_id, role)

    modes: dict[str, object] = {}
    modes_file = archive_dir / ".modes"
    if modes_file.exists():
        try:
            modes = json.loads(modes_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            modes = {}

    deleted: list[str] = []
    deleted_file = archive_dir / ".deleted"
    if deleted_file.exists():
        deleted = [
            line for line in deleted_file.read_text(encoding="utf-8").splitlines() if line
        ]

    for src in sorted(archive_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(archive_dir)
        if str(rel) in _ARCHIVE_MARKER_FILES:
            continue
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if modes.get(str(rel)) == "100755":
            dest.chmod(dest.stat().st_mode | 0o111)

    for rel in deleted:
        target = dest_root / rel
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)


_RETRY_STAGE_ROLE = {
    "facts": FACTS_ROLE, "probe": PROBER_ROLE, "plan": PLANNER_ROLE, "design": DESIGNER_ROLE,
}


def _reopen_for_retry(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, stale_attempt_id: str,
                      on_progress: ProgressCallback | None,
                      processor_factory: orbit.ProcessorFactory | None,
                      *, retry_stage: str | None = None,
                      intent: JobPosition | None = None) -> tuple[FullOutcome | None, str]:
    """`intent` (the position the retry reruns from, with no attempt yet)
    is recorded before the fresh attempt opens, carrying the archive to
    restore (`restore_from`); an attempt adopted from that intent that
    has no request yet is reused as-is, with the restore redone.

    Closes `stale_attempt_id` (report -> close, same as the design-
    approval resume already does on this shared facts/probe/plan/design
    attempt) and opens a brand-new one for the SAME job. Needed before
    a STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW resume reruns a station whose
    own merge already landed and was committed once on `stale_attempt_id`
    -- engine.steps.merge's own base_head check (_verify_destinations_
    unchanged) refuses a SECOND merge of the same path on the SAME
    attempt, because the repo's HEAD (carrying that first, already-
    committed probe/plan trail) no longer matches that attempt's fixed
    base_head: "someone else's committed change landed there since this
    copy's base" -- true here even though the "someone else" is this
    very job's own earlier station. A fresh attempt's own base_head is
    the CURRENT repo HEAD (already including that earlier commit), so
    the retry's own new merge sees no conflict. Returns (None, the new
    attempt_id) to keep going, or (the failed FullOutcome to return
    right now, "").

    `retry_stage`, when given ("facts"/"probe"/"plan"/"design"), is
    this retry's OWN station -- close's own archive_tmp step already
    copied out every path THAT station's merge had landed uncommitted
    in the real repo (including, e.g., a probe log an owner hand-edited
    while the job sat STAGE_PROBE_RETRY needs_owner) before reset_to_
    head wiped the real repo back to HEAD; _overlay_archive_into_copy
    restores that same content into the fresh attempt's own role copy
    (_RETRY_STAGE_ROLE), so the owner's edit is still there for the
    retry to see. Without this, close's own archive preserved it only
    in cold storage -- the owner's edit never actually reached the new
    attempt at all."""
    # A genuine crash (as opposed to the two pre-existing callers' own
    # terminal "failed"/"done" stop) can leave ONE request for
    # `stale_attempt_id` still "running" -- engine.processor.Processor.
    # run_once treats "running" exactly like "queued" (something still
    # to dispatch), so _close_attempt's own report/close requests,
    # submitted right below, would have run_once sweep that orphaned
    # request up and actually run it FIRST -- silently reconciling (and
    # wasting) a worker call for an attempt this resume is about to
    # discard entirely. A no-op for the two pre-existing callers (their
    # stale attempt's own request is already terminal by the time they
    # call this).
    if (intent is not None and stale_attempt_id
            and ledger.attempt(stale_attempt_id).status == "active"
            and not _attempt_has_requests(ledger, project, stale_attempt_id)):
        restore_from = intent.get("restore_from")
        if retry_stage is not None and isinstance(restore_from, str):
            _overlay_archive_into_copy(config, stale_attempt_id, _RETRY_STAGE_ROLE[retry_stage],
                                       Path(restore_from))
        return None, stale_attempt_id
    if retry_stage in ("probe", "plan", "design"):
        # facts.md is this retry's input, read from the repository: an
        # owner's repair of it (made while the job waited) is promoted
        # into the job trail and checked, instead of being archived away
        # with the stale attempt's own output.
        repo = config.projects[project].repo
        facts_rel = str(facts_path(repo, job_name).relative_to(repo))
        if (repo / facts_rel).is_file() and facts_rel in dirty_paths(repo, ()):
            _commit_paths(repo, [facts_rel], f"gongbang: {job_name} - facts (owner repair)")
            problems, _facts = _checked_facts(repo, job_name)
            if problems:
                return FullOutcome("needs_owner", "facts:" + "; ".join(problems),
                                   stale_attempt_id, None), ""
    _abandon_orphaned_requests(ledger, project, stale_attempt_id)
    close_outcome = _close_attempt(config, ledger, fakes, project, stale_attempt_id, job_name,
                                   on_progress, processor_factory)
    if close_outcome.status != "done":
        return FullOutcome("failed", f"close:{close_outcome.reason}", stale_attempt_id, None), ""
    # Read only AFTER close has run -- close's own archive_tmp/archive_
    # commit steps are what actually create the "archive.done" event
    # this looks up; nothing exists to find before that.
    archive_dir = (_archive_dir_for_attempt(ledger, project, stale_attempt_id)
                   if retry_stage is not None else None)
    restore = (retry_stage is not None and archive_dir is not None
               and _archive_has_content(archive_dir))
    if intent is not None:
        extra = {k: v for k, v in intent.extra.items() if k != "restore_from"}
        if restore:
            extra["restore_from"] = str(archive_dir)
        new_attempt_id = _open_with_intent(config, ledger, project,
                                           replace(intent, extra=extra), EARLY_ROLES)
    else:
        new_attempt_id = open_job(config, ledger, project, job_name, "full", EARLY_ROLES)
    if restore and archive_dir is not None and retry_stage is not None:
        _overlay_archive_into_copy(config, new_attempt_id, _RETRY_STAGE_ROLE[retry_stage],
                                   archive_dir)
    return None, new_attempt_id


def _check_resumed_plan(ledger: Ledger, project: str, job_name: str, repo: Path) -> list[str]:
    """engine.plan.check_resumed_plan(ledger, project, job_name, repo),
    with "plan_never_reviewed" filtered out -- that code can be true
    for a job whose plan was reviewed before the "plan.reviewed" ledger
    event existed at all (an older worktree's own job), so on its own
    it is never a reason to stop a resume. Every OTHER code (plan load/
    validate_plan failures, "plan_changed_since_review", "requirement_
    removed_since_review: X", "task_removed_since_review: X") IS.

    Call sites that already run their own re-review before trusting the
    plan (STAGE_DESIGN_APPROVAL's own _maybe_rerun_plan_review) call
    this AFTER that re-review, never before: checking first would
    wrongly stop a resume on "plan_changed_since_review" for an edit
    the re-review is about to re-approve anyway (_resume_design_approval)."""
    from engine import plan as plan_mod

    return [p for p in plan_mod.check_resumed_plan(ledger, project, job_name, repo)
            if p != "plan_never_reviewed"]


def _stop_for_resumed_plan_problems(ledger: Ledger, project: str, job_name: str,
                                    attempt_id: str, repo: Path) -> FullOutcome | None:
    """None iff _check_resumed_plan finds nothing wrong -- the caller
    keeps going. Otherwise returns the needs_owner FullOutcome to
    return right now, WITHOUT recording a new position: a plan.json/
    scene edit made (or a requirement/task silently dropped) while this
    job sat needs_owner is caught before ANY stage reruns work against
    it, and the position that describes the real stop (e.g. a
    STAGE_TASK_OWNER stop with its remaining_task_ids) stays the newest,
    so the next `gb2 continue`, once the edit is fixed, resumes exactly
    where the job was."""
    plan_problems = _check_resumed_plan(ledger, project, job_name, repo)
    if not plan_problems:
        return None
    reason = ("resumed_plan_invalid:" + "; ".join(plan_problems))[:300]
    return FullOutcome("needs_owner", reason, attempt_id, None)


def pending_release_progress(ledger: Ledger, project: str,
                             job_name: str) -> dict[str, object] | None:
    """The release-approval stop this job is waiting on, if that is where
    it is (read by `gb2 resolve` for the report_path)."""
    pos = load_position(ledger, project, job_name)
    if pos is None or pos.stage != STAGE_RELEASE_APPROVAL:
        return None
    return {"stage": pos.stage, "attempt_id": pos.attempt_id, **pos.extra}


def continue_full_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, *, on_progress: ProgressCallback | None = None,
                      processor_factory: orbit.ProcessorFactory | None = None) -> FullOutcome:
    """engine.position.resume_job under the name tools/gb2 calls."""
    return resume_job(config, ledger, fakes, project, job_name, on_progress=on_progress,
                      processor_factory=processor_factory)


# ---------------------------------------------------------------------
# resume handlers -- one per position stage, dispatched by
# engine.position.resume_job through RESUME_TABLE
# ---------------------------------------------------------------------

def _max_fix_rounds(pos: JobPosition) -> int:
    return pos.max_fix_rounds


def _task_for(plan: Plan, task_id: str, fixing: bool) -> Task:
    """The Task a position names: an acceptance-fix round's synthesized
    task (owning every plan task's paths), or the plan's own task."""
    if fixing:
        all_owns = tuple(sorted({entry for t in plan.tasks for entry in t.owns}))
        return Task(id=task_id, title="수락 실패 수정", detail="", owns=all_owns, depends_on=())
    by_id = {t.id: t for t in plan.tasks}
    task = by_id.get(task_id)
    if task is None:
        raise FullOrbitError(f"unknown task id for resume: {task_id!r}")
    return task


_BEFORE_PLAN_STAGES = frozenset({
    STAGE_FACTS, STAGE_PROBE, STAGE_PLAN, STAGE_PROBE_RETRY, STAGE_PLAN_REVIEW, STAGE_SMALL_PLAN,
})


def resumed_plan_stop(run: Run, pos: JobPosition) -> FullOutcome | None:
    """The check every resume runs before dispatching, from the point the
    job has a committed plan on: the plan must still be the reviewed one,
    with no requirement or task dropped (_stop_for_resumed_plan_problems).
    Stages before the plan exists have nothing to check; a design-approval
    resume re-reviews an edited plan first and checks after that
    (_resume_design_approval)."""
    if pos.stage in _BEFORE_PLAN_STAGES or pos.stage == STAGE_DESIGN_APPROVAL:
        return None
    if pos.stage == STAGE_EARLY_STATION and pos.get("station") != "design":
        return None
    return _stop_for_resumed_plan_problems(run.ledger, run.project, run.job,
                                           pos.attempt_id or "", run.repo)


def _resumed_plan(run: Run, pos: JobPosition) -> tuple[FullOutcome | None, Plan | None]:
    """The committed plan, or the failed outcome when it cannot be
    loaded. The plan was already checked against its review before the
    resume dispatched (resumed_plan_stop)."""
    try:
        plan = load_plan(run.repo, run.job)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", pos.attempt_id, None), None
    return None, plan


def _advance_past_task(run: Run, pos: JobPosition, plan: Plan,
                       attempt_id: str | None) -> FullOutcome:
    """The task `pos` names has finished on `attempt_id`: continue with the
    acceptance-fix loop (an acceptance-fix round) or the tasks still
    queued behind it."""
    if pos.fixing:
        return _run_acceptance_and_handoff(run.config, run.ledger, run.fakes, run.project,
                                           run.job, plan, _max_fix_rounds(pos), run.on_progress,
                                           run.processor_factory, attempt_id)
    by_id = {t.id: t for t in plan.tasks}
    remaining = [by_id[i] for i in pos.remaining_task_ids[1:] if i in by_id]
    return _finish_from_tasks(run.config, run.ledger, run.fakes, run.project, run.job, plan,
                              remaining, _max_fix_rounds(pos), run.on_progress,
                              run.processor_factory, attempt_id)


_STATION_STAGE = {
    "facts": STAGE_FACTS, "probe": STAGE_PROBE, "plan": STAGE_PLAN, "design": STAGE_DESIGN,
}

_REOPEN_STATION = {
    STAGE_FACTS: "facts", STAGE_PROBE: "probe", STAGE_PLAN: "plan", STAGE_DESIGN: "design",
    STAGE_PROBE_RETRY: "probe", STAGE_PLAN_REVIEW: "plan",
}


def _resume_reopened_station(run: Run, pos: JobPosition) -> FullOutcome:
    """A facts/probe/plan/design station that crashed, failed, or stopped
    on its own content (probe failure, plan review) reruns on a FRESH
    attempt: the stale one is closed (its merged work archived and
    overlaid into the new copy, see _reopen_for_retry) because merge's
    own base_head check refuses a second merge of a path the job already
    committed on that attempt. Every early-station role gets a copy, so
    a small job (planner copy only) resumes here as a full job."""
    station = _REOPEN_STATION[pos.stage]
    request = str(pos.get("request", ""))
    intent = JobPosition(job=run.job, stage=_STATION_STAGE[station], attempt_id=None,
                         extra={k: v for k, v in pos.extra.items()
                                if k in ("request", "probe_text", "max_fix_rounds",
                                         "restore_from")})
    failed, attempt_id = _reopen_for_retry(run.config, run.ledger, run.fakes, run.project,
                                           run.job, pos.attempt_id or "", run.on_progress,
                                           run.processor_factory, retry_stage=station,
                                           intent=intent)
    if failed is not None:
        return failed
    if station == "facts":
        return _facts_then_rest(run, attempt_id, request, _max_fix_rounds(pos))
    return _probe_plan_design_close(run.config, run.ledger, run.fakes, run.project, run.job,
                                    attempt_id, request, _max_fix_rounds(pos), run.on_progress,
                                    run.processor_factory, start=station,
                                    probe_text=str(pos.get("probe_text", "")))


def _resume_early_station(run: Run, pos: JobPosition) -> FullOutcome:
    """A facts/probe/plan/design run_station stop reruns that station on
    the SAME attempt once every owner request it made is fulfilled, with
    a continuation note when there were requests (the earlier run_station
    request is already "done", so an unchanged prompt would never run)."""
    if outstanding_requests(run.ledger, run.project, pos, repo=run.repo):
        return FullOutcome("needs_owner", "owner_request", pos.attempt_id, None)
    station = str(pos.get("station", ""))
    request = str(pos.get("request", ""))
    attempt_id = pos.attempt_id or ""
    needs_note = bool(pos.pending_requests)
    if station == "facts":
        return _facts_then_rest(run, attempt_id, request, _max_fix_rounds(pos),
                                needs_resume_note=needs_note)
    if station not in ("probe", "plan", "design"):
        raise FullOrbitError(f"unknown early station for resume: {station!r}")
    return _probe_plan_design_close(run.config, run.ledger, run.fakes, run.project, run.job,
                                    attempt_id, request, _max_fix_rounds(pos), run.on_progress,
                                    run.processor_factory, start=station,
                                    probe_text=str(pos.get("probe_text", "")),
                                    needs_resume_note=needs_note)


def _resume_small_plan(run: Run, pos: JobPosition) -> FullOutcome:
    from engine import small_orbit

    return small_orbit.resume_small_plan(run.config, run.ledger, run.fakes, run.project,
                                         run.job, pos, on_progress=run.on_progress,
                                         processor_factory=run.processor_factory)


def _resume_design_approval(run: Run, pos: JobPosition) -> FullOutcome:
    """Re-review an owner edit to plan.json first (only then is the plan
    checked against its review), then wait for the approval, then close
    the shared early attempt and run the tasks."""
    attempt_id = pos.attempt_id or ""
    try:
        plan = load_plan(run.repo, run.job)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None)
    stale = _maybe_rerun_plan_review(run.config, run.ledger, run.fakes, run.project, run.job,
                                     attempt_id, plan, run.on_progress, run.processor_factory)
    if stale is not None:
        return stale
    stop = _stop_for_resumed_plan_problems(run.ledger, run.project, run.job, attempt_id,
                                           run.repo)
    if stop is not None:
        return stop
    if not check_design_approved(run.ledger, run.project, run.job):
        return FullOutcome("needs_owner", "design_approval", attempt_id, None)
    return _close_early_attempt_then_tasks(run, attempt_id, plan, _max_fix_rounds(pos))


def _resume_early_close(run: Run, pos: JobPosition) -> FullOutcome:
    stop, plan = _resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    return _close_early_attempt_then_tasks(run, pos.attempt_id or "", plan,
                                           _max_fix_rounds(pos), record=False)


def _resume_acceptance(run: Run, pos: JobPosition) -> FullOutcome:
    """post_tasks (a crash or failure anywhere in acceptance -> handoff ->
    security -> release) and a requirements-review stop both rerun
    acceptance fresh: the owner may have changed the plan or the app."""
    stop, plan = _resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    return _run_acceptance_and_handoff(run.config, run.ledger, run.fakes, run.project, run.job,
                                       plan, _max_fix_rounds(pos), run.on_progress,
                                       run.processor_factory, pos.attempt_id)


def _resume_security_fix(run: Run, pos: JobPosition) -> FullOutcome:
    """Rerun only the security check, unless the source acceptance and
    the handoff certified (its content fingerprint, which also sees
    uncommitted edits) has changed since -- then acceptance reruns too."""
    stop, plan = _resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    from engine import evidence

    certified = pos.get("certified_fingerprint")
    current = evidence.fingerprint(run.repo, run.config.projects[run.project].excludes)
    if certified and current != certified:
        return _resume_acceptance(run, pos)
    return _finish_after_handoff(run.config, run.ledger, run.project, run.job, plan,
                                 pos.attempt_id or "", Path(str(pos.get("report_path", ""))),
                                 _max_fix_rounds(pos))


def _resume_inflight_publish(run: Run, attempt_id: str, request_id: str) -> None:
    """An approved publish request left "running" (its process died
    mid-journal, e.g. right after call_publisher was journaled started)
    is resumed as that same request: the processor's own resume rule and
    the publisher's receipt reconcile decide whether anything is
    dispatched again. A request another live processor holds is left
    alone; finalize_release then keeps waiting."""
    from engine.processor import ProcessorBusy

    if run.ledger.request(request_id).status != "running":
        return
    factory = run.processor_factory or orbit._default_processor_factory
    processor = factory(run.config, run.ledger, run.fakes_for(attempt_id))
    try:
        processor.run_request(run.project, request_id)
    except ProcessorBusy:
        return


def _resume_release_approval(run: Run, pos: JobPosition) -> FullOutcome:
    """finalize_release no-ops while the publish request is still
    queued/running, and finishes report -> close -> decision once it is
    terminal (including after a crash in between)."""
    stop, _plan = _resumed_plan(run, pos)
    if stop is not None:
        return stop
    if not pos.get("request_id"):
        return _prepare_release(run.ledger, run.config, run.project, pos)
    _resume_inflight_publish(run, pos.attempt_id or "", str(pos.get("request_id")))
    return finalize_release(run.config, run.ledger, run.fakes, run.project,
                            pos.attempt_id or "", run.job, str(pos.get("request_id", "")),
                            str(pos.get("report_path", "")), run.on_progress,
                            run.processor_factory)


def _resume_task(run: Run, pos: JobPosition) -> FullOutcome:
    """A task position with no stop recorded: the task crashed, or its
    attempt ended at an ordinary "failed". A closed attempt either
    committed (the task finished -- advance) or was closed without
    committing (abandoned, or closed for a repair that never opened its
    replacement -- rerun the task from scratch on a new attempt). An
    active attempt reruns its build on the same attempt; the idempotent
    request ids skip whatever already finished."""
    stop, plan = _resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    attempt_id = pos.attempt_id or ""
    if attempt_id and run.ledger.attempt(attempt_id).status in ("closed", "abandoned"):
        if _task_attempt_committed(run.ledger, run.project, attempt_id):
            return _advance_past_task(run, pos, plan, attempt_id)
        return _rerun_task_on_new_attempt(run, pos, plan)
    return _rerun_task_owner_stop(run, pos, plan, crash=True)


def _rerun_task_on_new_attempt(run: Run, pos: JobPosition, plan: Plan) -> FullOutcome:
    task = _task_for(plan, pos.task_id or "", pos.fixing)
    task_text = str(pos.get("task_text", ""))
    base_task_text = str(pos.get("base_task_text", task_text))
    outcome = _run_task_attempt(run.config, run.ledger, run.fakes, run.project, run.job, task,
                                task_text, plan, run.on_progress, run.processor_factory,
                                base_task_text, fixing=pos.fixing,
                                remaining_task_ids=pos.remaining_task_ids,
                                max_fix_rounds=_max_fix_rounds(pos))
    full = _station_outcome_to_full(outcome, "build")
    if full is not None:
        return full
    return _advance_past_task(run, pos, plan, outcome.attempt_id)


def _resume_task_owner(run: Run, pos: JobPosition) -> FullOutcome:
    stop, plan = _resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    return _rerun_task_owner_stop(run, pos, plan, crash=False)


def _rerun_task_owner_stop(run: Run, pos: JobPosition, plan: Plan, *,
                           crash: bool) -> FullOutcome:
    """Continues a build task on its SAME attempt/copy:
    - a required-context gap rebuilds and rechecks the context first;
    - a crash submits the task's own stage list again (_task_stages):
      every request that already finished reconciles by its id;
    - worker_stuck reruns the build from scratch;
    - a fulfilled owner request reruns run_station with a note that the
      install is done and the already-changed files to read first.
    An owner request that is still outstanding stays needs_owner."""
    if outstanding_requests(run.ledger, run.project, pos, repo=run.repo):
        return FullOutcome("needs_owner", "owner_request", pos.attempt_id, None)
    attempt_id = pos.attempt_id or ""
    base_task_text = str(pos.get("base_task_text", pos.get("task_text", "")))
    task_text = str(pos.get("task_text", base_task_text))
    task = _task_for(plan, pos.task_id or "", pos.fixing)
    run_config = task_attempt_config(run.config, run.project, run.job, plan)
    spec = pos.get("stages")
    if not isinstance(spec, list):
        # A position recorded before stage specs were: rebuild the same list.
        spec = stages_spec(_task_stages(run.config, run_config, run.ledger, run.project, run.job,
                                        plan, task, task_text, base_task_text, attempt_id))
    if crash:
        stages = stages_from_spec(spec, run_config, attempt_id)
        return _continue_task_stages(run, pos, plan, run_config, stages, task_text)
    if pos.get("context_gap"):
        gap_sc = _build_station_context(run.config, run.ledger, run.project, attempt_id, "build",
                                        base_task_text, _planned_skills(run.config, plan, "build"))
        if gap_sc.unmet_required():
            reason = _unmet_context_reason(gap_sc)
            record_position(run.ledger, run.project, task_stop_position(
                pos, attempt_id, reason, (), context_gap=True))
            return FullOutcome("needs_owner", reason, attempt_id, None)
        task_text = f"{base_task_text}\n\n{gap_sc.text}" if gap_sc.text else base_task_text
        run_station_text = task_text
    elif pos.stop_reason == "worker_stuck":
        run_station_text = task_text
    else:
        build_copy = copy_root(run_config, attempt_id, BUILDER_ROLE)
        run_station_text = _install_followup_task_text(task_text, build_copy)
    merge_retry_n = sum(1 for _seq, event in run.ledger.events(run.project, "merge.recorded")
                        if event.attempt_id == attempt_id)
    stages = stages_from_spec(spec, run_config, attempt_id, task_text=run_station_text,
                              merge_retry_n=merge_retry_n)
    return _continue_task_stages(run, pos, plan, run_config, stages, task_text)


def _continue_task_stages(run: Run, pos: JobPosition, plan: Plan, run_config: EngineConfig,
                          stages: list[orbit.Stage], task_text: str) -> FullOutcome:
    """Runs a resumed task's stages on its own attempt, records a new
    owner stop if it makes one, and otherwise moves past the task."""
    attempt_id = pos.attempt_id or ""
    outcome = orbit.continue_job(run_config, run.ledger, run.fakes_for(attempt_id), run.project,
                                 attempt_id, stages, processor_factory=run.processor_factory,
                                 on_progress=run.on_progress)
    if _stops_for_owner(outcome):
        task_pos = replace(pos, extra={**{k: v for k, v in pos.extra.items()
                                          if k != "context_gap"}, "task_text": task_text})
        _record_task_stop(run.ledger, run.project, task_pos, outcome)
    full = _station_outcome_to_full(outcome, "build")
    if full is not None:
        return full
    return _advance_past_task(run, pos, plan, attempt_id)


RESUME_TABLE: dict[str, Callable[[Run, JobPosition], FullOutcome]] = {
    STAGE_FACTS: _resume_reopened_station,
    STAGE_PROBE: _resume_reopened_station,
    STAGE_PLAN: _resume_reopened_station,
    STAGE_DESIGN: _resume_reopened_station,
    STAGE_PROBE_RETRY: _resume_reopened_station,
    STAGE_PLAN_REVIEW: _resume_reopened_station,
    STAGE_EARLY_STATION: _resume_early_station,
    STAGE_SMALL_PLAN: _resume_small_plan,
    STAGE_DESIGN_APPROVAL: _resume_design_approval,
    STAGE_EARLY_CLOSE: _resume_early_close,
    STAGE_TASK: _resume_task,
    STAGE_TASK_OWNER: _resume_task_owner,
    STAGE_POST_TASKS: _resume_acceptance,
    STAGE_REQUIREMENTS_REVIEW: _resume_acceptance,
    STAGE_SECURITY_FIX: _resume_security_fix,
    STAGE_RELEASE_APPROVAL: _resume_release_approval,
}


__all__ = [
    "FullOutcome", "FullOrbitError", "Run", "run_full_job", "continue_full_job",
    "record_release_decision", "finalize_release", "pending_owner_requests",
    "pending_release_progress", "RESUME_TABLE",
    "FACTS_ROLE", "PROBER_ROLE", "PLANNER_ROLE", "DESIGNER_ROLE", "BUILDER_ROLE",
    "REVIEWER_ROLE",
    "STAGE_DESIGN_APPROVAL", "STAGE_TASK_OWNER", "STAGE_PROBE_RETRY", "STAGE_PLAN_REVIEW",
    "STAGE_REQUIREMENTS_REVIEW", "STAGE_SECURITY_FIX", "STAGE_RELEASE_APPROVAL",
    "STAGE_EARLY_STATION",
]
