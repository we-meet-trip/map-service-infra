#!/usr/bin/env bash
# infra 의 .env 에서 앱이 쓰는 값만 골라 map-service-client/.env 를 만든다.
#
# 앱은 compose 로 뜨지 않아 여기 .env 를 읽지 못한다. 자기 레포의 .env 를 자산으로
# 싣고 시작할 때 그 파일을 읽는데, 그러다 보니 같은 값이 두 곳에 따로 적히고
# 한쪽만 바뀌는 일이 생긴다. 값을 적는 자리는 infra 한 곳으로 두고, 앱 쪽 파일은
# 여기서 만들어 낸다.
#
# 옮기는 값은 앱이 자기 손으로 외부에 보내야만 하는 것에 한정한다. 서버가 대신
# 부를 수 있는 키는 옮기지 않는다 — 그런 키는 앱 꾸러미에 실리는 순간 꺼내
# 볼 수 있게 된다.
#
# 인식 서버 주소는 개발자마다 달라 infra 에 두지 않는다. 이미 있는 파일의 값을
# 그대로 살리고, 없으면 환경변수로 받는다.
set -uo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAP_ROOT="$(cd "$INFRA_DIR/.." && pwd)"
SRC="$INFRA_DIR/.env"
DST="$MAP_ROOT/map-service-client/.env"

fail() { echo "✗ $*"; exit 1; }

# infra .env 에서 이름이 정확히 일치하는 줄의 값만 꺼낸다. 주석과 뒤쪽 공백을
# 걷어내지 않으면 값에 섞여 그대로 앱으로 넘어간다.
read_env() {
  grep -E "^$1=" "$SRC" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r'
}

[ -f "$SRC" ] || fail "$SRC 없음"
[ -d "$(dirname "$DST")" ] || fail "$(dirname "$DST") 없음"

# 지도 식별자. 이름이 비슷한 NAVER_CLIENT_ID 는 검색 API 키라 지도에 쓰면
# 타일 인증이 실패한다. 이름을 정확히 맞춰 읽는다.
#
# 두 개를 함께 내려보낸다. 앱은 앞의 것으로 먼저 붙고 인증이 막히면 뒤의 것으로
# 한 번 더 시도한다. 발급처는 등록 도메인·패키지와 요청이 맞지 않으면 인증을
# 거절하는데, 그 판정이 화면을 열기 전까지 드러나지 않아 잘못된 값이 오래
# 남기 쉽다. 어느 쪽이 맞는지는 열어 봐야 알 수 있으므로 둘 다 준다.
naver_map_id="$(read_env NAVER_MAP_CLIENT_ID)"
naver_map_id_fallback="$(read_env NAVER_MAP_CLIENT_ID_FALLBACK)"
# 주소 검색. 앱이 발급처를 직접 부르므로 여기 값이 앱 꾸러미에 실린다.
kakao_rest_key="$(read_env KAKAO_REST_API_KEY)"

# 인식 서버 주소: 환경변수 > 기존 파일 > 빈 값. 비어 있으면 앱이 서버 주소에서
# 스스로 만들어 쓰므로 비워 두어도 동작한다.
vision_host="${VISION_SERVER_HOST:-}"
if [ -z "$vision_host" ] && [ -f "$DST" ]; then
  vision_host="$(grep -E '^VISION_SERVER_HOST=' "$DST" | head -1 | cut -d= -f2- | tr -d '\r')"
fi

[ -n "$naver_map_id" ] || echo "  ! NAVER_MAP_CLIENT_ID 가 비어 있다 — 지도 타일이 인증에 실패한다"
[ -n "$kakao_rest_key" ] || echo "  ! KAKAO_REST_API_KEY 가 비어 있다 — 주소 검색이 동작하지 않는다"

# 이미 있는 파일과 값이 달라지는 키가 있으면 멈춘다.
#
# 두 파일에 같은 이름의 키가 서로 다른 값으로 들어 있는 경우가 실제로 있었다.
# 그때 말없이 덮으면 동작하던 값이 검증되지 않은 값으로 바뀌는데, 화면을 열어
# 보기 전까지 티가 나지 않아 한참 뒤에야 발견된다. 어느 쪽이 맞는지는 사람이
# 정해야 하므로 여기서는 알리고 멈춘다.
#
# 지도 식별자는 이 검사에서 뺀다. 순서가 정해져 있어(infra 가 먼저, 예비가
# 뒤) 덮어써도 이전 값이 사라지지 않고 예비 자리로 남기 때문이다.
if [ -f "$DST" ] && [ "${FORCE:-0}" != "1" ]; then
  # 지금 대조하는 것은 한 개다. 늘어나면 그때 목록으로 바꾼다.
  changed=""
  name=KAKAO_REST_API_KEY
  prev="$(grep -E "^$name=" "$DST" | head -1 | cut -d= -f2- | tr -d '\r')"
  # 아직 없던 키를 채우는 것은 덮어쓰기가 아니다.
  if [ -n "$prev" ] && [ "$prev" != "$kakao_rest_key" ]; then
    changed=" $name"
  fi
  if [ -n "$changed" ]; then
    echo "✗ 두 파일의 값이 다르다:$changed"
    echo "  infra 값으로 덮으면 지금 동작하는 값이 바뀐다. 어느 쪽이 맞는지"
    echo "  확인한 뒤 map-service-infra/.env 를 맞추고 다시 실행한다."
    echo "  확인을 마쳤고 그래도 덮으려면 FORCE=1 을 붙여 실행한다."
    exit 1
  fi
fi

# 덮어쓰기 전에 한 벌 남긴다. 손으로 적어 둔 값이 있으면 여기서 되찾는다.
if [ -f "$DST" ]; then
  cp -p "$DST" "$DST.synced-backup" || fail "기존 파일 백업 실패"
fi

cat > "$DST" <<EOF || fail "$DST 쓰기 실패"
# map-service-infra/scripts/sync-client-env.sh 가 만든 파일이다. 손으로 고치면
# 다음 실행에서 지워진다. 값은 map-service-infra/.env 에서 고친다.
#
# NAVER_MAP_CLIENT_ID : 지도 타일 인증에 쓰는 식별자. 지도를 띄우려면 브라우저나
#   앱이 이 값을 직접 보내야 해서 감출 수 없다. 방어는 발급처에 등록한 도메인·
#   패키지 목록이 한다.
# NAVER_MAP_CLIENT_ID_FALLBACK : 위 값으로 인증이 막혔을 때 한 번 더 써 보는 값.
NAVER_MAP_CLIENT_ID=$naver_map_id
NAVER_MAP_CLIENT_ID_FALLBACK=$naver_map_id_fallback
# KAKAO_REST_API_KEY : 주소 검색. 앱이 발급처를 직접 부르는 마지막 키다.
KAKAO_REST_API_KEY=$kakao_rest_key
# VISION_SERVER_HOST : 카메라 인식 서버 주소(호스트:포트). 비워 두면 앱이 서버
#   주소에서 만들어 쓰고 관문을 거친다.
VISION_SERVER_HOST=$vision_host
EOF

echo "✓ $DST 생성"
echo "  채운 키: $(grep -cE '^[A-Z_]+=' "$DST") 개"
[ -f "$DST.synced-backup" ] && echo "  이전 파일: $DST.synced-backup"
exit 0
