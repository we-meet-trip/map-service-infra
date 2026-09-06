#!/usr/bin/env bash
# 바깥에서 들어오는 길 그대로 훑는다.
#
# 왜 관문을 거치는가:
#   앱이 실제로 닿는 곳이 관문이다. 서비스에 직접 물으면 관문이 막는 것과
#   여는 것, 그리고 관문에서만 드러나는 한계가 검사에서 통째로 빠진다.
#
# 왜 모델을 되도록 부르지 않는가:
#   일정 한 건이 모델을 세 번 쓰고 하루 한도가 정해져 있다. 검사가 그 몫을
#   많이 쓰면 정작 시연에서 쓸 것이 줄어든다. 이미 만들어 둔 조건을 다시 눌러
#   캐시로 답하게 하고, 새로 만드는 것은 꼭 필요한 한 건만 한다.
#
# 사용:
#   ./scripts/e2e.sh                    # 관문(8090) 경유
#   BASE=http://127.0.0.1:8080 ./scripts/e2e.sh
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8090}"
EMAIL="${E2E_EMAIL:-maptester1@admin.map}"
PASSWORD="${E2E_PASSWORD:-admin123!}"
WEB_ORIGIN="${E2E_WEB_ORIGIN:-https://mapcenter-b59ca.web.app}"

pass=0; fail=0; skip=0
TOKEN=""

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
no()   { printf '  \033[31m✗\033[0m %s — %s\n' "$1" "$2"; fail=$((fail+1)); }
sk()   { printf '  \033[33m-\033[0m %s (%s)\n' "$1" "$2"; skip=$((skip+1)); }
sec()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

# 상태코드만 확인한다. want 는 쉼표로 여러 개를 받는다.
expect() {
  local label="$1" want="$2" got="$3"
  case ",$want," in *",$got,"*) ok "$label ($got)";; *) no "$label" "기대 $want, 실제 $got";; esac
}

code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }

# 상태코드와 본문을 함께 집는다. 마지막 줄이 코드고 나머지가 본문이다.
# 400 이 났을 때 어떤 400 인지(형식 오류인지 규칙 위반인지)가 남아야 고칠 수 있다.
code_body() {
  local out; out=$(curl -s -w $'\n%{http_code}' "$@")
  CODE=$(printf '%s' "$out" | tail -1)
  BODY=$(printf '%s' "$out" | sed '$d')
}
body() { curl -s "$@"; }

sec "1. 관문"
expect "상태 확인이 열려 있다" "200" "$(code "$BASE/healthz")"
expect "운영 지표 경로가 막혀 있다" "404" "$(code "$BASE/actuator/health")"
expect "목록에 없는 경로가 막혀 있다" "404" "$(code "$BASE/internal/admin/ping")"

sec "2. 교차 출처"
CORS_PATH="/api/v1/recommend/$(uuidgen)"
h=$(curl -s -D- -o /dev/null -X OPTIONS "$BASE$CORS_PATH" \
      -H "Origin: $WEB_ORIGIN" -H "Access-Control-Request-Method: GET")
echo "$h" | grep -qi "access-control-allow-origin" \
  && ok "게시 출처는 허용된다" || no "게시 출처는 허용된다" "허용 헤더 없음"
c=$(curl -s -o /dev/null -w "%{http_code}" -X OPTIONS "$BASE$CORS_PATH" \
      -H "Origin: https://evil.example.com" -H "Access-Control-Request-Method: GET")
expect "임의 출처는 거절된다" "403" "$c"

