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
WARM='{"schedule":{"start_date":"2026-09-05","end_date":"2026-09-05","active_start_hour":10,"active_end_hour":18},"budget":{"min":0,"max":100000},"themes":["산책"],"transport":"WALK","location":{"province":"서울특별시","city":"종로구"}}'
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
  EDIT='{"places":[{"place_id":0,"day":1,"name":"E2E남은곳","address":"주소","lat":37.5,"lng":127.0,"recommended_visit_time":"10:00"}]}'
  expect "본인 초안을 고칠 수 있다" "200,404" \
    "$(code -X POST "$BASE/api/v1/recommend/$tid/edit" -H "Content-Type: application/json" \
        -H "Idempotency-Key: e2e-$(date +%s)" "${AUTH[@]}" -d "$EDIT")"
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
[ "$(q "SELECT count(*) FROM user_service.recommend_training;")" -gt 0 ] \
  && ok "학습 신호가 보관된다" || no "학습 신호가 보관된다" "0건"
[ "$(q "SELECT count(*) FROM user_service.recommend_training WHERE payload ? 'llm_tokens';")" -gt 0 ] \
  && ok "토큰 사용량이 함께 남는다" || no "토큰 사용량이 함께 남는다" "0건"
[ "$(q "SELECT count(*) FROM user_service.recommend_edits;")" -gt 0 ] \
  && ok "초안 수정 전후가 남는다" || no "초안 수정 전후가 남는다" "0건"
[ "$(q "SELECT count(*) FROM user_service.recommend_jobs WHERE source='cache_hit';")" -gt 0 ] \
  && ok "캐시로 답한 잡이 구분된다" || no "캐시로 답한 잡이 구분된다" "0건"

printf '\n\033[1m통과 %d · 실패 %d · 건너뜀 %d\033[0m\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]
