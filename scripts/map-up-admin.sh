#!/usr/bin/env bash
# 관리자 스택 기동: 운영 콘솔(admin + admin-web), 선택적으로 모니터링까지.
#
# 사용법:
#   ./scripts/map-up-admin.sh              # 콘솔만
#   ./scripts/map-up-admin.sh --monitoring # 콘솔 + prometheus/grafana/exporter
#
# 관리자 스택은 서비스 스택이 만든 map-net 에 얹혀 있고, DB 도 서비스 스택 것을
# 쓴다. compose 가 스택을 넘어 depends_on 을 걸 수 없으므로 여기서 먼저
# 네트워크와 DB 준비 상태를 확인한 뒤 기동한다.
set -uo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml")
ADMIN_COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.admin.yml")

WITH_MONITORING=0
[ "${1:-}" = "--monitoring" ] && WITH_MONITORING=1

echo "== [0/3] 사전 점검 (Docker · 서비스 스택 네트워크) =="
if ! docker info >/dev/null 2>&1; then
  echo "  ✗ Docker 데몬 꺼짐. Docker Desktop 실행 후 재시도: open -a Docker"; exit 1
fi
# map-net 은 서비스 스택이 만든다. 없으면 관리자 스택이 붙을 곳이 없다.
if ! docker network inspect map-net >/dev/null 2>&1; then
  echo "  ✗ map-net 없음. 서비스 스택을 먼저 기동한다:"
  echo "      docker compose -f \"$INFRA_DIR/docker-compose.yml\" --profile full up -d"
  exit 1
fi
echo "  ✓ Docker 가동 · map-net 존재"

echo "== [1/3] postgres 준비 대기 =="
# admin 은 기동 직후 alembic 으로 admin_data 를 올린다. DB 가 연결을 수락하기
# 전에 뜨면 재시도로 회복되긴 하지만, 여기서 기다리면 로그가 깨끗하다.
pg_ok=0
for i in $(seq 1 30); do
  if "${SERVICE_COMPOSE[@]}" exec -T postgres pg_isready -U map -d map >/dev/null 2>&1; then pg_ok=1; break; fi
  sleep 2
done
if [ "$pg_ok" != 1 ]; then
  echo "  ✗ postgres 준비 타임아웃. 서비스 스택 상태 확인: docker compose -f \"$INFRA_DIR/docker-compose.yml\" ps"
  exit 1
fi
echo "  ✓ postgres ready"

echo "== [2/3] 관리자 스택 기동 =="
if [ "$WITH_MONITORING" = 1 ]; then
  echo "  콘솔 + 모니터링"
  "${ADMIN_COMPOSE[@]}" --profile monitoring up -d
else
  echo "  콘솔만 (모니터링까지 띄우려면 --monitoring)"
  "${ADMIN_COMPOSE[@]}" up -d
fi

echo "== [3/3] 헬스 체크 =="
fail=0
ok=0
for i in $(seq 1 25); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8002/health")" = "200" ]; then
    echo "  ✓ admin (:8002) 200"; ok=1; break
  fi
  sleep 3
done
if [ "$ok" != 1 ]; then
  echo "  ✗ admin (:8002) 헬스 실패. 로그: docker compose -f \"$INFRA_DIR/docker-compose.admin.yml\" logs admin"
  fail=1
fi

code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8003/")"
if [ "$code" = "200" ]; then echo "  ✓ admin-web (:8003) 200"; else echo "  ✗ admin-web (:8003) $code"; fail=1; fi

if [ "$WITH_MONITORING" = 1 ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:9090/-/ready")"
  [ "$code" = "200" ] && echo "  ✓ prometheus (:9090) ready" || echo "  · prometheus (:9090) $code (기동 직후면 잠시 후 재확인)"
fi

[ "$fail" = 0 ] || exit 1

echo ""
echo "== ✅ 관리자 스택 준비 완료. 콘솔: http://127.0.0.1:8003 =="
