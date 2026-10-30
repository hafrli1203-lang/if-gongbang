"""운영 방침: 사람 승인으로 멈추는 자리를 없앤다 (배포·외부 패키지 포함).
`gb2 new`/`gb2 change`/`gb2 continue`/
`gb2 fix`가 사람 입력 없이 처음부터 배포까지 끝까지 가도록, 이 모듈이
engine.application.job_state의 STAGE_* 멈춤 중 아래 자리에서 "운영자가
승인했을 때 하던 일"을 즉시 자동으로 수행하고 engine.application.pipeline.
continue_full_job으로 이어서 진행한다. 장부에는 매번 confirm_source="auto"
(또는 그 사실을 담은 별도 이벤트)를 남겨 감사 추적을 유지한다 -- 승인
행위 자체를 기록 없이 건너뛰지 않는다.

자동으로 처리하는 멈춤 (운영 방침이 정한 범위, 그 밖은 그대로 멈춘다):
  - STAGE_DESIGN_APPROVAL (design_approval / design_approval_stale)
      -> tools/gb2lib/approve_ops.cmd_approve_design이 TTY로 하던 것과
         똑같이 "design.approved" 이벤트를 confirm_source="auto"로 기록.
  - STAGE_TASK_OWNER / STAGE_EARLY_STATION, reason "owner_request"
      -> install/scripts_change/test_config_change는 tools/gb2lib/
         approve_ops.cmd_approve_install과 똑같이 owner_approval.
         approve_pending(confirm_source="auto") + 드레인; kind="edit"
         (OWNS_SCOPE_REQUEST_RULE) 요청은 `gb2 fix --also-own`과 똑같이
         repair_approval.approve_pending_edit로 전체 경로를 자동 승인.
  - STAGE_PLAN_REVIEW 라운드 상한 도달 (repair.FIX_ROUNDS_EXHAUSTED_PREFIX/
      NO_PROGRESS_PREFIX) -- "엔진이 멈추고 반장 직접"
      (ENGINE.md 완성 원칙): plan.review_capped 이벤트 + .gongbang/<job>/
      foreman-handoff.md를 남기고 "failed"로 끝낸다(plan_review_cap_stop).
      예전 "--past-cap 1회 뒤 강제 통과"는 시도마다 다시 1회를 허용해
      끝나지 않았고, 3회 거절된 계획을 통과시키는 것 자체가 운영 방침과
      어긋났다. 상한에 걸리지 않은 보통의 "plan_review_failed"는
      `_resume_reopened_station`이 "plan" 역을 새로 재시도하므로 이
      모듈은 continue_full_job만 다시 부른다.
  - STAGE_RELEASE_APPROVAL (release_approval) -- "배포는 config
      publisher가 설정돼 있으면 바로 실행": project.release가 설정된
      경우 tools/gb2lib/release_ops.cmd_ship과 똑같이
      engine.application.release.approve_and_run을 "승인 <request_id>"
      문구로 직접 호출. publisher가 설정되지 않은 프로젝트는 배포할
      대상이 없으므로 그대로 멈춘다 (승인 문제가 아니라 설정 문제).

이 목록 밖의 멈춤은 그대로 멈춘다: probe 실패(probe_targets_invalid/
probe_failed, STAGE_PROBE_RETRY)는 사실을 지어낼 수 없는 외부 호출
실패라서 자동 처리 목록에도 없고, 필수 컨텍스트/스킬 누락
(ContextGate.unmet), requirements_uncovered, 빌드 과제 자체의 fix
라운드 소진(worker_stuck/fix_rounds_exhausted, engine.application.repair.
fix가 FixupError로 올리는 것)은 엔진이 대신 풀 수 없는 환경/엔지니어링
문제라서 손대지 않는다 -- 멈출 때도 `tools/gb2lib/runner.py`의 `next`
안내 메시지는 그대로 유지된다(이 모듈이 그 경로를 전혀 건드리지
않으므로).

보안 점검 실패(awaiting_security_fix)는 이 모듈이 하는 일이 없다:
engine.application.pipeline_tasks.finish_after_handoff 자체가 이제
실패를 기록("security.auto_passed", confirm_source="auto")하고도
멈추지 않고 release 단계로 바로 넘어가므로, 이 모듈이 다룰 needs_owner
가 애초에 발생하지 않는다."""
from __future__ import annotations

