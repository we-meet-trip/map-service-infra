#!/usr/bin/env bash
# 실기기에서 앱을 쓸 수 있게, 이 맥의 스택을 바깥 주소로 연다.
#
# 하는 일: 스택 기동 → 관문 확인 → 터널 개통 → 새 주소를 앱이 읽는 곳에 게시.
# 마지막 단계까지 마쳐야 폰의 앱이 이 맥을 찾아온다. 앱은 시작할 때마다 그
# 주소를 읽으므로, 터널 주소가 바뀌어도 앱을 다시 깔 필요가 없다.
#
# 전제:
#   - BFF 가 컨테이너로 떠 있어야 한다(full 프로파일). 관문이 컨테이너 이름으로
#     BFF 를 찾기 때문에, 호스트에서 BFF 를 직접 띄우는 흐름과는 같이 못 쓴다.
#   - 주소 파일은 앱 저장소의 hosting 디렉터리에 쓴다. 그 디렉터리에는 주소
#     파일과 링크 검증 파일만 둔다 — 웹 빌드 산출물을 통째로 올리면 앱 설정
#     자산까지 공개 주소로 따라 올라간다.
#   - 도로를 따라가는 경로를 쓰려면 경로 그래프가 미리 만들어져 있어야 한다
#     (scripts/osrm-rebuild.sh, 최초 1회). 없어도 나머지는 그대로 열리고
#     화면의 경로만 두 점을 잇는 직선이 된다.
#
# 알려진 한계:
#   - 터널 주소는 열 때마다 달라진다. 그래서 이 스크립트가 매번 다시 게시한다.
#   - 터널은 원본 응답을 120초까지만 기다린다. 그보다 오래 걸리는 요청은 끊긴다.
#   - 주소를 아는 사람은 누구나 닿는다. 그래서 관문에서 경로를 목록으로 막고
#     생성 요청에 호출 상한을 건다.
#
# 정리는 map-serve-down.sh.
set -uo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAP_ROOT="$(cd "$INFRA_DIR/.." && pwd)"
CLIENT_DIR="$MAP_ROOT/map-service-client"
STATE_DIR="$INFRA_DIR/.serve"
TUNNEL_LOG="$STATE_DIR/cloudflared.log"
TUNNEL_PID="$STATE_DIR/cloudflared.pid"
CAFFEINATE_PID="$STATE_DIR/caffeinate.pid"
URL_FILE="$STATE_DIR/url.txt"
COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml")

fail() { echo "✗ $*"; exit 1; }

echo "== [0/5] 준비 확인 =="
command -v cloudflared >/dev/null || fail "cloudflared 없음 → brew install cloudflared"
command -v firebase >/dev/null   || fail "firebase CLI 없음 → npm i -g firebase-tools"
docker info >/dev/null 2>&1      || fail "도커 데몬 미기동"
[ -f "$INFRA_DIR/.env" ]         || fail "$INFRA_DIR/.env 없음"
[ -d "$CLIENT_DIR/hosting" ]     || fail "$CLIENT_DIR/hosting 없음 → 게시할 디렉터리가 없다"
[ -f "$CLIENT_DIR/.firebaserc" ] || fail "$CLIENT_DIR/.firebaserc 없음 → 배포 프로젝트가 정해지지 않았다"
firebase login:list 2>/dev/null | grep -q "Logged in as" || fail "firebase 미로그인 → firebase login"
mkdir -p "$STATE_DIR"
echo "  ✓ 도구·설정 확인"

echo "== [1/5] 스택 기동 (full + vision + routing) =="
# vision 을 함께 띄운다. 폰에서 카메라 인식을 쓰려면 관문의 /ws/vision 뒤에
# 그 서비스가 있어야 한다.
#
# --build 를 붙인다. 이것 없이 띄우면 예전에 만들어 둔 이미지가 그대로 떠서,
# 고친 코드가 반영되지 않은 채 밖으로 열린다. 그 상태는 겉으로 드러나지
# 않는다 — 컨테이너는 정상이고 로그도 조용하다. 바뀐 것이 없으면 도커가
# 캐시로 즉시 끝내므로 반복 기동이 느려지지도 않는다.
"${COMPOSE[@]}" --profile full --profile vision up -d --build 2>&1 | sed 's/^/  /'
[ "${PIPESTATUS[0]}" -eq 0 ] || fail "compose up 실패"

# hub 의 스키마를 최신까지 올린다. BFF 는 자기 스키마를 스스로 올리지만
# hub 는 그러지 않아, 새 revision 이 생긴 뒤 이 길로만 띄우면 hub 가 없는
# 테이블을 찾는다. alembic 은 멱등해서 이미 올라간 것은 그냥 지나간다.
echo "  hub 스키마..."
"${COMPOSE[@]}" run --rm --no-deps --entrypoint alembic hub upgrade head \
  2>&1 | sed 's/^/  /'