sec "3. 인증"
login=$(body -X POST "$BASE/api/v1/auth/login" -H "Content-Type: application/json" \
          -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")
TOKEN=$(printf '%s' "$login" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('access_token') or d.get('accessToken') or '')" 2>/dev/null)
if [ -n "$TOKEN" ]; then
  ok "테스터 계정으로 로그인된다"
else
  no "테스터 계정으로 로그인된다" "토큰 없음: $(printf '%s' "$login" | head -c 120)"
fi
AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

sec "4. 조회 경로 (모델 미사용)"
expect "날씨(홈 카드)" "200" "$(code "$BASE/api/v1/weather/home?lat=37.5665&lng=126.9780")"
expect "장소 검색" "200,400" "$(code "$BASE/api/v1/places?province=서울특별시&city=종로구&query=카페")"
expect "일정 목록" "200" "$(code "${AUTH[@]}" "$BASE/api/v1/schedules")"

sec "5. 일정 생성 — 이미 만들어 둔 조건 (캐시로 답해야 한다)"
WARM='{"schedule":{"start_date":"2026-09-05","end_date":"2026-09-05","active_start_hour":10,"active_end_hour":18},"budget":{"min":0,"max":100000},"themes":["산책"],"transport":"walk","location":{"province":"서울특별시","city":"종로구"}}'
t0=$(python3 -c 'import time;print(time.time())')
resp=$(body -X POST "$BASE/api/v1/trip/generate" -H "Content-Type: application/json" "${AUTH[@]}" -d "$WARM")
t1=$(python3 -c 'import time;print(time.time())')
el=$(python3 -c "print(f'{$t1-$t0:.1f}')")
tid=$(printf '%s' "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin).get('trip_id',''))" 2>/dev/null)
stops=$(printf '%s' "$resp" | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('stops') or []))" 2>/dev/null)
if [ -n "$tid" ] && [ "${stops:-0}" -gt 0 ]; then
  ok "예열된 조건이 즉시 답한다 (${el}초, 장소 ${stops}개)"
  awk "BEGIN{exit !($el < 3.0)}" && ok "캐시로 답했다 (3초 미만)" || no "캐시로 답했다" "${el}초 — 새로 만든 듯"
else
  no "예열된 조건이 즉시 답한다" "본문: $(printf '%s' "$resp" | head -c 120)"
fi

sec "6. 결과 조회"
if [ -n "$tid" ]; then
  expect "생성된 일정을 다시 읽는다" "200" "$(code "${AUTH[@]}" "$BASE/api/v1/recommend/$tid")"
else
  sk "생성된 일정을 다시 읽는다" "직전 단계 실패"
fi
expect "없는 식별자는 진행 중으로 답한다" "202" "$(code "$BASE/api/v1/recommend/$(uuidgen)")"

sec "7. 초안 수정 (소유권·기록)"
if [ -n "$tid" ]; then
  # 순서와 이동 구간은 장소를 자리 번호로 가리킨다. 셋을 함께 보내야 앞뒤가 맞는다.
  ONEPLACE='{"place_id":0,"day":1,"name":"E2E남은곳","address":"주소","lat":37.5,"lng":127.0,"recommended_visit_time":"10:00"}'
  code_body -X POST "$BASE/api/v1/recommend/$tid/edit" -H "Content-Type: application/json" \
      -H "Idempotency-Key: e2e-$(date +%s)" "${AUTH[@]}" \
      -d "{\"places\":[$ONEPLACE],\"visit_order\":[0],\"legs\":[]}"
  case ",200,404," in
    *",$CODE,"*) ok "본인 초안을 고칠 수 있다 ($CODE)";;
    *) no "본인 초안을 고칠 수 있다" "기대 200,404, 실제 $CODE — $(printf '%s' "$BODY" | head -c 160)";;
  esac

  # 장소만 줄이고 순서를 그대로 두면 없는 자리를 가리킨다. 저장되면 그 일정은
  # 다시 열 때 비어 보이므로, 저장하기 전에 막아야 한다.
  expect "앞뒤가 안 맞는 수정은 거절된다" "400" \
    "$(code -X POST "$BASE/api/v1/recommend/$tid/edit" -H "Content-Type: application/json" \
        -H "Idempotency-Key: e2e-bad-$(date +%s)" "${AUTH[@]}" \
        -d "{\"places\":[$ONEPLACE],\"visit_order\":[0,1,2,3]}")"
else
  sk "본인 초안을 고칠 수 있다" "직전 단계 실패"
