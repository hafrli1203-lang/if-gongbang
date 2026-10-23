# 강화 설치 (hardened mode)

기본 설치는 `hooks/*`가 챗봇의 협조를 가정한 안전망 한 겹이다(README "이 장치가 맡는 범위"). 강화
설치는 그 위에 에이전트 하네스 자체의 OS 수준 기능 두 가지를 더해, 모델의 Bash/Edit/Write 도구
호출이 **훅 자체·하네스 설정·대화 기록**을 건드리는 경로를 하네스 수준에서도 막는다.

## 두 겹

### 1. Bash sandbox

하네스 설정의 `sandbox` 블록을 켜면, Claude Code가 띄우는 Bash(및 그 하위 프로세스)가 OS 수준
경계(Linux/WSL2는 bubblewrap) 안에서 실행된다. 기본으로 쓰기 가능한 곳은 작업 폴더와 임시
폴더뿐이고, `sandbox.filesystem.denyWrite`로 특정 경로를 명시적으로 더 막을 수 있다.
`sandbox.allowUnsandboxedCommands: false`는 샌드박스 실패 시 모델이 "샌드박스 없이 다시
시도"하는 우회 경로(`dangerouslyDisableSandbox`)를 끈다. `sandbox.failIfUnavailable: true`는
bubblewrap/socat 같은 의존 패키지가 없을 때 **조용히 무보호로 넘어가지 않고 시작 자체를
막는다** — 이 값 없이는 의존성이 빠졌을 때 경고 한 줄만 찍고 보호 없이 계속 실행된다.

**중요한 한계:** Edit/Write 도구, MCP 서버, hook 자체는 이 샌드박스 범위 밖에서 실행된다(하네스
공식 문서). 즉 샌드박스는 Bash가 직접 쓰는 경로만 막고, 모델이 Edit/Write 도구로 같은 경로를
쓰는 것은 막지 못한다 — 그래서 아래 2번이 따로 필요하다.

### 2. 파일 경로 거부 규칙 (`permissions.deny`)

Edit/Write 도구가 특정 경로를 건드리지 못하게 하려면 `permissions.deny`에 그 경로를 올린다.
**`Edit(path)` 규칙만 Write/MultiEdit/NotebookEdit 도구에도 함께 적용된다** — 하네스는
`Write(path)`·`MultiEdit(path)` 형태의 경로 규칙을 문법 오류 없이 받아들이지만 실제로는 절대
확인하지 않는다(하네스 공식 문서: 파일 경로 검사는 `Edit(path)`/`Read(path)`로만 한다). 그래서
Write 도구로 어떤 경로에 쓰는 것을 막으려 해도 반드시 `Edit(...)` 규칙으로 걸어야 한다.

## 설정 예시

`install/settings.hardened.example.json`을 본다 — `install/settings.example.json`에 위 두
블록을 더한 것이다. 보호 대상 경로는 이 저장소가 이미 보호하는 네 곳과 그대로 맞춘다
(`hooks/gongbang_config.py`의 `installed_hooks_dir`, `legacy_gate_dir`, `transcripts_root`,
그리고 하네스 자신의 `settings.json`/`settings.local.json`).

AI가 정상적으로 쓰는 경로(이 저장소의 `lessons_dir`/`library_root`/`log_dir`/`work_queue_path`
같은 작업 파일)는 이 거부 목록에 올리지 않는다 — 좁게 훅·설정·대화 기록만 막는다.

선택: 관리 설정(managed-settings.json)으로 이 블록을 고정하면 프로젝트 쪽 설정이 이를 완화하지
못하게 할 수 있고, 설치된 훅 파일 자체를 OS 소유권으로 한 번 더 막을 수도 있다(예:
`chown root:root`로 소유자를 바꾸고 `chmod 444`로 읽기 전용을 건다) — 이 문서는 그 명령의
형태만 예로 남기고 실행하지 않는다. 실행은 설치자가 자신의 운영 계정 구조에 맞게 직접 한다.

