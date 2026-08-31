#!/usr/bin/env bash
# 시험 스택용 .env.test 를 만든다.
#
# 원본은 반드시 .env.example 이다. .env 를 원본으로 삼으면 실제 발급처 키가
# 시험 스택으로 새어 들어가고, 그러면 시험이 운영의 하루 한도를 대신 태운다.
# 특히 기상청·두루누비 계열은 같은 발급 계정을 쓰므로 한 번 소진되면 운영의
# 예보 폴링과 코스 동기화가 함께 멈춘다. 원본을 example 로 고정하는 것이
# 그 사고를 구조적으로 막는 유일한 방법이라 여기서 선택지를 두지 않는다.
#
# 사용: ./scripts/make-test-env.sh [--force]
set -euo pipefail

cd "$(dirname "$0")/.."
SRC=.env.example
DST=.env.test

[ -f "$SRC" ] || { echo "원본이 없다: $SRC" >&2; exit 1; }

if [ -f "$DST" ] && [ "${1:-}" != "--force" ]; then
  echo "이미 있다: $DST (덮어쓰려면 --force)" >&2
  exit 1
fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cp "$SRC" "$TMP"

# 값 하나를 바꾼다. 키가 없으면 끝에 붙인다.
# sed -i 는 BSD 와 GNU 의 인자 형태가 달라 쓰지 않는다.
set_kv() {
  local key=$1 val=$2 out
  if grep -qE "^${key}=" "$TMP"; then
    out=$(awk -v k="$key" -v v="$val" \
      'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$TMP")
    printf '%s\n' "$out" > "$TMP"
  else
    printf '%s=%s\n' "$key" "$val" >> "$TMP"
  fi
}

# (1) 발급처 키를 전부 비운다. example 이 이미 비어 있어도 방어적으로 다시 비운다 —
#     누군가 템플릿에 실제 값을 적어 넣었을 때 그것이 시험으로 넘어가면 안 된다.
for k in KAKAO_REST_API_KEY KAKAO_MAPS_JS_KEY KAKAO_OAUTH_CLIENT_ID \
         KAKAO_OAUTH_CLIENT_SECRET KMA_SERVICE_KEY AIRKOREA_SERVICE_KEY \
         TOUR_API_SERVICE_KEY NAVER_CLIENT_ID NAVER_CLIENT_SECRET \
         NAVER_MAP_CLIENT_ID NAVER_MAP_CLIENT_ID_FALLBACK GOOGLE_MAPS_API_KEY \
         ODSAY_API_KEY ODSAY_API_KEY_FALLBACK SEOUL_OPENAPI_KEY PM_SERVICE_KEY \
         VISION_GEMINI_API_KEY JWT_PRIVATE_KEY JWT_PUBLIC_KEY; do
  set_kv "$k" ""
done

# (2) 시험 전용 자격증명. 운영과 겹치지 않게 한눈에 보이는 값으로 둔다.
TEST_PW=test-local-only
set_kv POSTGRES_DB   map_test
set_kv POSTGRES_USER map
set_kv POSTGRES_PASSWORD "$TEST_PW"
# 드라이버 이름은 hub 가 실제로 가진 것(psycopg)이어야 한다. 다른 이름을
# 적으면 hub 는 그대로 뜨고 상태 확인도 통과하는데, 예약해 둔 수집 작업만
# 조용히 실패한다.
set_kv HUB_DATABASE_URL   "postgresql+psycopg://map:${TEST_PW}@postgres:5432/map_test"
set_kv MAP_ADMIN_PASSWORD "$TEST_PW"
set_kv ADMIN_DATABASE_URL "postgresql+psycopg://map_admin:${TEST_PW}@postgres:5432/map_test"
set_kv INTERNAL_SERVICE_TOKEN test-internal-token-not-a-real-secret-value
set_kv ADMIN_BOOTSTRAP_PASSWORD "$TEST_PW"
set_kv GF_SECURITY_ADMIN_PASSWORD "$TEST_PW"

# (3) agent 는 이 값이 비면 부팅을 멈춘다. 뜨기는 하되 실제 호출은 거절당하도록
#     한눈에 가짜인 값을 넣는다. 비워 두면 부팅 실패와 구분이 안 된다.
set_kv GEMINI_API_KEY test-not-a-real-key-calls-will-be-rejected

# (4) 외부 호출을 전부 스텁으로 돌린다. 키가 비어도 스텁으로 떨어지지만,
#     그 경우와 의도적으로 끈 경우를 로그에서 가를 수 없어 명시한다.
set_kv PLACES_STUB_MODE true

# (5) 오버레이가 요구하는 표식. 이 값이 없으면 시험 오버레이가 뜨지 않는다.
set_kv MAP_STACK_ENV test

# (6) 서명 열쇠 한 쌍을 그때그때 만들어 넣는다.
#     인증을 켜면 이 값이 비어 있을 때 BFF 가 부팅하지 않는다. 비워 두면
#     인증을 켜 보려는 사람이 매번 손으로 만들어야 하고, 그 번거로움 때문에
#     시험을 인증 없이만 돌리게 된다 — 운영은 켜 두는데 시험은 그 경로를
#     한 번도 지나지 않는 상태가 된다.
#     저장소에 담기지 않는 파일이고 만들 때마다 달라지므로 운영과 섞이지 않는다.
if command -v openssl >/dev/null 2>&1; then
  KEYDIR=$(mktemp -d)
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
    -out "$KEYDIR/k.pem" 2>/dev/null
  openssl rsa -in "$KEYDIR/k.pem" -pubout -out "$KEYDIR/k.pub" 2>/dev/null
  # 설정 파일은 한 줄이어야 하므로 머리말과 줄바꿈을 걷어 낸다.
  set_kv JWT_PRIVATE_KEY "$(grep -v -- '-----' "$KEYDIR/k.pem" | tr -d '\n')"
  set_kv JWT_PUBLIC_KEY  "$(grep -v -- '-----' "$KEYDIR/k.pub" | tr -d '\n')"
  rm -rf "$KEYDIR"
else
  echo "openssl 이 없어 서명 열쇠를 만들지 못했다. 인증을 켜려면 직접 넣는다." >&2
fi

# (7) 인증을 켠 채로 만든다. 운영이 켜고 도는데 시험만 꺼 두면, 토큰을 싣지
#     않는 호출이 시험에서는 통과하고 운영에서만 401 이 된다 — 시험이 잡으라고
#     있는 종류의 어긋남을 시험이 못 잡는다. 켜는 데 필요한 서명 열쇠는 위에서
#     이미 만들었으므로 이 값만으로 부팅한다.
set_kv AUTH_ENFORCED true
set_kv CORS_ALLOWED_ORIGINS "https://test.invalid"

# (7-1) 저장 본문과 통신 본문을 감싸는 열쇠를 그때그때 만든다.
#      본보기 파일의 값은 자리를 보여 주는 용도라 그대로 쓰면 모든 시험
#      환경이 같은 열쇠를 쓰게 된다. 그리고 통신 열쇠가 비어 있으면 감싸기가
#      아예 돌지 않아, 시험이 그 경로를 한 번도 지나지 않는다.
#      두 열쇠를 다른 값으로 두는 이유는 쓰임과 나눠 가지는 상대가 달라서다.
if command -v openssl >/dev/null 2>&1; then
  set_kv LOCATION_ENC_ENABLED true
  set_kv LOCATION_ENC_ACTIVE_KID k1
  set_kv LOCATION_ENC_KEYS "k1:$(openssl rand -base64 32)"
  set_kv LOCATION_WIRE_ENABLED true
  set_kv LOCATION_WIRE_KEY "$(openssl rand -base64 32)"
else
  echo "openssl 이 없어 좌표 열쇠를 만들지 못했다. 직접 넣는다." >&2
fi

# (8) 시험 계정은 시험 스택에서만 켠다.
set_kv TESTER_SEED_ENABLED true
set_kv TESTER_SEED_PASSWORD "$TEST_PW"

mv "$TMP" "$DST"
trap - EXIT
chmod 600 "$DST"
echo "생성: $DST (권한 600)"