fi

sec "8. 한도"
lim_hit=0
for _ in $(seq 1 12); do
  c=$(code -X POST "$BASE/api/v1/trip/generate" -H "Content-Type: application/json" -d "$WARM")
  [ "$c" = "429" ] && { lim_hit=1; break; }
done
[ "$lim_hit" = "1" ] && ok "생성 경로에 분당 상한이 걸린다 (429)" \
  || no "생성 경로에 분당 상한이 걸린다" "12회 연속 429 없음"

sec "9. 이번 작업 산출물이 실제로 쌓이는가"
q() { docker exec map-service-postgres psql -h 127.0.0.1 -At -U map -d map -c "$1" 2>/dev/null; }
[ "$(q "SELECT count(*) FROM user_service.recommend_jobs WHERE source IS NOT NULL;")" -gt 0 ] \
  && ok "잡 출처(source)가 기록된다" || no "잡 출처(source)가 기록된다" "0건"
if [ "${TRAINING_CAPTURE_ENABLED:-false}" = true ]; then
  [ "$(q "SELECT count(*) FROM user_service.recommend_training;")" -gt 0 ] \
    && ok "학습 신호가 보관된다" || no "학습 신호가 보관된다" "0건"
else
  sk "학습 신호 수집" "학습 보류; 생성·Streams 발행 차단은 agent 회귀시험으로 검증"
fi
[ "$(q "SELECT count(*) FROM user_service.recommend_edits;")" -gt 0 ] \
  && ok "초안 수정 전후가 남는다" || no "초안 수정 전후가 남는다" "0건"
[ "$(q "SELECT count(*) FROM user_service.recommend_jobs WHERE source='cache_hit';")" -gt 0 ] \
  && ok "캐시로 답한 잡이 구분된다" || no "캐시로 답한 잡이 구분된다" "0건"

sec "10. 일정 저장 이후 (저장 → 목록 → 상세 → 시작)"
SID=""
if [ -n "$tid" ]; then
  SAVE="{\"job_id\":\"$tid\",\"title\":\"E2E 종로 산책\",\"date_start\":\"2026-09-05\",\"date_end\":\"2026-09-05\",\"transport\":\"walk\",\"active_start_hour\":10,\"active_end_hour\":18}"
  sresp=$(body -X POST "$BASE/api/v1/schedules" -H "Content-Type: application/json" "${AUTH[@]}" -d "$SAVE")
  SID=$(printf '%s' "$sresp" | python3 -c "import sys,json;print(json.load(sys.stdin).get('schedule_id',''))" 2>/dev/null)
  [ -n "$SID" ] && ok "만든 일정이 저장된다 (schedule_id=$SID)" \
    || no "만든 일정이 저장된다" "본문: $(printf '%s' "$sresp" | head -c 120)"
else
  sk "만든 일정이 저장된다" "생성 단계 실패"
fi

if [ -n "$SID" ]; then
  printf '%s' "$(body "${AUTH[@]}" "$BASE/api/v1/schedules")" \
    | grep -q "\"schedule_id\":$SID" \
    && ok "저장 목록에 나온다" || no "저장 목록에 나온다" "목록에 없음"

  # 상세는 방문지까지 조립해서 온다 — 목록에는 제목과 기간뿐이라 여기서만 본다.
  n=$(body "${AUTH[@]}" "$BASE/api/v1/schedules/$SID" \
      | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('stops') or []))" 2>/dev/null)
  [ "${n:-0}" -gt 0 ] && ok "상세에 방문지가 실려 온다 (${n}곳)" \
    || no "상세에 방문지가 실려 온다" "stops 0개"

  # 시작 시각은 처음 한 번만 새긴다. 두 번 눌러도 같아야 한다.
  code -X POST "${AUTH[@]}" "$BASE/api/v1/schedules/$SID/start" >/dev/null
  first=$(docker exec map-service-postgres psql -h 127.0.0.1 -At -U map -d map \
          -c "SELECT started_at FROM user_service.schedules WHERE schedule_id=$SID;" 2>/dev/null)
  code -X POST "${AUTH[@]}" "$BASE/api/v1/schedules/$SID/start" >/dev/null
  second=$(docker exec map-service-postgres psql -h 127.0.0.1 -At -U map -d map \
           -c "SELECT started_at FROM user_service.schedules WHERE schedule_id=$SID;" 2>/dev/null)
  [ -n "$first" ] && ok "따라가기 시작이 새겨진다 ($first)" \
    || no "따라가기 시작이 새겨진다" "started_at 비어 있음"
  [ "$first" = "$second" ] && ok "두 번 눌러도 시작 시각은 그대로다" \
    || no "두 번 눌러도 시작 시각은 그대로다" "$first → $second"
