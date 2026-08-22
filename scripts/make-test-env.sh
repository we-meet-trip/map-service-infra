#!/usr/bin/env bash
# 시험 스택이 쓸 .env.test 를 만든다.
#
# 왜 스크립트인가:
#   .env.example 을 통째로 복사한 두 번째 템플릿을 두면 키가 늘 때마다 한쪽만
#   갱신되어 조용히 어긋난다. 원본 하나를 두고 필요한 값만 갈아 끼운다.
#
# 왜 .env 가 아니라 .env.example 에서 만드는가:
#   .env 에는 실제 발급처 키가 있다. 그것을 시험 쪽으로 복사하면 시험이
#   운영 쿼터를 태운다. 특히 KMA 와 두루누비는 같은 data.go.kr 키를 쓰므로,
#   시험이 하루 한도를 소진하면 운영의 예보 폴링과 코스 동기화가 함께 멈춘다.
#   출처를 .env.example 로 못박아 두면 그 사고가 구조적으로 불가능해진다.
#
# 발급처 키는 전부 비운다. 시험에서 외부를 부르는 경로는 실패한다 —
# 이는 의도한 것이다. 조용히 운영 쿼터를 쓰는 것보다 낫다.
# 외부 경로까지 시험하려면 별도 발급 계정을 채워 넣어야 한다.
#
# 주의: Gemini 한도는 키가 아니라 프로젝트 단위다. 같은 프로젝트에서 키만 새로
# 발급해도 한도는 나뉘지 않는다. 나누려면 프로젝트를 따로 파야 한다.
#
# 사용: ./scripts/make-test-env.sh [--force]
set -euo pipefail

cd "$(dirname "$0")/.."

SRC=".env.example"
OUT=".env.test"

[ -f "$SRC" ] || { echo "✗ $SRC 가 없다"; exit 1; }

if [ -f "$OUT" ] && [ "${1:-}" != "--force" ]; then
  echo "✗ $OUT 이 이미 있다. 덮어쓰려면 --force"
  exit 1
fi

# 발급처 키 — 시험에서는 전부 빈 값으로 둔다.
PROVIDER_KEYS=(
  KAKAO_OAUTH_CLIENT_ID KAKAO_OAUTH_CLIENT_SECRET KAKAO_REST_API_KEY KAKAO_MAPS_JS_KEY
  KMA_SERVICE_KEY AIRKOREA_SERVICE_KEY TOUR_API_SERVICE_KEY
  NAVER_CLIENT_ID NAVER_CLIENT_SECRET NAVER_MAP_CLIENT_ID NAVER_MAP_CLIENT_ID_FALLBACK
  GOOGLE_MAPS_API_KEY VISION_GEMINI_API_KEY
  ODSAY_API_KEY ODSAY_API_KEY_FALLBACK SEOUL_OPENAPI_KEY PM_SERVICE_KEY
)

# 시험 전용으로 값을 갈아 끼울 항목.
declare -a OVERRIDES=(
  "POSTGRES_PASSWORD=test-local-only"
  "MAP_ADMIN_PASSWORD=test-local-only"
  "INTERNAL_SERVICE_TOKEN=test-internal-token-not-for-production"
  "AUTH_ENFORCED=false"
  "TESTER_SEED_ENABLED=true"
  # 비워 두면 agent 가 부팅을 거부한다(키 없이는 뜨지 않는 설계). 그렇다고
  # 운영 키를 넣으면 시험이 운영 한도를 태운다. 누가 봐도 가짜인 값을 주어
  # 부팅은 되게 하고, 실제 호출은 발급처에서 거절되게 한다 — 조용히 통과하는
  # 것보다 그 자리에서 막히는 편이 낫다.
  "GEMINI_API_KEY=test-not-a-real-key-calls-will-be-rejected"
  # 접속 문자열 안의 비밀번호는 위 POSTGRES_PASSWORD 와 같아야 한다. 한쪽만
  # 갈면 hub 와 admin 이 인증에서 막히는데, 그 실패는 부팅 로그를 봐야만
  # 드러난다.
  "HUB_DATABASE_URL=postgresql+psycopg://map:test-local-only@postgres:5432/map"
  "ADMIN_DATABASE_URL=postgresql+psycopg://map_admin:test-local-only@postgres:5432/map"
)

cp "$SRC" "$OUT"

# 제자리 편집(sed -i)은 쓰지 않는다. BSD 와 GNU 의 인자 형태가 달라
# 개발 기계(macOS)에서 되는 것이 검사 기계(리눅스)에서 깨진다.
# 임시 파일에 쓰고 옮기는 방식은 양쪽에서 같게 동작한다.
replace_line() {
  local key="$1" line="$2" tmp="${OUT}.tmp"
  sed -E "s|^${key}=.*|${line}|" "$OUT" > "$tmp" && mv "$tmp" "$OUT"
}

# 발급처 키 비우기.
for key in "${PROVIDER_KEYS[@]}"; do
  if grep -qE "^${key}=" "$OUT"; then
    replace_line "$key" "${key}="
  fi
done

# 시험 값으로 교체(없는 키는 뒤에 덧붙인다).
for pair in "${OVERRIDES[@]}"; do
  key="${pair%%=*}"
  if grep -qE "^${key}=" "$OUT"; then
    replace_line "$key" "$pair"
  else
    printf '%s\n' "$pair" >> "$OUT"
  fi
done

# .env.example 에 없는 시험 전용 스위치.
{
  printf '\n'
  printf '# --- 시험 스택 전용 (make-test-env.sh 가 덧붙임) ---\n'
  printf '# 장소 조회를 외부 대신 고정 응답으로 돌린다. 지금 스텁이 있는 경로는\n'
  printf '# 이것 하나뿐이다 — 날씨·리뷰·경로·사진·지하철·따릉이·킥보드는 스텁이\n'
  printf '# 없어 키가 비면 그 경로가 실패한다.\n'
  printf 'PLACES_STUB_MODE=true\n'
} >> "$OUT"

chmod 600 "$OUT"

echo "✓ $OUT 생성"
echo "  발급처 키 ${#PROVIDER_KEYS[@]}개 비움 · 시험 값 ${#OVERRIDES[@]}건 교체 · PLACES_STUB_MODE=true"
echo "  남은 발급처 키(비어 있어야 정상):"
for key in "${PROVIDER_KEYS[@]}"; do
  value="$(grep -E "^${key}=" "$OUT" | head -1 | cut -d= -f2- || true)"
  [ -n "$value" ] && echo "    ✗ ${key} 가 비어 있지 않다"
done
echo "  (위에 아무 것도 안 나오면 전부 비어 있다)"
