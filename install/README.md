# 설치 방법 (사람이 직접 따라 하는 단계)

자동 설치 스크립트는 일부러 만들지 않았다. 설정 파일을 자동으로 고쳐 쓰는 스크립트는 결함이
생기기 쉽다. 아래 단계를 손으로 따라 한다.

## 1. 설정 파일 만들기

이미 `gongbang.config.json`이 있으면(재설치·업데이트) 절대 덮어쓰지 않는다. `-n`(no-clobber)을
붙이면 파일이 있을 때 조용히 건너뛴다 — 기존 경로·차단 모드·예외 목록을 지키려면 항상 이 형태로
쓴다:

```bash
cp -n gongbang.config.example.json gongbang.config.json
```

새로 받은 예시 파일에 키가 추가됐다면, 기존 `gongbang.config.json`을 지우지 말고 새 키만 손으로
병합한다(diff로 비교 후 필요한 줄만 추가).

`gongbang.config.json`을 열어 자신의 환경에 맞게 값을 채운다. 각 키의 뜻은
`hooks/gongbang_config.py`의 `DEFAULTS` 주석을 본다. 최소한 다음은 꼭 확인한다.

- `apps_root`: 프로젝트들이 모여 있는 폴더.
- `installed_hooks_dir`: hook 파일을 실제로 설치할 폴더(기본 `~/.claude/hooks`). 승인 관문·
  위험 명령 차단 훅은 **이 경로 안의 파일만** 항상 보호한다 — 다른 곳(이 저장소의 체크아웃
  폴더 등)에 같은 이름의 파일이 있어도 그건 보호 대상이 아니다.
- `legacy_gate_dir`: 예전 파일 기반 관문의 흔적을 막기 위한 폴더(기본 `~/.claude/gate`). 지금
  설계는 이 파일을 아예 보지 않지만, 혹시 남아 있는 옛 관문 파일을 모델이 되살리지 못하게
  경로만 계속 보호한다. 빈 문자열 `""`로 설정하면 이 보호를 끈다.
- `transcripts_root`: 에이전트 하네스가 세션 대화 기록(jsonl)을 쌓는 폴더(Claude Code 기준
  `~/.claude/projects`). **승인 관문의 유일한 근거**다 — `approve_gate.py`/`scope_guard.py`가
  더 이상 파일에 승인을 저장하지 않고, 매번 이 경로에서 세션 대화 기록을 다시 읽어 "승인 X"를
  재생한다.
- `approval_window_seconds`: 승인이 유효한 시간(기본 14400 = 4시간).
- `extra_protected_hook_names`: 이 저장소 밖에 추가로 보호하고 싶은 자체 hook 파일 이름 목록
  (선택).
- `work_queue_path`, `alerts_path`, `lessons_dir`, `library_root`: 각자 환경의 실제 경로.

## 2. hook 파일 배치

`hooks/` 폴더를 `installed_hooks_dir`(기본 `~/.claude/hooks`)로 복사하거나, 하네스 설정에서
이 저장소의 절대 경로를 직접 가리키게 한다. **`fence_parse.py`를 다른 두 파일보다 먼저
설치한다** — `approve_gate.py`와 `scope_guard.py` 둘 다 이 파일을 import하므로, 이 파일이
없는 상태에서 PreToolUse 호출이 들어오면 ImportError가 난다(그 경우 "입력 파싱 실패/내부
오류로 차단"으로 안전하게 막히긴 하지만, 설치 중 일시적으로 모든 PreToolUse가 거부되는 것은
피하는 게 낫다). 어느 방식이든 아래 파일들이 같은 폴더 안에 함께 있어야 한다(서로를
import하기 때문):

```
gongbang_config.py
fence_parse.py      (approve_gate.py와 scope_guard.py 둘 다 이 파일을 먼저 필요로 한다)
lessons_store.py
lesson_capture.py   (library_nudge.py가 이 파일의 clean_prompt/MAX_PROMPT_CHARS를 가져다 쓴다)
library_nudge.py
lesson_start.py
approve_gate.py
scope_guard.py       (block_dangerous.py가 이 파일의 파서 함수를 가져다 쓴다)
verify_claims.py
preflight_gate.py
block_dangerous.py
next_task.sh
```

기존 설치를 업그레이드하는 경우, 예전 파일 기반 관문(`<legacy_gate_dir>/approved.json`)이
남아 있으면 지우지 말고 그대로 둬도 안전하다 — 새 코드는 그 파일을 아예 읽지 않는다. 다만
롤백 시 옛 코드가 되살아나 그 파일을 다시 신뢰하지 않도록, 걱정되면 직접 옮겨 두는 것을
권장한다.

## 3. 환경 변수

hook을 실행하는 셸 환경에 다음을 설정한다 (하네스 설정의 `env` 블록이나 셸 프로파일 어디든):

```bash
export GONGBANG_CONFIG=/absolute/path/to/gongbang.config.json
```

지정하지 않으면 각 hook이 자기 파일 위치에서 위로 올라가며 `gongbang.config.json`을 찾는다 — 저장소를
그대로 체크아웃해 쓰는 경우라면 이 자동 탐색만으로도 충분하다.

## 4. 하네스 설정에 hook 등록

`install/settings.example.json`을 참고해 자신의 `settings.json`에 이벤트별로 등록한다. 예시 파일은
그대로 복사해 쓰는 용도가 아니라, 각 hook이 어떤 이벤트(UserPromptSubmit / PreToolUse / Stop /
SessionStart)에 걸리는지 보여 주는 참고용이다. 실제 경로는 자신의 설치 위치로 바꾼다.

## 5. 확인

```bash
python3 -m pytest tests/
```

모두 통과하면 설치가 하네스 자체를 깨뜨리지 않았다는 뜻이다(이 저장소 안의 로직만 검증한다 — 실제
하네스에 등록한 뒤에는 짧은 대화로 한 번 더 손으로 확인한다: `승인 테스트`를 입력해 보고, 바로
"승인 관문 열림: 테스트"가 뜨는지 본다).
