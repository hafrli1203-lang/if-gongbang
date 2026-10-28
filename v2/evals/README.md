# AI 결과물 품질 평가 세트

공방 엔진이 실제로 만들어주는 앱의 품질을 재는 첫 평가 세트다. `docs/flow-map.md` ⑧ "9개 영역 대조"의
A항목("AI 결과 품질 측정 — 없음")을 처음 채운다.

## 왜 업종을 10개나 섞었나

"업종 하드코딩 금지" 원칙을 코드가 아니라 **시험 세트 자체**로도 지킨다. 한
업종(예: 세차장)만 평가하면 엔진이 그 업종 전용으로 치우쳤는지 알 수 없다. 이 세트는 서로 다른 10개
업종(`evals/tasks/*.json`)을 돌려, 한 업종의 결과물에 다른 업종의 전용 단어가 섞여 들어가는지까지 자동으로
검사한다(`forbidden_cross_industry_terms`, 아래).

## 과제 파일 (`evals/tasks/*.json`)

과제 10개, 업종 10개(미용실·반찬가게·광고대행사·병원·콘텐츠팀·카페·헬스장·공인중개사·행사주최·프리랜서).
각 파일의 스키마:

```json
{
  "id": "booking-salon",
  "industry": "beauty_salon",
  "request": "사용자가 실제로 입력할 법한 한국어 요청 문장",
  "requirements": [
    {"id": "R1", "text": "요구사항 설명", "kind": "function"}
  ],
  "industry_terms": ["이 업종에서만 쓰는 단어들"],
  "pass_criteria": {
    "required_paths": ["파일/경로 glob 패턴 (대소문자 무시)"],
    "forbidden_cross_industry_terms": true,
    "scenes_must_pass": true,
    "arch_check_must_be_clean": true,
    "build_must_pass": true
  },
  "human_review": ["자동으로는 못 재는, 사람이 봐야 할 것들"]
}
```

- `id`: 파일 이름(확장자 제외)과 반드시 같다.
- `requirements[*].kind`: `function` 또는 `security`만.
- `industry_terms`: 이 업종에서만 쓰는 단어 목록. **다른 과제의 결과물에 이 단어가 나오면 그 다른 과제가
  실패한다** — 업종 전용 어휘가 새어 들어갔다는 뜻이다.
- `pass_criteria.required_paths`: 생성된 저장소 안에서 이 패턴(fnmatch, 대소문자 무시) 중 하나라도 걸리는
  파일이 있어야 한다. 엔진의 새 앱 골격(`tools/templates/new-app`)이 특정 프레임워크의 고정 라우팅 구조를
  강제하지 않으므로, 정확한 파일 경로가 아니라 느슨한 패턴으로 "그 기능이 어딘가에 있다"만 확인한다.
- `human_review`: 자동 채점에 넣지 않는다. 사용자나 검수자가 생성된 앱을 직접 보고 판단한다.

## 통과 기준이 실제로 재는 것

| 기준 | 무엇을 재는가 | 비는 경우 |
|---|---|---|
| `required_paths` | 요청한 핵심 기능/화면이 결과물에 실제로 존재하는가 | — |
| `forbidden_cross_industry_terms` | 다른 업종 전용 단어가 이 결과물에 새어 들어왔는가 (업종 범용성) | — |
| `scenes_must_pass` | `engine.acceptance`가 실제로 기록한 `acceptance.recorded` 사건의 `passed` | 원장에 그 사건이 없으면 실패 |
| `arch_check_must_be_clean` | `tools/arch_check.py <생성된 앱>` (`docs/standards/architecture.md` §5) | **그 스크립트가 없으면 "건너뜀"(실패 아님)** — 과제 자체의 통과/탈락에 영향 없음 |
| `build_must_pass` | `npm run build` (생성된 앱의 `package.json`이 이미 정의한 스크립트) | `package.json`이 없으면 실패 |

## 실행

```bash
# 건식 실행(기본값) — 과제 파일 스키마만 검사. 실제 AI를 절대 부르지 않는다.
tools/run_evals.py
tools/run_evals.py --task booking-salon   # 과제 하나만

# 실제 실행 — 과제마다 임시 GB2_WORKSPACE/GB2_APPS_ROOT에서 `tools/gb2 new`를 실제로 돌린다.
# claude/codex 같은 실제 AI를 부른다 — 손으로만, 시험에서는 절대 쓰지 않는다.
tools/run_evals.py --live
tools/run_evals.py --live --task booking-salon

# 결과를 파일로도 남기기
tools/run_evals.py --out /tmp/eval-results.json
```

건식 실행은 과제 파일 10개의 스키마와 "업종이 4개 이상인가"만 확인하고 끝난다(`tools/run_evals.py`의
`dry_run`). 실제 실행(`--live`)만 결과물 저장소를 만들고 `pass_criteria`를 전부 검사한다 — 과제마다 완전히
새 임시 디렉터리를 쓰므로 여러 과제를 돌려도 서로 섞이지 않고, 사용자의 실제 `apps/`도 건드리지 않는다.

각 과제의 `--live` 결과: `task_id`, `ran`, `wall_sec`(초), `gb2_summary`(`gb2 new`가 찍은 JSON 그대로),
`criteria`(기준별 `passed`/세부 사유), `cost_usd`·`turns`(실제 작업자가 원장에 남긴 값이 있을 때만, 없으면
`null`), `ok`.

## 시험

`tests/unit/test_evals_schema.py` — 과제 파일 10개 전부의 스키마, `tools/run_evals.py`의 건식 실행,
cross-industry 단어 검사 로직 자체를 실제 AI 없이 검증한다.
