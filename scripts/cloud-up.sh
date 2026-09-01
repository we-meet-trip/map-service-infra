#!/usr/bin/env bash
# 리눅스 서버에서 스택을 순서대로 세운다.
#
# 기존 기동 스크립트들은 맥을 전제한다 — 도커 데스크톱을 열고, 잠들지 않게
# 붙들고, 안드로이드 도구 경로를 본다. 서버에는 그런 것이 없다.
#
# 순서가 중요하다. hub 는 스스로 마이그레이션하지 않는데, 표가 하나도 없는
# 상태에서도 상태 확인에는 정상이라고 답한다. 그래서 이 단계를 빠뜨리면
# 전부 초록으로 뜬 다음 요청을 받을 때 비로소 실패한다. 여기서는 표가
# 실제로 생겼는지까지 보고 넘어간다.
#
# 사용:
#   ./scripts/cloud-up.sh                    운영
#   ./scripts/cloud-up.sh --test             시험(오버레이·시험 환경파일)
#   ./scripts/cloud-up.sh --edge             바깥 노출까지 함께
#   ./scripts/cloud-up.sh --test --micro     메모리 1GB 서버
#   ./scripts/cloud-up.sh --registry         이미지를 만들지 않고 받아 쓴다
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=./.env
FILES=(-f docker-compose.yml)
PROFILES=(--profile full)
LABEL=운영
MICRO=0
PULL=0
ROUTING=0

for arg in "$@"; do
  case "$arg" in
    --test) ENV_FILE=./.env.test; FILES+=(-f docker-compose.test.yml); LABEL=시험 ;;
    # 덧칠 순서가 중요하다. 나중에 붙은 것이 이긴다.
    --micro) FILES+=(-f docker-compose.micro.yml); MICRO=1 ;;
    --registry) FILES+=(-f docker-compose.registry.yml); PULL=1 ;;
    --edge) FILES+=(-f docker-compose.edge.yml); PROFILES+=(--profile edge --profile dns) ;;
    # 경로 엔진을 함께 올린다. 켜지 않으면 hub 가 주소를 못 찾아 구간마다
    # 실패 왕복을 반복하고, 화면에는 도로를 따르지 않는 직선이 그려진다.
    --routing) PROFILES+=(--profile routing); ROUTING=1 ;;
    *) echo "모르는 인자: $arg" >&2; exit 2 ;;
  esac
done

[ -f "$ENV_FILE" ] || { echo "환경파일이 없다: $ENV_FILE" >&2; exit 1; }

dc() { docker compose --env-file "$ENV_FILE" "${FILES[@]}" "$@"; }

# 값이 비면 그 서비스가 부팅하다 멈추는 것들만 미리 본다. 여기서 걸러 내지
# 않으면 컨테이너가 뜨다 죽기를 반복하는 모습으로만 드러난다.
for key in POSTGRES_PASSWORD HUB_DATABASE_URL GEMINI_API_KEY; do
  if ! grep -qE "^${key}=.+" "$ENV_FILE"; then
    echo "$ENV_FILE 에 $key 값이 없다" >&2
    exit 1
  fi
done

# 경로 데이터는 이미지 안이 아니라 따로 만들어 둔 저장 자리에 있다. 없으면
# 엔진이 뜨자마자 죽는데, 그 모습은 다른 기동 실패와 구분되지 않는다.
if [ "$ROUTING" = 1 ]; then
  missing=$(docker run --rm -v osrm-data:/data alpine sh -c \
    'for f in /data/foot/korea.osrm /data/bicycle/korea.osrm; do [ -e "$f" ] || echo "$f"; done' 2>/dev/null)
  if [ -n "$missing" ]; then
    echo "경로 데이터가 없다: $missing" >&2
    echo "먼저 ./scripts/osrm-rebuild.sh 로 만든다. 내려받기와 손질에 시간이 걸린다." >&2
    exit 1
  fi
fi

if [ "$PULL" = 1 ]; then
  echo "[$LABEL] 0/4 이미지 받기"
  # 받는 곳은 네 가지 사정을 모두 같은 글자(denied)로 답한다 — 로그인을 안
  # 했을 때, 토큰이 만료됐을 때, 판 이름을 잘못 적었을 때, 이름이 바뀌었을 때.
  # 그 넷을 구분해 주지 않으므로 여기서 무엇을 봐야 하는지 대신 적어 준다.
  if ! dc "${PROFILES[@]}" pull; then
    echo "이미지를 받지 못했다. 아래를 차례로 본다." >&2
    echo "  1) 이 계정으로 받을 수 있는가 — docker login ghcr.io (패키지가 비공개면 필요하다)" >&2
    echo "  2) 판 이름이 실제로 올라간 이름인가 — $ENV_FILE 의 IMAGE_TAG" >&2
    echo "  3) sudo 로 돌리고 있다면 로그인한 계정과 같은 계정인가" >&2
    exit 1
  fi
fi

echo "[$LABEL] 1/4 저장소 기동"
dc --profile infra up -d

echo "[$LABEL] 2/4 저장소가 실제로 받을 준비가 될 때까지 기다린다"
db_user=$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2-)
db_name=$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2-)
for _ in $(seq 1 60); do
  # -h 를 준다. 이것을 빼면 초기화 중에 잠깐 뜨는 내부 서버에도 응답이 와서,
  # 아직 받을 수 없는 상태를 준비됐다고 읽는다.
  if dc exec -T postgres pg_isready -h 127.0.0.1 -U "$db_user" -d "$db_name" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "[$LABEL] 3/4 hub 표 만들기"
dc run --rm --no-deps --entrypoint alembic hub upgrade head

tables=$(dc exec -T postgres psql -U "$db_user" -d "$db_name" -tAc \
  "select count(*) from information_schema.tables where table_schema='hub_data'" | tr -d '[:space:]')
if [ "${tables:-0}" -lt 1 ]; then
  echo "hub_data 에 표가 없다. 이 상태로 올리면 전부 정상으로 보이다가 요청에서 실패한다" >&2
  exit 1
fi
echo "     hub_data 표 ${tables}개"

echo "[$LABEL] 4/4 애플리케이션 기동"
# 상태가 정상이 될 때까지 기다린다. 기다리지 않으면 표 손질에 실패해 뜨다
# 죽기를 반복하는 상태에서도 이 스크립트가 성공으로 끝나고, 바로 아래 목록은
# 아직 기동 중이라 그 실패와 구분되지 않는다.
if ! dc "${PROFILES[@]}" up -d --wait --wait-timeout 180; then
  echo "정해진 시간 안에 정상이 되지 않았다. 어느 서비스인지 아래에서 보고" >&2
  echo "그 서비스의 기록을 본다: dc logs <서비스>" >&2
  dc ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}' >&2
  exit 1
fi

echo
dc ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}\t{{.Image}}'

if [ "$MICRO" = 1 ]; then
  echo
  echo "메모리가 작은 서버다. 상태가 정상이어도 실제 요청을 한 번 보내 본다 —"
  echo "메모리가 모자라 앱이 죽어도 컨테이너는 살아 있고 자원 한도에 걸린"
  echo "표시도 남지 않아, 겉으로는 정상으로 보인다."
fi