else
  sk "저장 이후 흐름" "저장 실패"
fi

sec "11. 채팅 (두 사람 · 관문 경유 소켓)"
if [ -n "$SID" ]; then
  chat_out=$(python3 "$(dirname "$0")/e2e_chat.py" --base "$BASE" --schedule-id "$SID" 2>&1)
  echo "$chat_out" | grep -v "^RESULT" | sed 's/^/  /'
  cp_=$(echo "$chat_out" | sed -n 's/.*RESULT pass=\([0-9]*\).*/\1/p')
  cf_=$(echo "$chat_out" | sed -n 's/.*RESULT.*fail=\([0-9]*\).*/\1/p')
  if [ -n "$cp_" ]; then
    pass=$((pass + cp_)); fail=$((fail + cf_))
  else
    # RESULT 줄이 없으면 도중에 끊긴 것이다. 통과로 세면 안 된다.
    no "채팅 검사가 끝까지 돈다" "RESULT 줄 없음"
  fi
else
  sk "채팅" "일정 저장 실패 — 방을 열 대상이 없다"
fi

sec "12. 재탐색 (다시 짜기)"
if [ -n "$tid" ]; then
  # 재탐색은 만들 때와 같은 본문을 받아 초안을 버리고 다시 짠다. 여기서만
  # 모델을 실제로 부른다 — 캐시로는 "다시 짜기" 를 검증할 수 없다.
  RESEARCH='{"date":{"date_start":"2026-09-05","date_end":"2026-09-05","time_start":"10:00:00","time_end":"18:00:00"},"budget":100000,"theme":["산책"],"mobility":"walk","province":"서울특별시","city":"종로구"}'
  rc=$(code -X POST "$BASE/api/v1/recommend/$tid/research" -H "Content-Type: application/json" \
        "${AUTH[@]}" -d "$RESEARCH")
  case ",202,200,409," in *",$rc,"*) ok "재탐색이 접수된다 ($rc)";; *) no "재탐색이 접수된다" "실제 $rc";; esac
  if [ "$rc" = "202" ] || [ "$rc" = "200" ]; then
    sleep 25
    m=$(docker exec map-service-postgres psql -h 127.0.0.1 -At -U map -d map \
        -c "SELECT count(*) FROM user_service.recommend_jobs WHERE mode='research' AND parent_job_id='$tid';" 2>/dev/null)
    [ "${m:-0}" -gt 0 ] && ok "재탐색 잡이 원본과 이어진다 (parent_job_id)" \
      || no "재탐색 잡이 원본과 이어진다" "0건"
  else
    sk "재탐색 잡이 원본과 이어진다" "접수 안 됨(응답 $rc)"
  fi
else
  sk "재탐색" "생성 단계 실패"
fi

sec "13. 정리 (삭제)"
if [ -n "$SID" ]; then
  expect "저장한 일정을 지운다" "200,204" "$(code -X DELETE "${AUTH[@]}" "$BASE/api/v1/schedules/$SID")"
  expect "지운 일정은 안 보인다" "404" "$(code "${AUTH[@]}" "$BASE/api/v1/schedules/$SID")"
else
  sk "삭제" "저장 실패"
fi

printf '\n\033[1m통과 %d · 실패 %d · 건너뜀 %d\033[0m\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]
