#!/usr/bin/env bash
# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
# IF 공방 작업 사본(wt) 환경 멱등 설치 -- 묶음 8 #30.
# 있으면 건너뛰고, 없으면 만든다. 두 번 돌려도 같은 결과(멱등). .env*는 열지 않는다.
# (2026-11-02 배선 수정: "승인 관문" 문구 제거 -- 승인 관문은 11-02 폐지됐다, MASTER_RULES §7.
#  crontab은 그래도 바꾸지 않는다 -- 적용은 crontab -e로 직접, 이 설치는 제안만 출력한다.)
#
#   bash /apps/_ops/tools/install_v3.sh
set -euo pipefail

HOME_DIR="${HOME:-/some/other/cwd}"
SKILLS_LIBRARY="$HOME_DIR/.claude/skills-library"
SKILLS_DIR="$HOME_DIR/.claude/skills"
AGENTS_DIR="$HOME_DIR/.claude/agents"
PSTACK_SKILLS="$SKILLS_LIBRARY/03-에이전트·하네스·오케스트레이션/pstack/skills"
PSTACK_LINKS=(poteto-mode create-verification-skill ketchup correct)
RAIL_AGENTS=(사실확정자 실측자 계획자 시안자 구현자 검토자)
WT_DATA="${WT_DATA:-/data/_wt}"
WT_SHARED="/apps/_shared"
WT_ADMIN_ENV="/apps/search/.env"
WT_ENVPY="$WT_SHARED/wt-env.py"

say() { echo "[설치] $*"; }

# 1) pstack 링크 4개 -- 있으면 건너뛰고, 없으면 skills-library에서 심링크.
say "1) pstack 링크 4개"
for name in "${PSTACK_LINKS[@]}"; do
  link="$SKILLS_DIR/$name"
  target="$PSTACK_SKILLS/$name"
  if [ -e "$link" ]; then
    echo "  skip  $name (이미 있음)"
  elif [ -d "$target" ]; then
    mkdir -p "$SKILLS_DIR"
    ln -s "$target" "$link"
    echo "  made  $name -> $target"
  else
    echo "  FAIL  $name -- 원본 없음: $target"
  fi
done

# 2) skills-library INDEX.md -- 있으면 건너뛰고, 없으면 build-index.py로 생성.
say "2) skill-library INDEX.md"
INDEX="$SKILLS_LIBRARY/INDEX.md"
BUILD_INDEX="$SKILLS_LIBRARY/tools/build-index.py"
if [ -f "$INDEX" ]; then
  echo "  skip  INDEX.md 있음"
elif [ -f "$BUILD_INDEX" ]; then
  python3 "$BUILD_INDEX"
  echo "  made  $INDEX"
else
  echo "  FAIL  INDEX.md 없음, $BUILD_INDEX 도 없음 -- 확인 필요"
fi

# 3) 작업조 정의(레일 6개) -- 존재만 확인한다. 내용을 지어내 만들지 않는다
#    (근거 없는 에이전트 정의는 facts.md 없이 코드를 짜지 않는 규칙 위반).
say "3) 작업조 정의(레일 6) -- 확인만"
missing_agents=()
for name in "${RAIL_AGENTS[@]}"; do
  if [ -f "$AGENTS_DIR/$name.md" ]; then
    echo "  ok    $name"
  else
    echo "  확인 필요  $name -- $AGENTS_DIR/$name.md 없음 (내용을 지어내 만들지 않음)"
    missing_agents+=("$name")
  fi
done

# 4) 시험 DB 풀 -- 있는 슬롯만 센다. 없는 슬롯을 만들려면 앱마다 다른 소유
#    역할(owner role)이 필요한데 그 생성 호출 형식의 실측 로그가 없어 짜지
#    않는다(probe 없이 DB 생성 코드 금지) -- 빠진 게 있으면 "확인 필요"로 보고.
say "4) 시험 DB 풀 -- 확인만 (없는 슬롯은 만들지 않음, 실측 로그 없음)"
if [ -f "$WT_ENVPY" ] && [ -f "$WT_ADMIN_ENV" ] && command -v psql >/dev/null 2>&1; then
  slot_list="$(python3 "$WT_ENVPY" pg --env-file "$WT_ADMIN_ENV" -- psql -lqt 2>/dev/null \
    | grep -oE '[A-Za-z0-9._-]+_wt[1-3]_test' | sort -u || true)"
  by_app="$(echo "$slot_list" | sed -E 's/_wt([1-3])_test$/ \1/' | awk '{print $1}' | sort -u)"
  total=0
  incomplete=()
  for app in $by_app; do
    count="$(echo "$slot_list" | grep -c "^${app}_wt[1-3]_test$" || true)"
    total=$((total + count))
    [ "$count" = 3 ] || incomplete+=("$app:$count/3")
  done
  echo "  ok    슬롯 DB $total 개"
  if [ "${#incomplete[@]}" -gt 0 ]; then
    echo "  확인 필요  1/2/3 중 빠짐: ${incomplete[*]}"
  fi
else
  echo "  확인 필요  psql 또는 wt-env.py 또는 admin env 없음"
fi

# 5) /data/_wt 폴더 -- 없으면 만든다(순수 디렉터리, 위험 없음).
say "5) /data/_wt 폴더"
if [ -d "$WT_DATA" ]; then
  echo "  skip  있음"
else
  mkdir -p "$WT_DATA"
  echo "  made  $WT_DATA"
fi

# 6) crontab -- 바꾸지 않는다. 제안만 출력. (2026-11-02: wt_board.py 제안은 뺐다 -- the office app 작업 일지
#    화면이 같은 정보를 보여 줘서 _보관으로 옮겼다. tools/_보관/README.md 참고.)
say "6) crontab 제안 (적용하지 않음)"
cat <<'CRON'
  제안: 매일 03:35 환경 doctor 점검(FAIL만 기록)
    35 3 * * * /usr/bin/python3 /apps/_ops/tools/wt_doctor.py >> /data/_wt/doctor.log 2>&1
  적용하려면: crontab -e 로 직접 추가.
CRON

say "끝."
