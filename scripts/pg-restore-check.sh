#!/usr/bin/env bash
# 백업이 정말 복원되는지 확인한다.
#
# 왜 필요한가:
#   복원해 본 적 없는 백업은 없는 것과 같다. 파일이 생겼고 gzip 이 온전하다는
#   것과, 그것으로 빈 데이터베이스를 다시 세울 수 있다는 것은 다른 이야기다.
#   확인은 실제로 세워 보는 것 말고 다른 방법이 없다.
#
# 운영을 건드리지 않는 방법:
#   운영 postgres 에 붓지 않는다. 같은 이미지로 일회용 컨테이너를 따로 띄우고
#   거기에 붓는다. 네트워크에 붙이지 않고 포트도 열지 않으며, 끝나면 볼륨까지
#   지운다. 그래서 이 스크립트는 운영이 떠 있든 아니든 안전하다.
#
# 사용:
#   ./scripts/pg-restore-check.sh                    # 가장 최근 백업으로
#   ./scripts/pg-restore-check.sh ~/backups/map-....sql.gz
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f ./.env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
: "${POSTGRES_USER:=map}"
: "${POSTGRES_DB:=map}"

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
IMAGE="${POSTGRES_IMAGE:-postgis/postgis:17-3.5}"
# 운영 컨테이너와 절대 겹치지 않는 이름. 확인이 끝나면 사라진다.
BOX="map-restore-check-$$"

DUMP="${1:-}"
if [ -z "$DUMP" ]; then
  DUMP="$(ls -t "${BACKUP_DIR}"/map-*.sql.gz 2>/dev/null | head -1 || true)"
fi
[ -n "$DUMP" ] && [ -f "$DUMP" ] || { echo "✗ 복원할 백업을 못 찾았다 (${BACKUP_DIR})"; exit 1; }

echo "== 대상: $DUMP ($(du -h "$DUMP" | cut -f1)) =="

cleanup() {
  docker rm -f -v "$BOX" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== 일회용 postgres 기동 =="
# --network none: 어떤 스택에도 붙지 않는다. 포트도 열지 않는다.
# 데이터는 익명 볼륨에만 쓰고 -v 로 함께 지운다.
docker run -d --name "$BOX" \
  --platform linux/amd64 \
  --network none \
  -e POSTGRES_DB="$POSTGRES_DB" \
  -e POSTGRES_USER="$POSTGRES_USER" \
  -e POSTGRES_PASSWORD=restore-check-only \
  "$IMAGE" >/dev/null

echo "== 준비 대기 =="
# TCP 로 물어본다. pg_isready 를 쓰면 안 된다 — postgres 이미지는 초기화
# 단계에서 임시 서버를 띄우는데, 그 서버도 유닉스 소켓에는 응답하므로
# pg_isready 가 "준비됨" 을 돌려준다. 그 상태에서 복원을 시작하면 초기화가
# 끝나는 순간 entrypoint 가 임시 서버를 내리면서 복원이 중간에 잘린다.
# 임시 서버는 TCP 를 열지 않으므로(listen_addresses 가 비어 있다) TCP 로
# 물으면 진짜 서버가 뜬 뒤에만 통과한다.
for i in $(seq 1 90); do
  if docker exec "$BOX" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
       -c 'SELECT 1' >/dev/null 2>&1; then
    echo "   준비됨 (${i}s)"
    break
  fi
  [ "$i" -eq 90 ] && { echo "✗ 일회용 postgres 가 뜨지 않았다"; docker logs "$BOX" 2>&1 | tail -20; exit 1; }
  sleep 1
done

# 역할을 먼저 세운다. 데이터 덤프에는 역할 정의가 들어가지 않아서, 이것 없이
# 부으면 map_admin 같은 역할을 찾지 못해 중간에 멈춘다.
GLOBALS="$(dirname "$DUMP")/$(basename "$DUMP" | sed 's|^map-|map-globals-|')"
if [ -f "$GLOBALS" ]; then
  echo "== 역할 복원: $(basename "$GLOBALS") =="
  # 이미 있는 역할(컨테이너가 만든 superuser)은 중복 오류가 나는데, 그것은
  # 정상이므로 멈추지 않는다. 진짜 문제는 다음 단계에서 드러난다.
  gzip -dc "$GLOBALS" | docker exec -i "$BOX" \
    psql -h 127.0.0.1 -v ON_ERROR_STOP=0 -q -U "$POSTGRES_USER" -d postgres >/dev/null 2>&1 || true
else
  echo "== 역할 덤프 없음 ($(basename "$GLOBALS")) — 데이터만 시도한다 =="
fi

echo "== 복원 =="
if ! gzip -dc "$DUMP" | docker exec -i "$BOX" \
     psql -h 127.0.0.1 -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null; then
  echo "✗ 복원 중 오류 — 이 백업으로는 되살릴 수 없다"
  [ -f "$GLOBALS" ] || echo "  (역할 덤프가 없다. pg-backup.sh 가 둘을 함께 뜨는지 확인할 것)"
  exit 1
fi

echo "== 내용 확인 =="
# 스키마가 서 있고 테이블이 실제로 들어왔는지 본다. 복원이 조용히 아무것도
# 하지 않은 경우를 잡는다.
SUMMARY="$(docker exec "$BOX" psql -h 127.0.0.1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
  SELECT table_schema || ' ' || count(*)
  FROM information_schema.tables
  WHERE table_type = 'BASE TABLE'
    AND table_schema NOT IN ('pg_catalog','information_schema')
  GROUP BY table_schema ORDER BY table_schema;")"

if [ -z "$SUMMARY" ]; then
  echo "✗ 복원 후 사용자 테이블이 하나도 없다"
  exit 1
fi

echo "$SUMMARY" | while read -r schema count; do
  printf '   %-16s 테이블 %s개\n' "$schema" "$count"
done

echo "== ✓ 복원 확인 완료 (일회용 컨테이너는 정리된다) =="