[ "${PIPESTATUS[0]}" -eq 0 ] || fail "hub 마이그레이션 실패"

# 경로 엔진은 명령을 따로 낸다. 이 엔진은 미리 만들어 둔 그래프 파일을 읽어야
# 뜨는데, 그 파일이 없는 환경에서는 이 기동만 실패할 수 있다. 위 명령에 프로파일을
# 하나 더 얹어 한 줄로 합치면 그 실패가 명령 전체의 실패가 되어, 앞의 여섯
# 컨테이너까지 열리지 않는다. 그래서 떼어 두고 여기서 실패해도 멈추지 않는다 —
# 경로만 직선이 될 뿐 나머지 기능은 그대로다.
#
# 서비스 이름을 직접 적으면 그 둘만 다루므로, 이미 떠 있는 다른 컨테이너를
# 건드릴 여지가 없다.
echo "  경로 엔진..."
routing_up=0
"${COMPOSE[@]}" --profile routing up -d osrm-foot osrm-bicycle 2>&1 | sed 's/^/  /'
[ "${PIPESTATUS[0]}" -eq 0 ] && routing_up=1

echo "== [2/5] 관문·BFF 준비 대기 =="
# 관문은 백엔드와 무관한 정적 응답을 준다. 이것만으로는 BFF 준비를 알 수 없어
# BFF 상태도 따로 본다. BFF 는 마이그레이션까지 마치고 나서야 UP 이 된다.
ready=0
for _ in $(seq 1 60); do
  gate="$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/healthz)"
  bff="$(curl -s -m 3 http://127.0.0.1:8080/actuator/health | grep -o '"status":"UP"' | head -1)"
  if [ "$gate" = "200" ] && [ -n "$bff" ]; then ready=1; break; fi
  sleep 3
done
[ "$ready" -eq 1 ] || fail "관문(8090) 또는 BFF(8080) 미준비 — ${COMPOSE[*]} logs user proxy 확인"
echo "  ✓ 관문 200 / BFF UP"

# 카메라 인식은 부가 기능이라 준비되지 않아도 노출을 막지 않는다. 다만 폰에서
# 그 기능만 조용히 실패하는 상황을 피하려고 상태를 알려 준다.
vision_ok=0
for _ in $(seq 1 15); do
  [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8004/health)" = "200" ] && { vision_ok=1; break; }
  sleep 2
done
if [ "$vision_ok" -eq 1 ]; then
  echo "  ✓ 카메라 인식(8004) 200"
else
  echo "  · 카메라 인식(8004) 미준비 — 앱의 카메라 기능만 실패한다(나머지는 정상)."
  echo "    로그: ${COMPOSE[*]} logs yolo"
fi

# 도로를 따라가는 경로는 이 엔진이 만든다. 엔진이 없으면 화면의 경로가 두 점을
# 잇는 직선이 되는데, 앱은 그 상태로도 끝까지 동작해 겉으로는 정상처럼 보인다.
# 그래서 노출을 막지는 않되 상태만은 분명히 알려 준다.
#
# 준비 판정은 좌표를 도로에 붙여 보는 조회로 한다. 이 조회는 그래프를 실제로
# 읽어야 답할 수 있어, 포트만 열린 상태와 경로를 낼 수 있는 상태를 갈라 준다.
# 뿌리 경로는 그래프 없이도 답을 주므로 판정에 쓸 수 없다.
#
# 두 엔진을 한 루프에서 함께 본다. 따로 기다리면 둘 다 없는 환경에서 대기가
# 두 배가 된다. 기동 자체가 실패했으면 기다릴 이유가 없어 건너뛴다.
osrm_probe() {
  curl -s -m 3 "http://127.0.0.1:$1/nearest/v1/$2/126.9780,37.5665" \
    | grep -q '"code":"Ok"'
}
foot_ok=0
bike_ok=0
if [ "$routing_up" -eq 1 ]; then
  for _ in $(seq 1 15); do
    [ "$foot_ok" -eq 1 ] || { osrm_probe 5000 foot    && foot_ok=1; }
    [ "$bike_ok" -eq 1 ] || { osrm_probe 5001 bicycle && bike_ok=1; }
    [ "$foot_ok" -eq 1 ] && [ "$bike_ok" -eq 1 ] && break
    sleep 2
  done
fi
if [ "$foot_ok" -eq 1 ] && [ "$bike_ok" -eq 1 ]; then
  echo "  ✓ 경로 엔진(5000·5001) 응답"
