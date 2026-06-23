#!/usr/bin/env bash
# MAP PoC Stage 1: 도커 백엔드 기동(postgres·redis·hub·agent).
# 라이브 추천 경로는 OSRM 을 쓰지 않으므로 --no-deps 로 osrm(대용량 사전빌드)을 제외한다.
# 헬스 게이트 통과 후 종료(컨테이너는 detached 로 계속 실행).
set -uo pipefail

# INFRA_DIR = map-service-infra (compose/.env 소유). 스크립트 위치 기준이라 cwd 무관.
INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml")
ENVF="$INFRA_DIR/.env"

echo "== [0/5] 사전 점검 (Docker 데몬 · .env 키) =="
# Docker 데몬이 꺼져 있으면 이후 모든 compose 호출이 실패하므로 선제 차단.
if ! docker info >/dev/null 2>&1; then
  echo "  ✗ Docker 데몬 꺼짐. Docker Desktop 실행 후 재시도: open -a Docker"; exit 1
fi
[ -f "$ENVF" ] || { echo "  ✗ $ENVF 없음. cp .env.example .env 후 키 입력."; exit 1; }
# 부팅 필수 키만 검사: agent 는 GEMINI_API_KEY 비면 부팅 실패, BFF 는 JWT_SECRET 필요.
for k in POSTGRES_PASSWORD GEMINI_API_KEY JWT_SECRET; do
  v="$(grep -E "^$k=" "$ENVF" | cut -d= -f2-)"
  [ -n "$v" ] || { echo "  ✗ .env 의 $k 비어 있음(필수)."; exit 1; }
done
echo "  ✓ Docker 가동·필수 키 존재"

echo "== [1/5] postgres · redis 기동 =="
"${COMPOSE[@]}" up -d postgres redis

# 마이그레이션은 DB 가 연결을 수락한 뒤에만 안전하므로 postgres 준비를 명시적으로 대기.
echo "  postgres 준비 대기..."
pg_ok=0
for i in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T postgres pg_isready -U map -d map >/dev/null 2>&1; then pg_ok=1; break; fi
  sleep 2
done
[ "$pg_ok" = 1 ] || { echo "  ✗ postgres 준비 타임아웃"; exit 1; }
echo "  ✓ postgres ready"

echo "== [2/5] hub · agent 이미지 확인/빌드 =="
# 이미지가 없을 때만 빌드(있으면 생략해 반복 기동을 빠르게).
if ! docker image inspect map-hub >/dev/null 2>&1 || ! docker image inspect map-agent >/dev/null 2>&1; then
  echo "  이미지 빌드(최초 1회)..."; "${COMPOSE[@]}" build hub agent
else
  echo "  ✓ 이미지 존재"
fi

echo "== [3/5] hub_data 마이그레이션(시드) =="
# region_grid 가 비면 hub /v1/weather 가 404 → agent 잡 중단. 미시드일 때만 alembic 실행.
seeded="$("${COMPOSE[@]}" exec -T postgres psql -U map -d map -tAc \
  "SELECT count(*) FROM hub_data.region_grid" 2>/dev/null | tr -d '[:space:]')"
if [[ "${seeded:-x}" =~ ^[0-9]+$ ]] && [ "$seeded" -ge 1 ]; then
  echo "  ✓ 이미 시드됨(region_grid=$seeded)"
else
  # hub ENTRYPOINT 가 uvicorn 이라 그대로 두면 'uvicorn ... alembic upgrade head' 로
  # 합쳐져 실패한다 → --entrypoint alembic 로 덮어써 'alembic upgrade head' 단독 실행.
  echo "  마이그레이션: alembic upgrade head"
  "${COMPOSE[@]}" run --rm --no-deps --entrypoint alembic hub upgrade head
fi

echo "== [4/5] hub · agent 기동 (OSRM 제외: --no-deps) =="
# hub/agent 는 compose 상 osrm-foot/bicycle 에 depends_on 되어 있으나 라이브 경로에서
# OSRM 을 호출하지 않으므로, --no-deps 로 osrm 기동(대용량 데이터 필요)을 건너뛴다.
"${COMPOSE[@]}" up -d --no-deps hub agent

echo "== [5/5] 헬스 체크 =="
fail=0
for pair in "hub:8001" "agent:8000"; do
  name="${pair%%:*}"; port="${pair##*:}"; ok=0
  for i in $(seq 1 20); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/health")" = "200" ]; then
      echo "  ✓ $name (:$port) 200"; ok=1; break
    fi
    sleep 3
  done
  if [ "$ok" != 1 ]; then
    echo "  ✗ $name (:$port) 헬스 실패. 로그: docker compose -f \"$INFRA_DIR/docker-compose.yml\" logs $name"
    fail=1
  fi
done
[ "$fail" = 0 ] || exit 1

echo ""
echo "== ✅ 백엔드 준비 완료. 다음(별도 터미널): map-up-2-bff.sh =="
