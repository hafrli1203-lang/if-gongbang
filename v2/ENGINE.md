# IF 공방 v2 엔진 — 동작 명세

코드가 기준이다. 이 문서는 지금 코드가 하는 일을 설명한다.

## 1. 식별자
- `job_id = J-{project}-{ULID 26자}`, `attempt_id = {job_id}#{n}`(같은 일의 n번째 회차, 복구도 새 회차).
- `request_id = R-{sha256(project|attempt|kind|정규화 payload)[:16]}` — 같은 내용을 다시 내면 같은 번호, 새 효과 없음.
- 파일 경로를 만들 때는 회차 번호의 `#`을 `-`로 바꾼다(도구들이 경로 속 `#`을 URL 조각으로 읽는 문제를 피함).

## 2. 장부
SQLite(WAL) 한 파일. 모든 쓰기는 사건 하나(`append`)와 파생 표 갱신을 한 트랜잭션으로 한다. 파일이 없거나 스키마 버전이
다르면 열지 않는다(새로 만들지 않음). 요청 상태 `queued → running → done | failed | unknown`; `unknown`은 사람이
`retry-unknown`으로만 되돌리고, 그동안 그 프로젝트의 처리기는 아무것도 처리하지 않는다.

## 3. 상태와 처리기
- 프로젝트 상태 `CLOSED / OPEN / CLOSING / RECOVERING`. 전이는 표 하나(`engine/state.py`)가 유일한 출처.
- 처리기는 프로젝트마다 하나만 돈다((holder, pid) 잠금, 죽은 pid의 잠금은 회수).
- 요청 종류마다 단계 목록이 고정되고 단계마다 일지를 남긴다. 재개 때 끝난 단계는 건너뛰고, 시작만 된 단계는
  대조 함수로 실제 결과를 확인한다. 뒤 단계는 앞 단계의 일지 내용을 믿지 않고 관찰 가능한 사실(HEAD, 파일, 장부)에서 다시 읽는다.

| 요청 | 단계 |
|---|---|
| run_station | spawn_worker → await_worker → record_station |
| merge | collect_copies → check_conflicts → apply_allowed_paths → record |
| review | compute_fingerprint → spawn_reviewer → await_reviewer → record_review |
| bench | compute_fingerprint → run_tests → trust_checks → record_bench |
| commit | compute_fingerprint → verify_three → git_commit → record_commit → report |
| publish | check_approval → check_artifact → (advance_master) → record_source → call_publisher → record_publish → report |
| install 류 | acquire_project_lock → snapshot_manifest → run_installer → verify_manifest → invalidate_evidence → record |
| close / abandon | set_closing → archive_tmp → archive_commit → reset_to_head → apply_deferred_approvals → verify_tree_eq_head → set_closed → open_queue |

## 4. 궤도(한 건)
열기 → 빌드 → 합치기 → 검수 + 시험대(동시에) → 커밋 → 보고 → 닫기. 배포 설정이 있으면 커밋 뒤 사람 승인을 기다린 뒤
발행한다. 작업자가 사람의 조치(의존성 추가 등)를 요청하면 합치기까지 끝낸 뒤 멈춘다.

## 5. 작업자와 관문
- 작업자 = 자식 프로세스. 빌더는 Claude CLI(`--setting-sources ""`, PreToolUse 훅 = `python -P -m engine.gate`), 검수자는
  다른 계열 CLI를 읽기 전용 샌드박스로.
- 관문은 엔진이 미리 써 둔 **읽기 전용 사실 파일**(프로젝트 상태·세대)과 설정으로 판정하고, 판정을 JSONL 로그에 쓴다.
  장부는 열지 않는다. 처리기는 작업자가 끝난 뒤(실패·시간초과 포함) 로그를 감사 전용 사건으로 가져온다. 로그 속 종류·
  프로젝트·회차 값은 쓰지 않고 엔진이 아는 값으로 묶는다.
- 정책: 미등록 프로젝트 전부 거부 / 작업자 읽기는 자기 사본 안(realpath) / 쓰기는 칸·역할별 경로 접두어 안 / 터미널은
  칸·역할별 정규식 fullmatch, 셸 메타문자·`~`·`..`·숨은 문자 거부 / 보호 경로(장부·비밀·.git·.gitignore·설정 파일 등)는 항상 거부.
- 훅 실패는 막는 쪽으로: 시작 실패·내부 멈춤은 종료코드 2, 사실 파일이 없거나 깨지면 거부 응답.

