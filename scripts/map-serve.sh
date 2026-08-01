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
#   - 앱 쪽 웹 산출물이 만들어져 있어야 한다(map-service-client/build/web).
#     주소 파일을 그 안에 써서 함께 올린다.
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
[ -d "$CLIENT_DIR/build/web" ]   || fail "$CLIENT_DIR/build/web 없음 → 먼저 (cd $CLIENT_DIR && flutter build web --release)"
[ -f "$CLIENT_DIR/.firebaserc" ] || fail "$CLIENT_DIR/.firebaserc 없음 → 배포 프로젝트가 정해지지 않았다"
firebase login:list 2>/dev/null | grep -q "Logged in as" || fail "firebase 미로그인 → firebase login"
mkdir -p "$STATE_DIR"
echo "  ✓ 도구·설정 확인"

echo "== [1/5] 스택 기동 (full) =="
"${COMPOSE[@]}" --profile full up -d 2>&1 | sed 's/^/  /'
[ "${PIPESTATUS[0]}" -eq 0 ] || fail "compose up 실패"

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

# 터널이 실제로 관문까지 닿는지 본다. 주소만 받고 경로가 안 서면, 게시된 뒤
# 폰에서야 실패가 드러난다.
tunnel_ok=0
for _ in $(seq 1 20); do
  [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$base_url/healthz")" = "200" ] && { tunnel_ok=1; break; }
  sleep 2
done
[ "$tunnel_ok" -eq 1 ] || fail "터널 경유 /healthz 실패 — 터널은 떴으나 관문까지 닿지 않는다"
echo "  ✓ 터널 경유 관문 확인"

echo "== [4/5] 앱이 읽는 주소 게시 =="
# 앱은 시작할 때 이 파일 하나만 본다. 파일 위치는 고정, 내용만 매번 바뀐다.
python3 - "$CLIENT_DIR/build/web/app_config.json" "$base_url" <<'PY'
import json, sys, datetime
path, base = sys.argv[1], sys.argv[2]
# 갱신 안내용 자리는 지금 비워 둔다 — 배포 채널이 따로 알림을 보낸다.
doc = {
    "api_base_url": base,
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