else
  echo "  · 경로 엔진 미준비(도보=$foot_ok 자전거=$bike_ok) — 경로가 직선으로 그려진다."
  echo "    그래프를 만든 적이 없으면(최초 1회, 오래 걸린다):"
  echo "      $INFRA_DIR/scripts/osrm-rebuild.sh"
  echo "    로그: ${COMPOSE[*]} logs osrm-foot osrm-bicycle"
fi

# 엔진이 떠 있어도 hub 쪽 주소가 비어 있으면 hub 는 엔진을 부르지 않고 자체
# 대체 경로를 쓴다. 그 경로는 직선이 아니라 몇 점 꺾인 모양이라 도로를 따라간
# 것처럼 보이므로, 값이 비었는지 여기서 미리 짚어 준다.
osrm_env_missing=0
for k in OSRM_FOOT_BASE_URL OSRM_BICYCLE_BASE_URL; do
  [ -n "$(grep -E "^$k=" "$INFRA_DIR/.env" | cut -d= -f2-)" ] || osrm_env_missing=1
done
if [ "$osrm_env_missing" -eq 1 ]; then
  echo "  · .env 의 OSRM_FOOT_BASE_URL / OSRM_BICYCLE_BASE_URL 이 비어 있다 —"
  echo "    엔진이 떠 있어도 호출되지 않는다. 각각 http://osrm-foot:5000 ·"
  echo "    http://osrm-bicycle:5000 을 채우고 ${COMPOSE[*]} restart hub"
fi

echo "== [3/5] 터널 개통 =="
# 앞서 띄운 터널이 남아 있으면 주소가 둘이 되어 어느 쪽이 게시됐는지 흐려진다.
if [ -f "$TUNNEL_PID" ]; then kill "$(cat "$TUNNEL_PID")" 2>/dev/null; rm -f "$TUNNEL_PID"; fi
pkill -f "cloudflared tunnel --url http://localhost:8090" 2>/dev/null
: > "$TUNNEL_LOG"
# 주소는 로그로만 알려 준다. 표준오류로 나오므로 함께 받는다.
nohup cloudflared tunnel --url http://localhost:8090 >>"$TUNNEL_LOG" 2>&1 &
tunnel_pid=$!
echo "$tunnel_pid" > "$TUNNEL_PID"

base_url=""
for _ in $(seq 1 30); do
  kill -0 "$tunnel_pid" 2>/dev/null || fail "터널 프로세스 조기 종료 — $TUNNEL_LOG 확인"
  base_url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)"
  [ -n "$base_url" ] && break
  sleep 1
done
[ -n "$base_url" ] || fail "터널 주소를 못 읽었다 — $TUNNEL_LOG 확인"
echo "$base_url" > "$URL_FILE"
echo "  ✓ $base_url"

# 이름이 실제로 풀릴 때까지 먼저 기다린다. 주소를 받자마자 물으면 아직 없는
# 이름이라는 답을 받는데, 맥은 그 답을 한동안 들고 있어 이후 조회까지 전부
# 막힌다.
#
# 두 갈래로 물어본다. 먼저 맥 자체 해석기 — 앱과 브라우저가 쓰는 것과 같은
# 길이라, 여기서 답이 나오면 실제로 닿는다는 뜻이다. 그게 비면 공개 해석기에
# 직접 묻는다. 조회 도구가 시스템 설정을 그대로 못 쓰는 경우가 있어서다.
# 공유기가 알려 주는 이름 서버가 링크로컬 주소이면 dig 는 그 서버에 질의하지
# 못하고 조용히 빈 답을 준다 — 이름은 멀쩡히 풀리는데 확인만 실패한다.
resolve_tunnel_ip() {
  local host="$1" ip=""
  ip="$(python3 -c '
import socket, sys
try:
    print(socket.getaddrinfo(sys.argv[1], 443, socket.AF_INET)[0][4][0])
except Exception:
    pass
' "$host" 2>/dev/null)"
  if [ -n "$ip" ]; then echo "$ip"; return; fi
  for ns in 1.1.1.1 8.8.8.8; do
    ip="$(dig +short "$host" A "@$ns" 2>/dev/null | grep -E '^[0-9.]+$' | head -1)"
    if [ -n "$ip" ]; then echo "$ip"; return; fi
  done
}

tunnel_host="${base_url#https://}"
tunnel_ip=""
for _ in $(seq 1 30); do
  tunnel_ip="$(resolve_tunnel_ip "$tunnel_host")"
  [ -n "$tunnel_ip" ] && break
  sleep 2