from engine.config import EngineConfig
from engine.domain.records import Event
from engine.application.pipeline_base import FullOutcome, fakes_for as _resolve_fakes
from engine.application.job_state import (
    STAGE_DESIGN_APPROVAL, STAGE_EARLY_STATION, STAGE_PLAN_REVIEW, STAGE_RELEASE_APPROVAL,
    STAGE_SMALL_PLAN, STAGE_TASK_OWNER, JobPosition, load_position,
)
from engine.application.repair import FIX_ROUNDS_EXHAUSTED_PREFIX, NO_PROGRESS_PREFIX, OWNS_SCOPE_STUCK_PREFIX

AUTO_CONFIRM_SOURCE = "auto"
# A generous but finite ceiling on how many auto-actions one auto_settle
# call may chain -- never an infinite loop even if something this module
# does not recognize keeps reappearing after each resume (it would just
# stop acting and return that stop, see the main loop below).
_MAX_ITERATIONS = 50


def auto_settle(config: EngineConfig, ledger, fakes, project: str, job_name: str,
                outcome: "FullOutcome | object", *,
                on_progress=None, processor_factory=None) -> "FullOutcome | object":
    """`outcome`, unchanged (same object), iff it is not a "needs_owner"
    stop this module recognizes -- a caller (tools/gb2lib) can tell
    nothing happened with `result is outcome`. Otherwise performs
    whatever auto-action the current position calls for, resumes via
    engine.application.pipeline.continue_full_job, and repeats on the
    new outcome -- up to `_MAX_ITERATIONS` times, or until the job
    reaches "done"/"failed", or until a needs_owner stop outside this
    module's own list (see module docstring) is reached, whichever
    comes first. `outcome` may be a FullOutcome OR a SmallOutcome
    (engine.application.quick_pipeline) -- both carry the same
    `status`/`reason`/`attempt_id` fields this function reads; the
    moment this module actually acts, it resumes through
    continue_full_job, whose own result is always a FullOutcome from
    then on (job_state.resume_job's RESUME_TABLE dispatch, which also
    already covers a small job's own STAGE_SMALL_PLAN)."""
    from engine.application.pipeline import continue_full_job

    current = outcome
    retried_plan_attempts: set[str] = set()
    for _ in range(_MAX_ITERATIONS):
        status = getattr(current, "status", None)
        # a build whose review or
        # bench did not pass used to end the whole run "failed" and wait
        # for a hand-typed `gb2 fix`. 완성 원칙 says: fix, re-review, at
        # most max_fix_rounds, THEN stop for the foreman -- so the repair
        # round runs here, automatically.
        if status == "failed" and _is_build_review_or_bench_failure(getattr(current, "reason", None)):
            repaired = _auto_repair(config, ledger, fakes, project, job_name, current,
                                    on_progress, processor_factory)
            if repaired is None:
                return current
            current = repaired
            continue
        if status != "needs_owner":
            return current
        # a small job that fell back to its full twin ("<job>-full") during a
        # RESUME left this loop reading the ORIGINAL job's position (stage
        # small_plan) and re-opening planning on top of the open full attempt
        # -> ProjectOpenError. Follow the project's current attempt instead.
        job_name = _follow_fallback(ledger, project, job_name)
        pos = load_position(ledger, project, job_name)
        if pos is None:
            return current
        reason = current.reason or ""
        next_outcome = _advance(config, ledger, fakes, project, job_name, pos, reason,
                                retried_plan_attempts, on_progress, processor_factory)
        if next_outcome is None:
            return current
        current = next_outcome
    return current


def _resume(config, ledger, fakes, project, job_name, *, on_progress, processor_factory,
           past_cap: bool = False) -> FullOutcome:
    from engine.application.pipeline import continue_full_job

    return continue_full_job(config, ledger, fakes, project, job_name, on_progress=on_progress,
                             processor_factory=processor_factory, past_cap=past_cap)


