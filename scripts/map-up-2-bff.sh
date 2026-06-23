#!/usr/bin/env bash
# MAP PoC Stage 2: user-BFF 를 호스트 JVM 으로 실행(gradlew bootRun, 포그라운드).
# 도커 백엔드의 "호스트 포트"(agent:8000/hub:8001/pg:5432/redis:16379)에 연결한다.
set -uo pipefail

# INFRA_DIR = map-service-infra(.env 소유). MAP_ROOT = 그 상위(형제 레포 map-service-user 접근용).
INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAP_ROOT="$(cd "$INFRA_DIR/.." && pwd)"
ENVF="$INFRA_DIR/.env"
[ -f "$ENVF" ] || { echo "✗ $ENVF 없음"; exit 1; }

# 8080 선점 시 중복 기동을 막는다.
if lsof -ti:8080 >/dev/null 2>&1; then
  echo "✗ 8080 사용 중. 먼저 ./map-down.sh 로 정리하세요."; exit 1
fi
# 동기 facade 는 요청 처리 중 agent·hub 를 호출하므로 백엔드 준비를 선검사.
for pair in "hub:8001" "agent:8000"; do
  port="${pair##*:}"
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/health")" != "200" ]; then
    echo "✗ ${pair%%:*}(:$port) 미응답. 먼저 ./map-up-1-backend.sh 실행."; exit 1
  fi
done

# 비밀값은 환경변수로만 주입(화면 미출력). 로컬 실행이라 컨테이너 DNS 가 아닌 호스트 포트 사용:
# redis 는 호스트 16379(컨테이너 6379), agent/hub 는 호스트 8000/8001.
export POSTGRES_PASSWORD="$(grep -E '^POSTGRES_PASSWORD=' "$ENVF" | cut -d= -f2-)"
export JWT_SECRET="$(grep -E '^JWT_SECRET=' "$ENVF" | cut -d= -f2-)"
export POSTGRES_HOST=localhost POSTGRES_PORT=5432 POSTGRES_DB=map POSTGRES_USER=map
export REDIS_HOST=localhost REDIS_PORT=16379
export AGENT_BASE_URL=http://localhost:8000 HUB_BASE_URL=http://localhost:8001

echo "== BFF bootRun(8080) — 'Started ServiceUserApplication' 출력 시 준비. 종료 Ctrl+C =="
cd "$MAP_ROOT/map-service-user"
# exec 로 현재 셸을 대체 → Ctrl+C(SIGINT)가 gradle/JVM 에 직접 전달된다.
exec ./gradlew bootRun --console=plain
