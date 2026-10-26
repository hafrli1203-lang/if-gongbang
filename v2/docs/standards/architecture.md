# COMMON ARCHITECTURE STANDARD — 모든 앱 공통 구조 규칙

> kept in sync with the company standard (no internal paths inside; a
> worker/reviewer reads this copy, not the original).

근거: 3계층 구조·보편적 폴더 이름·모듈화 원칙, 코딩 원칙(파일 800줄·함수 50줄·경계에서 Zod 검증·불변성), 회사 앱 중 계층을 이미 나눈 사례(`apt`: `src/domain/*/ports.ts`·`src/integrations/*`), 현재 앱들의 실제 구조(Next.js 16 앱 대부분이 `src/app`·`components`·`db`·`lib` 평평한 구조).
적용 범위: **새로 만드는 앱과 새 코드는 바로**. 이미 있는 앱은 앱마다 계획을 세워 승인 뒤 옮긴다(범위 밖 대량 수정 금지). 앱 자체 `docs/ARCHITECTURE.md`가 있으면 그 앱 안에서는 그 문서가 우선한다.

---

## 1. 세 층과 방향 규칙

| 층 | 하는 일 | 하지 않는 일 |
|---|---|---|
| **interface (입구)** | 화면·API 경로·명령줄. 밖에서 들어온 입력을 검증(Zod/스키마)하고, application을 부르고, 결과를 보여 준다 | 업무 규칙 판단, DB·외부 API 직접 호출 |
| **application (업무 흐름)** | 하나의 사용 장면을 처음부터 끝까지 진행한다(예: "예약 확정", "주문 결제"). domain 규칙을 부르고 infrastructure를 포트로 부른다 | 화면 그리기, SQL·HTTP 세부 |
| **domain (업무 규칙)** | 순수한 규칙·계산·상태(예: 예약 가능 여부, 할인 계산). 입출력 없음 | DB·네트워크·파일·시계·환경변수 직접 접근 |
| **infrastructure (데이터·외부 연결)** | DB, 외부 API, 파일, 메일·결제 같은 바깥 세계. application이 정한 포트(인터페이스)를 구현한다 | 업무 판단 |

**방향:** interface → application → domain. infrastructure는 application·domain이 정한 포트를 구현할 뿐, 위층을 부르지 않는다.
- application은 infrastructure를 직접 불러도 된다. 포트는 바꿔 끼워야 하는 곳에만 둔다.
- 아래층이 위층을 import하면 위반이다.
- 다른 모듈의 비공개 이름(`_이름`, 공개 목록에 없는 것)을 직접 부르면 위반이다.
- 순환 import는 위반이다.

---

## 2. 폴더 이름 (스택별 표준)

### Node · Next.js 앱 (회사 앱 대부분)
```
src/
  app/                 interface — 라우트·페이지·route handler (Next.js 규칙 그대로)
  components/          interface — 화면 부품 (ui/는 범용 부품, 기능별 폴더는 그 기능 부품)
  server/
    actions/           interface — server action (입력 검증 후 application 호출)
    application/       application — 사용 장면별 서비스 (booking/confirm-booking.ts 처럼 동사-명사)
    domain/            domain — 기능별 폴더 안에 model.ts·rules.ts·ports.ts
    infrastructure/
      db/              DB 스키마·쿼리·마이그레이션
      integrations/    외부 API 어댑터 (서비스 이름별 폴더)
  lib/                 층과 무관한 순수 도구만 (날짜 포맷 등). 업무 코드 넣지 않음
  config/              환경변수 읽기·검증 (Zod) — 한 곳에서만
tests/                 src와 같은 구조로 거울처럼 (unit/, e2e/, fakes/)
```

### Python 앱·엔진
```
<패키지>/
  interface/           cli.py, api/ (FastAPI 등), 화면 생성
  application/         사용 장면별 흐름, steps/
  domain/              규칙·모델 (입출력 없음)
  infrastructure/      db/, integrations/, workers/, 파일·프로세스
  config/              설정 읽기·검증
tests/                 unit/, acceptance/, fakes/ (시험용 가짜는 여기에만)
```