def _advance(config: EngineConfig, ledger, fakes, project: str, job_name: str,
            pos: JobPosition, reason: str, retried_plan_attempts: set[str],
            on_progress, processor_factory) -> "FullOutcome | None":
    """The next outcome once this module's own auto-action (if any) for
    `pos`/`reason` has run, or None iff nothing here applies (the
    caller's loop stops and hands back the stop as-is)."""
    if pos.stage == STAGE_PLAN_REVIEW:
        if reason.startswith(NO_PROGRESS_PREFIX) or reason.startswith(FIX_ROUNDS_EXHAUSTED_PREFIX):
            # Round cap reached: stop for the foreman (ENGINE.md 완성 원칙
            # "3회째는 엔진이 멈추고 반장 직접 구현"). The previous "--past-cap
            # once, then force-pass" rule was keyed per attempt, so every
            # new attempt earned one more past-cap round and the loop
            # never ended (live job,: a 4th plan attempt
            # after 3 rejections), and a force-passed plan that the
            # reviewer rejected three times is exactly what the owner
            # said must not happen. `retried_plan_attempts` is kept only
            # for the signature.
            del retried_plan_attempts
            return plan_review_cap_stop(config, ledger, project, job_name, pos, reason)
        # A plain (uncapped) plan_review_failed/facts_changed_after_check
        # stop: _resume_reopened_station already reopens "plan" fresh on
        # its own, same round-cap check guarding it either way -- no
        # owner action is needed here at all, just resume again.
        return _resume(config, ledger, fakes, project, job_name,
                      on_progress=on_progress, processor_factory=processor_factory)

    if pos.stage == STAGE_DESIGN_APPROVAL and reason in ("design_approval",
                                                         "design_approval_stale"):
        if not _auto_approve_design(ledger, project, job_name, pos.attempt_id or "",
                                     config.projects[project].repo):
            return None
        return _resume(config, ledger, fakes, project, job_name,
                       on_progress=on_progress, processor_factory=processor_factory)

    if pos.stage in (STAGE_TASK_OWNER, STAGE_EARLY_STATION, STAGE_SMALL_PLAN) \
            and reason == "owner_request":
        return _auto_resolve_owner_requests(config, ledger, fakes, project, job_name, pos,
                                            on_progress, processor_factory)

    if pos.stage == STAGE_SMALL_PLAN and reason == "plan_review_failed":
        # A small job's combined facts+plan+review call has no round cap
        # of its own, so the cap lives HERE: the same max_fix_rounds
        # (default 2) as a full job
        # -- after that many rejected planning attempts the engine stops
        # for the foreman instead of reopening planning forever.
        attempt_id = pos.attempt_id or ""
        retried_plan_attempts.add(attempt_id)
        cap = getattr(pos, "max_fix_rounds", None) or 2
        if len(retried_plan_attempts) > cap:
            return plan_review_cap_stop(config, ledger, project, job_name, pos,
                                        f"{FIX_ROUNDS_EXHAUSTED_PREFIX}{len(retried_plan_attempts) - 1}")
        return _resume(config, ledger, fakes, project, job_name,
                       on_progress=on_progress, processor_factory=processor_factory)

    if pos.stage == STAGE_RELEASE_APPROVAL and reason == "release_approval":
        if not _auto_approve_release(config, ledger, fakes, project, job_name, pos):
            return None
        return _resume(config, ledger, fakes, project, job_name,
                       on_progress=on_progress, processor_factory=processor_factory)

    return None


def _auto_approve_design(ledger, project: str, job_name: str, attempt_id: str, repo) -> bool:
    """The `gb2 approve-design` TTY line's own effect, done without ever
    reading stdin: appends `design.approved` (confirm_source="auto")
    naming the SAME pinned target_version check_design_approved/
    design_approval_still_matches already read before this -- see
    tools/gb2lib/approve_ops.cmd_approve_design's own docstring, or
    engine.application.standing_approval.design_standing_approved for
    the unconditional-policy twin of this exact pattern. False (nothing
    appended) only if the design artifact itself cannot be read --
    never fabricated, same as every other caller of this check."""
    from engine.application.office_approval import DesignArtifactUnreadable, design_target_version

    try:
        target_version = design_target_version(repo, job_name)
    except DesignArtifactUnreadable:
        return False
    ledger.append(Event(
        project=project, kind="design.approved", actor_kind="processor",
        actor_id="auto_pilot", attempt_id=attempt_id,
        payload={"job": job_name, "confirm_source": AUTO_CONFIRM_SOURCE,
                 "target_version": target_version},
    ))
    return True


