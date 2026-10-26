# 엔진 폴더 구조 — 옛 경로 → 새 경로

기준: `docs/standards/architecture.md` §1~§3(세 층·폴더 이름·모듈화). 이 표는 옮기기 **전에** 정한 전체 대응표다. 동작은 바꾸지 않는다.

## 규칙

- 옛 경로(`engine/<옛 이름>.py`)는 호환 모듈로 남는다. 내용은 `sys.modules[__name__] = <새 모듈>` 한 줄짜리 별칭이라, 옛 경로로 import하거나 `monkeypatch`·`mock.patch`를 걸어도 새 모듈 자체에 걸린다(모듈이 두 번 실리지 않는다).
- `python -m engine.gate`, `python -m engine.cli`, `python -m engine.fakes.scripted_worker`처럼 실행되는 옛 경로는 `__main__`일 때 새 모듈의 진입 함수를 부르고, import될 때는 별칭이 된다(시험이 이 명령 문자열을 그대로 확인한다).
- `engine/steps/`, `engine/workers/`, `engine/fakes/`는 실제 패키지로 남고(별칭으로 바꾸면 하위 모듈이 새 위치에서 두 번 실린다) 하위 모듈마다 별칭을 둔다. `engine/steps/__init__.py`는 `KINDS`를 새 패키지에서 다시 내보낸다.
- `engine/fakes/hooks/`(정책 훅 시험 스크립트 4개)는 옮기지 않는다. 시험이 이 파일 경로를 설정에 넣고, 설정은 그 경로를 `resolve()`로 풀어 비교하므로 심볼릭 링크로 남기면 시험 값이 달라진다(실측: `test_config.py::test_hooks_are_loaded_from_json_and_resolved`).
- 새 코드는 새 경로만 import한다. 예외: 시험이 `engine` 패키지 속성을 바꿔 끼우는 함수 안 지연 import 네 개(`from engine import app_security / handoff / acceptance / full_orbit`)는 옛 형태를 유지한다 — 바꾸면 그 시험의 바꿔 끼우기가 닿지 않는다.
- 호환 모듈은 나중 작업에서 지운다. 지우기 전에 위 지연 import와 `tools/gb2`·`tools/gb2lib`·`tests`의 옛 경로를 새 경로로 바꿔야 한다.

## 층 방향

interface → application → domain. infrastructure는 domain·config만 import한다. config(`engine/config/`)는 표준 §2 Python 배치대로 층과 무관한 설정 폴더다(모두가 import할 수 있고, config는 config만 import한다).

## 이름을 바꾼 이유

| 옛 이름 | 새 이름 | 이유 |
|---|---|---|
| `full_orbit*`, `small_orbit`, `orbit` | `pipeline*`, `quick_pipeline`, `task_pipeline` | 회사 별명(궤도)을 파일 이름에 쓰지 않는다(표준 §2) |
| `position` | `job_state` | 하는 일(작업 위치 기록·재개) 그대로 |
| `fixup`, `fixup_target` | `repair`, `repair_target` | 같은 이유 |
| `gate.py` / `gates.py` | `interface/hook_gate.py` / `domain/stage_checks.py` | 비슷한 이름 두 개 금지(표준 §2). 앞은 Claude 훅 입구, 뒤는 칸 산출물 검사 |
| `hooks.py` | `infrastructure/policy_hooks.py` | 정책 훅 스크립트 실행기 |
| `bench.py`, `steps/bench_*` | `domain/suite_trust.py`, `steps/suite_*` | 회사 별명(시험대) 금지. `test_*`로 하면 pytest가 시험 파일로 모은다 |
| `station_prompts.py`, `steps/station*` | `stage_prompts.py`, `steps/worker_*` | 회사 별명(칸) 금지. 칸 = 작업자 한 명이 도는 단계 |
| `owner.py` | `owner_approval.py` | 사용자 승인 요청 검사·제출 |
| `ledger*` | `infrastructure/db/ledger*` | 장부 = SQLite DB(표준 §2 `db/`) |
| `fakes/publisher.py` | `infrastructure/fake_publisher.py` | 설정 기본 배포기(`publisher.kind: "fake"`)라 운영 코드가 쓴다 — 시험 전용이 아니다 |

## 같이 나누는 것 (옮기기만, 동작 같음)

