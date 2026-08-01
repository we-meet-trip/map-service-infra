#!/usr/bin/env bash
# 외부 노출만 닫는다. 스택은 그대로 둔다.
#
# 터널을 닫으면 폰에서 오는 길이 끊긴다. 게시된 주소 파일은 그대로 남지만,
# 그 주소로는 아무것도 응답하지 않는다 — 앱은 서버가 없는 것으로 본다.
# 다시 열려면 map-serve.sh 를 실행한다(새 주소가 발급되고 다시 게시된다).
#
# 컨테이너까지 내리려면 map-down.sh.
set -uo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$INFRA_DIR/.serve"
TUNNEL_PID="$STATE_DIR/cloudflared.pid"
CAFFEINATE_PID="$STATE_DIR/caffeinate.pid"
URL_FILE="$STATE_DIR/url.txt"

echo "== [1/2] 터널 종료 =="
closed=0
if [ -f "$TUNNEL_PID" ] && kill "$(cat "$TUNNEL_PID")" 2>/dev/null; then
  echo "  터널 종료(pid $(cat "$TUNNEL_PID"))"
  closed=1
fi
# pid 파일이 없거나 어긋난 경우까지 훑는다(스크립트를 거치지 않고 띄운 경우).
if pkill -f "cloudflared tunnel --url http://localhost:8090" 2>/dev/null; then
  echo "  남은 터널 프로세스 종료"
  closed=1
fi
[ "$closed" -eq 1 ] || echo "  실행 중 터널 없음"

echo "== [2/2] 잠자기 방지 해제 =="
# 터널 pid 를 지켜보게 걸어 둔 것이라 대개 함께 끝난다. 남았으면 직접 정리.
if [ -f "$CAFFEINATE_PID" ] && kill "$(cat "$CAFFEINATE_PID")" 2>/dev/null; then
  echo "  잠자기 방지 해제"
else
  echo "  이미 해제됨"
fi

rm -f "$TUNNEL_PID" "$CAFFEINATE_PID" "$URL_FILE"

echo ""
echo "== ✅ 외부 노출 종료. 스택은 그대로 떠 있다(내리려면 map-down.sh) =="