def _auto_resolve_owner_requests(config: EngineConfig, ledger, fakes, project: str,
                                 job_name: str, pos: JobPosition, on_progress,
                                 processor_factory) -> "FullOutcome | None":
    """Every genuinely pending owner request this job is stopped on
    (STAGE_TASK_OWNER or STAGE_EARLY_STATION, reason "owner_request"),
    approved the same way `gb2 approve-install`/`gb2 fix --also-own`
    would: install/scripts_change/test_config_change through
    owner_approval.approve_pending(confirm_source="auto") + the
    existing processor drain (engine.interface.cli._drain), and any
    "edit" (OWNS_SCOPE_REQUEST_RULE) report through repair_approval.
    approve_pending_edit, which grants every one of its own pending
    paths and resumes the job itself in the SAME call. None iff there
    is nothing actually pending any more (a resume raced this exact
    check -- the caller's own loop then sees the position has moved on
    and stops safely rather than looping on nothing)."""
    from engine.application.job_state_requests import pending_owner_requests
    from engine.application.office_approval import pinned_install_batch
    from engine.application.owner_approval import approve_pending
    from engine.application.repair_approval import approve_pending_edit
    from engine.application.repair_target_owns import is_owns_scope_request
    from engine.interface.cli import _drain

    repo = config.projects[project].repo
    pending = pending_owner_requests(ledger, project, job_name, repo=repo)
    if not pending:
        return None
    edit_requests = [r for r in pending if is_owns_scope_request(r)]
    others = [r for r in pending if not is_owns_scope_request(r)]
    attempt_id = pos.attempt_id or ""
    if others:
        batch = pinned_install_batch(ledger, project, job_name, others)
        for entry in batch:
            # live (marketing-chat lint-setup-3): a worker emitted
            # kind-less "requests" ({description, blocking}) -- approve_pending
            # raised ValueError(kind_not_whitelisted) and killed the driver.
            # A request that cannot be approved stays for the foreman.
            try:
                request_id = approve_pending(
                    config, ledger, project, attempt_id, entry["request"],
                    confirm=True, confirm_source=AUTO_CONFIRM_SOURCE, proposal=entry["proposal"],
                )
            except ValueError as exc:
                ledger.append(Event(
                    project=project, kind="auto_pilot.request_refused", actor_kind="processor",
                    actor_id="auto_pilot", attempt_id=attempt_id,
                    payload={"job": job_name, "reason": str(exc),
                             "request": {k: str(v)[:200] for k, v in dict(entry["request"]).items()}},
                ))
                return None
            _drain(config, ledger, project, attempt_id, request_id)
    if edit_requests:
        paths = tuple(str(r["path"]) for r in edit_requests)

        def fakes_for_fn(a_id: str):
            return _resolve_fakes(fakes, a_id)

        # Observed on a live job (an edit request for eslint.config.js
        # outside the builder prefixes): approve_pending_edit's own refusals used to
        # escape as FixupError and kill the driver. A refusal means "this
        # stop stays for the owner/foreman" -- hand the stop back intact.
        from engine.application.repair_target import FixupError
        try:
            return approve_pending_edit(config, ledger, project, paths, fakes_for_fn,
                                        on_progress=on_progress, processor_factory=processor_factory)
        except FixupError as exc:
            ledger.append(Event(
                project=project, kind="auto_pilot.edit_refused", actor_kind="processor",
                actor_id="auto_pilot", attempt_id=attempt_id,
                payload={"job": job_name, "paths": list(paths), "reason": str(exc)},
            ))
            return None
    return _resume(config, ledger, fakes, project, job_name,
                  on_progress=on_progress, processor_factory=processor_factory)