## 6. OS 샌드박스(bubblewrap)
- 설정 `sandbox: "bwrap"`. 마운트는 경로 깊이 순으로 내고, 숨길 경로와 쓸 경로가 같거나 조상 관계로 숨김을 되살리면 거부한다.
- 작업자: 쓰기 = 자기 사본·자기 칸 폴더·CLI 상태 폴더; 장부 폴더·비밀 폴더·엔진 홈·이 프로젝트 저장소·다른 프로젝트·`hidden_paths`(예: 앱 원본 저장소)는 숨김;
  사실 파일·설정은 읽기 전용 전용 폴더. 네트워크 있음(CLI 인증).
- 시험대: 쓰기 = 저장소(단 `.git`은 읽기 전용); 실제 홈은 숨기고 회차별 빈 홈을 준다(필요한 캐시만 `bench_readonly_paths`로
  읽기 전용 노출); 네트워크 없음; 케이스 환경 변수로 PATH·LD_PRELOAD·HOME을 바꿀 수 없다.
- 엔진이 직접 부르는 git은 `core.fsmonitor`·`core.hooksPath`를 끈다.

## 7. 사본·합치기
- 사본 = `git archive` + 기준 커밋. `copy_links`로 gitignore된 큰 폴더(예: node_modules)를 링크하고, `copy_link_writable_subpaths`의
  하위 폴더만 사본 전용 쓰기 폴더로 둔다. 링크 원본이 저장소 밖이거나 숨김 영역이면 만들지도 마운트하지도 않는다.
- 합치기 후보는 사본의 git 상태(사본 .gitignore 적용)와 `excludes`로 고른 파일 중 허용 경로 안의 것만. 기준 커밋에서 목적지가
  바뀌었으면 `merge_conflict_with_head`.

## 8. 검수·시험대·커밋
- 검수자는 지문을 계산한 트리를 그대로 내보낸 읽기 전용 스냅숏을 읽는다(동시에 도는 시험대가 저장소를 건드려도 검수 대상은 바뀌지 않는다).
- 검수자는 원래 요청, 바뀐 파일 목록, 전체 diff를 받는다. 엔진이 diff를 다 보내지 못하면 범위를 "부분"으로 기록하고, 엔진과 검수자 둘 다 "전체"일 때만 통과한다.
- 검수 결과는 형식(심각도 P0~P3, 필드 타입)을 검사하고, P0·P1이 없을 때만 통과. 형식이 깨지면 실패.
- 시험대 기본값은 `bench_scope: "related"` — 바뀐 파일과 관련된 시험(`related_cmd`) + 신뢰 검사 케이스. 관련 시험이 하나도 실행되지 않았거나 삭제·이름 변경이 섞이면 전체 시험으로 돈다. 신뢰 검사는 빈 시험,
  항상 통과하는 채점기, 잘못된 실패 서명을 거부하고, 음성 대조 케이스가 실제로 실패하는지 확인한다.
- 실패한 요청은 같은 명령으로 다시 낼 수 있다(이전 요청과 연결된 새 요청이 생긴다). 닫기 실패 뒤에도 원인을 고치고 다시 닫으면 끝난다.
- 커밋: 검수·시험대·지문이 같은 트리일 때만. HEAD를 직전에 다시 읽어 비교 교체로 갱신.

## 9. 사람의 조치와 발행
- 작업자 요청 중 `install`·`scripts_change`·`test_config_change`만 받는다. `install`은 의존성 절만, 버전 표기만(파일·URL·git 금지).
  나머지 둘은 실행 문자열을 담으므로 터미널에서 사람이 직접 확인해야만 승인된다.
- 발행 승인은 회차·행동·대상·해시·만료·요청번호에 묶이고 한 번만 쓰인다. `@HEAD` 발행은 커밋 해시와 깨끗한 트리를 발행 직전에 다시 확인한다.
- 배포 설정(`release`)이 있으면 커밋 뒤 멈추고, 사람이 터미널에서 승인 문구를 입력해야 원본 master를 그 커밋으로 빨리감기(무시 파일 덮어쓰기 금지)한 뒤 배포한다. 공유 작업 트리에 추적 파일 수정이나 미추적 파일이 있으면 배포하지 않는다(배포는 작업 트리를 그대로 빌드하므로). 승인은 합치기 직전과 배포 직전에 두 번 확인한다.
- 배포 전에는 그 커밋에 대한 전체 시험 통과 증거를 새로 확인한다(관련 시험만으로는 출시하지 않는다).
- 배포 결과가 불명확하면(`unknown`) 다시 배포하지 않고, `gb2 resolve`로 확인 명령 결과를 본 뒤 사람이 결과를 기록한다. 영수증은 원자적으로 저장한다.
- 배포 명령은 `publisher.kind: "shell"`로 부르고, 자기 프로세스 그룹에서 돌려 시간 초과 시 그룹 전체를 끝낸다. 영수증이 `done`일 때만 성공으로 본다. 실행 전후 영수증을 남겨 끊겨도 같은 키로 두 번 배포하지 않는다. `dry_run`을 먼저 쓴다.

