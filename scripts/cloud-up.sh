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
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=./.env
FILES=(-f docker-compose.yml)
PROFILES=(--profile full)
LABEL=운영

for arg in "$@"; do
  case "$arg" in
    --test) ENV_FILE=./.env.test; FILES+=(-f docker-compose.test.yml); LABEL=시험 ;;
    --edge) FILES+=(-f docker-compose.edge.yml); PROFILES+=(--profile edge) ;;
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
dc "${PROFILES[@]}" up -d

echo
dc ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