def plan_review_cap_stop(config: EngineConfig, ledger, project: str, job_name: str,
                         pos: JobPosition, reason: str) -> FullOutcome:
    """The engine's final word at the plan review cap: a `plan.review_
    capped` ledger event (rounds, the last findings), a foreman note at
    .gongbang/<job>/foreman-handoff.md saying what was rejected and how
    to take over (`gb2 approve-plan-edit <app> --reason ...` after a hand
    edit of plan.json, then `gb2 continue`), and a "failed" outcome --
    never another planner round, never a forced pass."""
    from engine.application.repair_target import review_findings_for_attempt

    attempt_id = pos.attempt_id or ""
    findings = review_findings_for_attempt(ledger, project, attempt_id)
    ledger.append(Event(
        project=project, kind="plan.review_capped", actor_kind="processor",
        actor_id="auto_pilot", attempt_id=attempt_id,
        payload={"job": job_name, "stop_reason": reason, "findings": findings,
                 "next": "foreman"},
    ))
    repo = config.projects[project].repo
    note = repo / ".gongbang" / job_name / "foreman-handoff.md"
    lines = [
        f"# 반장 인계 — 계획 검토 상한 ({reason})", "",
        f"- 작업: {job_name} / 시도: {attempt_id}",
        "- 엔진은 여기서 멈췄다. 계획자를 다시 돌리지 않고 반장이 직접 맡는다(완성 원칙).", "",
        "## 마지막 검토 지적", "",
    ]
    for item in findings:
        if isinstance(item, dict):
            lines.append(f"- {item.get('severity')} {item.get('file')}:{item.get('line')} — {item.get('note')}")
    lines += ["", "## 다음", "",
              "1. plan.json(과 tests/scenes/)을 손으로 고친다.",
              f"2. `gb2 approve-plan-edit {project} --reason \"<사유>\"` — 반장 승인이 검토를 대신한다.",
              f"3. `gb2 continue {project}` — 계획 단계를 건너뛰고 다음 칸부터 이어 간다.", ""]
    try:
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass
    return FullOutcome("failed", f"plan_review_capped:{reason}", attempt_id, None)


def _auto_approve_release(config: EngineConfig, ledger, fakes, project: str, job_name: str,
                          pos: JobPosition) -> bool:
    """The `gb2 ship` TTY approval line's own effect, synthesized rather
    than typed: the exact "승인 <request_id>" text release.approve_and_
    run's own parse_owner_message checks for, built here from the
    request_id this job's own prepare_release already pinned on its
    position (never a secret -- a plain request id string). False
    (nothing done) when this project has no `release` configured at
    all (nothing to ship to -- a config gap, not an approval gap, so
    the caller leaves the stop as-is) or no request_id is recorded yet
    (a resume raced prepare_release itself)."""
    if config.projects[project].release is None:
        return False
    request_id = pos.get("request_id")
    if not request_id:
        return False
    from engine.application import release as release_mod

    attempt_id = pos.attempt_id or ""
    fakes_obj = _resolve_fakes(fakes, attempt_id)
    result = release_mod.approve_and_run(config, ledger, fakes_obj, project, str(request_id),
                                         f"승인 {request_id}")
    ledger.append(Event(
        project=project, kind="release.auto_approved", actor_kind="processor",
        actor_id="auto_pilot", attempt_id=attempt_id,
        payload={"job": job_name, "request_id": str(request_id),
                 "confirm_source": AUTO_CONFIRM_SOURCE,
                 "result_status": result.get("status")},
    ))
    return True


__all__ = ["auto_settle", "AUTO_CONFIRM_SOURCE"]

_BUILD_FAILURE_REASONS = ("build:review_not_passed", "build:bench_not_passed")


def _is_build_review_or_bench_failure(reason: object) -> bool:
    return isinstance(reason, str) and reason.startswith(_BUILD_FAILURE_REASONS)