## 10. 실행 명령
```
python -m engine.cli init|open|run|status|approve|retry-unknown|close --config <config.json>
GB2_APPS_ROOT=<앱들이 있는 폴더> GB2_WORKSPACE=<엔진 작업 폴더>
tools/gb2 init <app> [--with-release]  # 앱 원본의 작업 가지·작업 트리·설정·장부를 만든다(원본 작업 트리는 건드리지 않음)
tools/gb2 run <app> "<할 일>"          # 한 건을 궤도 끝까지
tools/gb2 ship <app> | skip-ship <app>  # 배포 승인(터미널 입력) 또는 배포 없이 닫기
tools/gb2 sync <app>                    # 작업 가지를 최신 master 위로
tools/gb2 status|log|diff <app>
```

## 11. 한계
- 같은 사용자 권한의 작업자 코드는 샌드박스가 보여 주는 범위를 정책 없이 읽는다. 관문 로그는 감사 기록일 뿐이다.
- 세션을 떼어 낸 자손 프로세스는 프로세스 그룹 종료로 못 잡는다(cgroup 미사용). 처리기가 죽은 동안 끝난 작업자의 종료코드는 알 수 없다.
- 복구된 회차가 이전 회차의 설치 표식을 보지 못해 끝난 설치를 반복할 수 있다.

## 전체 궤도 (`engine/full_orbit.py`)
- 사실확정·계획·(시안)은 한 회차를 함께 쓰고, 계획의 작업(task)마다 새 회차에서 구현 → 합치기 → 검토 + 시험대 → 커밋 → 보고 → 닫기를 돈다. 작업의 `owns`가 그 회차의 쓰기 범위다.
- 마지막 작업 뒤 계획의 통과 장면(`tests/scenes/`)과 전체 시험을 돌리고(`engine/acceptance.py`), 실패하면 고치기 작업을 정해진 횟수만큼 돈 뒤 인계 보고(`report.md`)를 쓴다.
- 시안이 있는 일은 사람이 `gb2 approve-design`으로 승인해야 구현이 열린다.

## 칸별 주입 (`engine/context.py`, `engine/skills.py`)
- 설정 `context`: `skill_roots`, `station_skills`(칸의 바탕 스킬), `library_mcp`(회사 자료 stdio MCP), `decisions_paths`, `lessons_app`, `direction_path`.
- 계획 칸은 스킬 목록 전체(바로 쓰는 스킬 설명, 창고 색인, 묶음 안 스킬 이름, 허락 가능한 MCP)를 받고 `plan.json`의 `tools`에 칸별 도구표를 쓴다. 구현·시안·검토 칸 지시문에는 바탕 스킬 + 계획이 고른 스킬 본문이 들어간다. 어느 뿌리에도 없는 스킬은 주입하지 않고 보고서에 "설치 필요"로 남는다.
- 자료 MCP 결과 중 AI가 만든 출력(`run_output`)은 넣지 않는다. 사람이 정한 글(결정·방향)은 자르지 않는다. 장부에 `context.injected {station, items, base, planned, missing}`.

## MCP 허락 (`engine/policy.py`, `engine/workers/claude_cli.py`)
- `mcp_servers`와 `station_mcp_allow`(칸별 상한). 한 요청에 열리는 서버 = 계획 도구표 ∩ 칸 상한. 작업자에게는 그 서버만 담은 `--mcp-config`와 `--strict-mcp-config`를 준다. 관문은 `mcp__<server>__*` 호출을 그 목록으로만 허락한다.

## 이어서 하기 (`engine/workers/base.py`, `engine/orbit.py`)
- 작업자에게 총 시간 제한은 없다. 출력·관문 기록·결과 파일이 `timeout_sec` 동안 하나도 바뀌지 않을 때만 멈춘 것으로 본다.
- 멈춤(`worker_stalled`)이나 중간 종료(`worker_failed`)는 실패로 끝내지 않는다. 같은 칸을 같은 사본에서 원래 지시 + 이미 바뀐 파일 목록과 함께 다시 돌린다. 세 번 뒤에도 안 되면 `needs_owner`(`worker_stuck`).
- `WorkerConfig.budget_usd`·`max_turns`는 기본 없음(설정한 경우에만 CLI에 넘김).