- `ledger_schema.py`의 기록 모양(`Event`, `*Row`, `LedgerError`, 상태 상수)은 `engine/domain/records.py`로 옮기고 `ledger_schema`가 그대로 다시 내보낸다. domain·application이 장부 구현이 아니라 기록 모양에만 기대게 한다.
- `context.py`의 `ContextConfig`·`LibraryMcpConfig`(설정 데이터만)는 `engine/config/context_settings.py`로 옮기고 `context.py`가 다시 내보낸다. 설정 모듈이 infrastructure를 import하지 않게 한다.

## 전체 대응표

### interface (입구)

| 옛 경로 | 새 경로 |
|---|---|
| `engine/cli.py` | `engine/interface/cli.py` |
| `engine/board.py` | `engine/interface/board.py` |
| `engine/board_data.py` | `engine/interface/board_data.py` |
| `engine/board_render.py` | `engine/interface/board_render.py` |
| `engine/gate.py` | `engine/interface/hook_gate.py` |

### application (업무 흐름)

| 옛 경로 | 새 경로 |
|---|---|
| `engine/orbit.py` | `engine/application/task_pipeline.py` |
| `engine/full_orbit.py` | `engine/application/pipeline.py` |
| `engine/full_orbit_base.py` | `engine/application/pipeline_base.py` |
| `engine/full_orbit_early.py` | `engine/application/pipeline_planning.py` |
| `engine/full_orbit_release.py` | `engine/application/pipeline_release.py` |
| `engine/full_orbit_resume.py` | `engine/application/pipeline_resume.py` |
| `engine/full_orbit_runner.py` | `engine/application/pipeline_stages.py` |
| `engine/full_orbit_tasks.py` | `engine/application/pipeline_tasks.py` |
| `engine/small_orbit.py` | `engine/application/quick_pipeline.py` |
| `engine/position.py` | `engine/application/job_state.py` |
| `engine/fixup.py` | `engine/application/repair.py` |
| `engine/fixup_target.py` | `engine/application/repair_target.py` |
| `engine/release.py` | `engine/application/release.py` |
| `engine/release_certificate.py` | `engine/application/release_certificate.py` |
| `engine/handoff.py` | `engine/application/handoff.py` |
| `engine/acceptance.py` | `engine/application/acceptance.py` |
| `engine/acceptance_bench.py` | `engine/application/acceptance_bench.py` |
| `engine/acceptance_probe.py` | `engine/application/acceptance_probe.py` |
| `engine/acceptance_scene.py` | `engine/application/acceptance_scene.py` |
| `engine/processor.py` | `engine/application/processor.py` |
| `engine/owner.py` | `engine/application/owner_approval.py` |
| `engine/station_prompts.py` | `engine/application/stage_prompts.py` |
| `engine/app_security.py` | `engine/application/app_security.py` |
| `engine/steps/__init__.py` | `engine/application/steps/__init__.py` |
| `engine/steps/bench_run.py` | `engine/application/steps/suite_run.py` |
| `engine/steps/bench_select.py` | `engine/application/steps/suite_select.py` |
| `engine/steps/bench_snapshot.py` | `engine/application/steps/suite_snapshot.py` |
| `engine/steps/bench_zero_tests.py` | `engine/application/steps/suite_zero_tests.py` |
| `engine/steps/close.py` | `engine/application/steps/close.py` |
| `engine/steps/commit.py` | `engine/application/steps/commit.py` |
| `engine/steps/install.py` | `engine/application/steps/install.py` |
| `engine/steps/merge.py` | `engine/application/steps/merge.py` |
| `engine/steps/open_job.py` | `engine/application/steps/open_job.py` |
| `engine/steps/publish.py` | `engine/application/steps/publish.py` |
| `engine/steps/report.py` | `engine/application/steps/report.py` |
| `engine/steps/review.py` | `engine/application/steps/review.py` |
| `engine/steps/station.py` | `engine/application/steps/worker_step.py` |
| `engine/steps/station_gate.py` | `engine/application/steps/worker_gate_log.py` |
| `engine/steps/station_paths.py` | `engine/application/steps/worker_paths.py` |
| `engine/steps/station_sandbox.py` | `engine/application/steps/worker_sandbox.py` |

### domain (업무 규칙)