done
[ -n "$tunnel_ip" ] || fail "터널 이름이 풀리지 않는다($tunnel_host)"

# 터널이 실제로 관문까지 닿는지 본다. 주소만 받고 경로가 안 서면, 게시된 뒤
# 폰에서야 실패가 드러난다. 이름은 위에서 얻은 값으로 직접 지정한다 — 맥에
# 남아 있을 수 있는 옛 답과 무관하게 확인하기 위해서다(폰은 제 나름의 조회를
# 하므로 이 확인은 이 맥에서만 필요한 우회다).
tunnel_ok=0
for _ in $(seq 1 20); do
  code="$(curl -s -m 5 --resolve "$tunnel_host:443:$tunnel_ip" \
    -o /dev/null -w '%{http_code}' "$base_url/healthz")"
  [ "$code" = "200" ] && { tunnel_ok=1; break; }
  sleep 2
done
[ "$tunnel_ok" -eq 1 ] || fail "터널 경유 /healthz 실패 — 터널은 떴으나 관문까지 닿지 않는다"
echo "  ✓ 터널 경유 관문 확인 ($tunnel_ip)"

echo "== [4/5] 앱이 읽는 주소 게시 =="
# 앱은 시작할 때 이 파일 하나만 본다. 파일 위치는 고정, 내용만 매번 바뀐다.
#
# 지도 식별자도 함께 싣는다. 앱이 아닌 정적 페이지(web/pm_map.html)는 자기
# 설정 파일을 갖지 않아 값을 적을 자리가 없는데, 소스에 적어 두면 공개
# 저장소에 값이 들어간다. 이 식별자는 브라우저가 어차피 밖으로 내보내는
# 값이고 방어는 발급처의 도메인 목록이 하므로, 여기서 내려 주고 페이지는
# 받아 쓰기만 한다.
#
# 예비 식별자도 함께 싣는다. 페이지는 앞의 것으로 먼저 붙고 인증이 막히면
# 뒤의 것으로 한 번 더 시도한다.
naver_map_id="$(grep -E '^NAVER_MAP_CLIENT_ID=' "$INFRA_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r')"
naver_map_id_alt="$(grep -E '^NAVER_MAP_CLIENT_ID_FALLBACK=' "$INFRA_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r')"
python3 - "$CLIENT_DIR/hosting/app_config.json" "$base_url" "$naver_map_id" "$naver_map_id_alt" <<'PY'
import json, sys, datetime
path, base, naver_map_id, naver_map_id_alt = sys.argv[1:5]
# 갱신 안내용 자리는 지금 비워 둔다 — 배포 채널이 따로 알림을 보낸다.
doc = {
    "api_base_url": base,
    "naver_map_client_id": naver_map_id,
    "naver_map_client_id_fallback": naver_map_id_alt,
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "latest_version": "",
    "latest_build_number": 0,
    "apk_url": "",
    "notice": "",
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
[ $? -eq 0 ] || fail "주소 파일 작성 실패"

( cd "$CLIENT_DIR" && firebase deploy --only hosting ) 2>&1 | tail -6 | sed 's/^/  /'
[ "${PIPESTATUS[0]}" -eq 0 ] || fail "호스팅 배포 실패"

echo "== [5/5] 게시 결과 확인 =="
# 배포는 끝났다고 하는데 옛 주소가 돌아오는 경우가 있다. 실제로 읽어서 맞춘다.
config_url="$(python3 -c "
import json,sys
with open('$CLIENT_DIR/.firebaserc') as f: print('https://%s.web.app/app_config.json' % json.load(f)['projects']['default'])
")"
published=""
for _ in $(seq 1 10); do
  published="$(curl -s -m 5 "$config_url" | python3 -c "import json,sys; print(json.load(sys.stdin).get('api_base_url',''))" 2>/dev/null)"
  [ "$published" = "$base_url" ] && break
  sleep 3
done
[ "$published" = "$base_url" ] || fail "게시된 주소 불일치(게시=$published, 기대=$base_url)"
echo "  ✓ $config_url → $published"

# 맥이 잠들면 터널도 끊긴다. 터널이 살아 있는 동안만 잠자기를 막는다.
caffeinate -w "$tunnel_pid" >/dev/null 2>&1 &
echo "$!" > "$CAFFEINATE_PID"

echo ""
echo "== ✅ 외부 노출 준비 완료 =="
echo "   서버 주소 : $base_url"
echo "   앱이 읽는 곳: $config_url"
echo "   폰에서 앱을 다시 시작하면 이 주소를 잡는다(재설치 불필요)."
echo "   정리: $INFRA_DIR/scripts/map-serve-down.sh"