### 이름 규칙
- **보편적 이름만** 쓴다: interface·application·domain·infrastructure·db·integrations·config·components·services·repositories·adapters·ports·models·schemas·tests·fakes.
- 회사 내부 별명(예: 궤도, 칸, 시험대)은 폴더·파일 이름에 쓰지 않는다. 문서나 화면 문구에서만 쓴다.
- 파일 이름: TypeScript는 `kebab-case.ts`, React 컴포넌트는 `PascalCase.tsx`, Python은 `snake_case.py`.
- 함수·파일 이름은 **하는 일** 그대로(동사-명사). `utils`, `helpers`, `misc`, `common2`, `new_`, `_v2`, `temp`처럼 무엇이 들었는지 모르는 이름은 금지.
- 비슷한 이름 두 개 금지(예: `gate.py`와 `gates.py`). 하나로 합치거나 역할이 드러나게 바꾼다.
- 업종·브랜드 이름을 폴더·파일·기본값에 넣지 않는다(업종 하드코딩 금지). 업종은 입력값·설정값이다.

---

## 3. 모듈화 규칙

| 규칙 | 기준 | 출처 |
|---|---|---|
| 파일 크기 | 800줄 이하 | 코딩 원칙 |
| 함수 크기 | 50줄 이하, 중첩 4단 이하 | 코딩 원칙 |
| 한 파일 한 책임 | 파일 이름이 그 파일이 하는 일 하나를 말해야 한다 | 외부 개발자 조언 |
| 공개 입구 | 다른 폴더에서 쓰는 것은 `index.ts` 또는 `__all__`로 공개한 이름만 | 이번 엔진 리팩터링 기준 |
| 경계 검증 | 밖에서 들어오는 값(요청·폼·환경변수·외부 API 응답)은 interface·config·infrastructure 경계에서 Zod/스키마로 검증 | 코딩 원칙 |
| 외부 연결 교체 가능 | 외부 API·DB는 포트 뒤에 두어 시험에서 가짜로 바꿀 수 있게 | `apt` 앱 사례 |
| 시험 위치 | `tests/`가 `src/` 구조를 따른다. 시험용 가짜는 `tests/fakes/`에만 | 이번 엔진 리팩터링 기준 |

---

## 4. 예시 (서로 다른 업종)

**예약형 매장 앱(미용실·병원 등):**
`src/app/booking/page.tsx` → `src/server/actions/create-booking.ts`(입력 검증) → `src/server/application/booking/create-booking.ts` → `src/server/domain/booking/rules.ts`(겹치는 예약 판단) + `src/server/domain/booking/ports.ts`(BookingRepository) ← `src/server/infrastructure/db/booking-repository.ts`.

**광고 성과 보고 앱(어느 업종이든):**
`src/app/reports/[id]/page.tsx` → `src/server/application/reports/build-weekly-report.ts` → `src/server/domain/reports/metrics.ts`(CPA·ROAS 계산, 입출력 없음) + `ports.ts`(AdPlatformClient) ← `src/server/infrastructure/integrations/meta-ads/client.ts`, `.../karrot-ads/client.ts`.

---

## 5. 지키는지 확인하는 방법

- **자동 검사:** 구조 검사 도구가 아래를 검사하고 위반이 있으면 실패한다.
  - 파일 800줄 초과
  - 아래층 → 위층 import
  - 비공개 이름 교차 import
  - 순환 import
  - 금지 이름
  - 업종 하드코딩 의심 단어
- **공방 엔진:**
  - 새 앱 골격(`tools/templates/new-app`)이 이 폴더 구조로 시작한다.
  - 계획 칸은 작업마다 어느 층 파일을 고치는지 적는다.
  - 검토 칸은 이 문서 기준으로 위반을 P1/P2로 판정한다.
  - 시험대는 구조 검사를 돌린다.
- **기존 앱:** 앱별 이전 계획(현재 구조 → 이 표준, 옮길 파일 목록, 시험)을 쓰고 승인 뒤 옮긴다. 옮기는 동안 동작은 바꾸지 않는다.
- **코드 리뷰:** 코드 리뷰 기준의 6대 항목에 "구조 위반(층 방향·이름·크기)"을 P2(새 코드는 P1)로 추가한다.