| 옛 경로 | 새 경로 |
|---|---|
| `engine/plan.py` | `engine/domain/plan.py` |
| `engine/plan_model.py` | `engine/domain/plan_model.py` |
| `engine/plan_review.py` | `engine/domain/plan_review.py` |
| `engine/plan_tools.py` | `engine/domain/plan_tools.py` |
| `engine/evidence_graph.py` | `engine/domain/evidence_graph.py` |
| `engine/policy.py` | `engine/domain/policy.py` |
| `engine/gates.py` | `engine/domain/stage_checks.py` |
| `engine/approvals.py` | `engine/domain/approvals.py` |
| `engine/state.py` | `engine/domain/state.py` |
| `engine/ids.py` | `engine/domain/ids.py` |
| `engine/faults.py` | `engine/domain/faults.py` |
| `engine/bench.py` | `engine/domain/suite_trust.py` |

### config (설정, 층과 무관)

| 옛 경로 | 새 경로 |
|---|---|
| `engine/config.py` | `engine/config/__init__.py` |
| `engine/config_model.py` | `engine/config/model.py` |
| `engine/config_fields.py` | `engine/config/fields.py` |
| `engine/config_io.py` | `engine/config/io.py` |

### infrastructure (데이터·외부 연결)

| 옛 경로 | 새 경로 |
|---|---|
| `engine/ledger.py` | `engine/infrastructure/db/ledger.py` |
| `engine/ledger_backup.py` | `engine/infrastructure/db/ledger_backup.py` |
| `engine/ledger_handlers.py` | `engine/infrastructure/db/ledger_handlers.py` |
| `engine/ledger_schema.py` | `engine/infrastructure/db/ledger_schema.py` |
| `engine/sandbox.py` | `engine/infrastructure/sandbox.py` |
| `engine/procs.py` | `engine/infrastructure/procs.py` |
| `engine/gitutil.py` | `engine/infrastructure/gitutil.py` |
| `engine/exit_writer.py` | `engine/infrastructure/exit_writer.py` |
| `engine/publishers.py` | `engine/infrastructure/publishers.py` |
| `engine/installers.py` | `engine/infrastructure/installers.py` |
| `engine/trusted.py` | `engine/infrastructure/trusted.py` |
| `engine/evidence.py` | `engine/infrastructure/evidence.py` |
| `engine/context.py` | `engine/infrastructure/context.py` |
| `engine/skills.py` | `engine/infrastructure/skills.py` |
| `engine/browser_check.py` | `engine/infrastructure/browser_check.py` |
| `engine/bootstrap.py` | `engine/infrastructure/bootstrap.py` |
| `engine/release_git.py` | `engine/infrastructure/release_git.py` |
| `engine/hooks.py` | `engine/infrastructure/policy_hooks.py` |
| `engine/workers/__init__.py` | `engine/infrastructure/workers/__init__.py` |
| `engine/workers/base.py` | `engine/infrastructure/workers/base.py` |
| `engine/workers/claude_cli.py` | `engine/infrastructure/workers/claude_cli.py` |
| `engine/workers/codex_cli.py` | `engine/infrastructure/workers/codex_cli.py` |
| `engine/workers/prompts.py` | `engine/infrastructure/workers/prompts.py` |
| `engine/workers/scripted.py` | `engine/infrastructure/workers/scripted.py` |
| `engine/fakes/publisher.py` | `engine/infrastructure/fake_publisher.py` |

### tests/fakes (시험용 가짜)

| 옛 경로 | 새 경로 |
|---|---|
| `engine/fakes/__init__.py` | `tests/fakes/__init__.py` |
| `engine/fakes/installer.py` | `tests/fakes/installer.py` |
| `engine/fakes/worker.py` | `tests/fakes/worker.py` |
| `engine/fakes/scripted_worker.py` | `tests/fakes/scripted_worker.py` |
| `engine/fakes/hooks/crash.py` | (그대로 — 위 규칙) |
| `engine/fakes/hooks/garbage.py` | (그대로 — 위 규칙) |
| `engine/fakes/hooks/ok.py` | (그대로 — 위 규칙) |
| `engine/fakes/hooks/slow.py` | (그대로 — 위 규칙) |

새 파일(옛 경로 없음): `engine/domain/records.py`, `engine/config/context_settings.py`, 각 층 폴더의 `__init__.py`.