## 시험으로 확인한 범위

이 저장소의 체크아웃(샌드박스/거부 규칙이 전혀 없는 상태)에서 `hooks/scope_guard.py` 자체는
`tests/test_scope_guard.py`의 서브프로세스 테스트로 매번 확인한다 — 이건 하네스와 별개로 이
저장소 혼자 보증하는 부분이다.

하네스 자체의 sandbox/permissions 강제를 실제 `claude -p` 세션으로 시험한 결과:

- **확인됨:** sandbox 의존 패키지(`socat`)가 없는 환경에서 `sandbox.enabled: true`만 켜면
  경고 한 줄을 찍고 **보호 없이 계속 실행**된다(하네스가 실제로 그렇게 출력했다) — 위
  `failIfUnavailable: true`를 꼭 같이 켜야 하는 실제 이유다.
- **확인됨:** 신뢰(trust)를 수락하지 않은 폴더에서는 그 폴더의 `settings.json`에 적은
  `permissions.allow` 항목이 무시된다(하네스가 실제로 그렇게 출력했다) — 즉 강화 설정을 심은
  폴더라도, 사람이 그 폴더에서 최소 한 번 대화형으로 신뢰를 수락해야 설정이 온전히 작동한다.
- **확인 필요:** 신뢰를 수락한 뒤 `permissions.deny`/`sandbox.filesystem.denyWrite`가 실제로
  보호 경로의 쓰기를 막는지는, 이 환경에서 비대화형으로 자동 재현하지 못했다 — 신뢰 수락 자체가
  1회 대화형 확인을 요구하고, 이를 비대화식으로 대신하는 것(하네스 자신의 신뢰/설정 파일을
  대신 고치거나, 승인 절차를 건너뛰는 권한 모드로 실행하는 것)은 이 저장소가 막으려는 바로 그
  종류의 행동이라 시도하지 않았다. 설치자가 아래 절차로 한 번 손으로 확인해야 한다.

## 설치자가 직접 확인하는 절차 (1회, 대화형)

강화 설정을 적용한 프로젝트 폴더에서 평소처럼 `claude`를 대화형으로 열어 신뢰 대화상자를
수락한 뒤, 같은 세션에서 아래 세 가지를 시켜 본다.

1. 그 프로젝트 폴더 **안**에 파일을 만들게 한다 — 성공해야 한다.
2. Bash로 보호 경로(`sandbox.filesystem.denyWrite`에 올린 경로) 안에 파일을 쓰게 한다 — 막혀야
   한다.
3. Write 도구로 보호 경로(`permissions.deny`의 `Edit(...)` 규칙에 올린 경로)에 쓰게 한다 —
   막혀야 한다.

1번만 성공하면 강화 설정이 의도대로 걸린 것이다.

## 외부 검토 반영 — 네 가지 실버그 (재현 뒤 수정)

| 지적 | 재현 | 수정 |
|---|---|---|
| `sudo -u deploy git push`가 관문을 지나감 | `command_hits(...)`가 빈 집합 | `sudo`를 건너뛰는 접두에서 제외 — sudo 자체가 관문 대상 |
| 프로젝트 폴더 목록을 못 읽으면 승인이 세션 전체로 넓어짐 | `listdir` 예외 → `None` → 전체 허용 | 폴더가 없음은 빈 목록, 그 밖의 오류는 거부(fail closed) |
| `~~~` 코드 블록·들여쓰기 코드 안의 `승인 X`가 인정됨 | 두 경우 모두 이름이 추출됨 | `~~~` 블록과 들여쓰기 코드 줄도 제거 |
| 입력 JSON 오류면 허용, `tool_input: null`이면 예외(비차단) | exit 0 / exit 1 | 둘 다 안전 쪽으로: 파싱 실패는 exit 2, null은 빈 명령 |

회귀 시험: `tests/test_v1_hotfix.py`.