def _auto_repair(config: EngineConfig, ledger, fakes, project: str, job_name: str,
                 current, on_progress, processor_factory) -> "FullOutcome | None":
    """One repair round for a build that failed review/bench: the same
    `repair.fix` + `job_state.resume_after_fix` path `gb2 fix` runs, with
    no extra owner text. At the round cap (FIX_ROUNDS_EXHAUSTED /
    NO_PROGRESS / OWNS_SCOPE_STUCK) the engine writes the foreman note
    and ends "failed" -- never a forced pass, never an endless loop. Any
    other refusal hands the original outcome back unchanged (None)."""
    from engine.application import repair as fixup
    from engine.application.job_state import resume_after_fix

    def fakes_for(attempt_id: str):
        return _resolve_fakes(fakes, attempt_id)

    try:
        result = fixup.fix(config, ledger, project, "", fakes_for,
                           on_progress=on_progress, processor_factory=processor_factory)
    except fixup.FixupError as exc:
        message = str(exc)
        if message.startswith((FIX_ROUNDS_EXHAUSTED_PREFIX, NO_PROGRESS_PREFIX, OWNS_SCOPE_STUCK_PREFIX)):
            return build_review_cap_stop(config, ledger, project, job_name,
                                         getattr(current, "attempt_id", None) or "", message)
        return None
    full = resume_after_fix(config, ledger, fakes_for, project, result,
                            on_progress=on_progress, processor_factory=processor_factory)
    if full is not None:
        return full
    # Plain single-task job: the fix outcome is the whole result.
    return FullOutcome(result.outcome.status, result.outcome.reason, result.new_attempt_id, None)


def build_review_cap_stop(config: EngineConfig, ledger, project: str, job_name: str,
                          attempt_id: str, reason: str) -> FullOutcome:
    """Build-side twin of plan_review_cap_stop: `build.review_capped`
    event + .gongbang/<job>/foreman-handoff.md with the last review
    findings and how to take over, then "failed"."""
    from engine.application.repair_target import review_findings_for_attempt

    findings = review_findings_for_attempt(ledger, project, attempt_id)
    ledger.append(Event(
        project=project, kind="build.review_capped", actor_kind="processor",
        actor_id="auto_pilot", attempt_id=attempt_id,
        payload={"job": job_name, "stop_reason": reason, "findings": findings, "next": "foreman"},
    ))
    repo = config.projects[project].repo
    note = repo / ".gongbang" / job_name / "foreman-handoff.md"
    lines = [
        f"# 반장 인계 — 구현 검토 상한 ({reason})", "",
        f"- 작업: {job_name} / 시도: {attempt_id}",
        "- 엔진은 여기서 멈췄다. 작업자를 다시 돌리지 않고 반장이 직접 고친다(완성 원칙).", "",
        "## 마지막 검토 지적", "",
    ]
    for item in findings:
        if isinstance(item, dict):
            lines.append(f"- {item.get('severity')} {item.get('file')}:{item.get('line')} — {item.get('note')}")
    lines += ["", "## 다음", "",
              "1. 지적된 파일을 앱 저장소(gb2/work 또는 main)에서 손으로 고치고 시험을 돌린다.",
              f"2. 끝났으면 `gb2 close {project} --reason '반장 직접 구현'` 로 장부를 닫는다.",
              f"3. 엔진이 이어 가게 하려면 `gb2 fix {project} '<무엇을 어떻게 고칠지>' --past-cap` (한 번만).", ""]
    try:
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass
    return FullOutcome("failed", f"build_review_capped:{reason}", attempt_id, None)

def _follow_fallback(ledger, project: str, job_name: str) -> str:
    """`job_name`, or its full-orbit twin when the project's current
    attempt belongs to that twin (quick_pipeline.fallback_job_name)."""
    from engine.application.quick_pipeline import fallback_job_name

    twin = fallback_job_name(job_name)
    if twin == job_name:
        return job_name
    try:
        current_attempt = ledger.project(project).current_attempt_id
    except Exception:  # noqa: BLE001 -- ledger shapes in tests vary; never break settle
        return job_name
    twin_pos = load_position(ledger, project, twin)
    if twin_pos is not None and current_attempt and twin_pos.attempt_id == current_attempt:
        return twin
    return job_name
