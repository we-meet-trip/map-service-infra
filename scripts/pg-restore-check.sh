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
for i in $(seq 1 60); do
  if docker exec "$BOX" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    echo "   준비됨 (${i}s)"
    break
  fi
  [ "$i" -eq 60 ] && { echo "✗ 일회용 postgres 가 뜨지 않았다"; exit 1; }
  sleep 1
done

echo "== 복원 =="
if ! gzip -dc "$DUMP" | docker exec -i "$BOX" \
     psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null; then
  echo "✗ 복원 중 오류 — 이 백업으로는 되살릴 수 없다"
  exit 1
fi

echo "== 내용 확인 =="
# 스키마가 서 있고 테이블이 실제로 들어왔는지 본다. 복원이 조용히 아무것도
# 하지 않은 경우를 잡는다.
SUMMARY="$(docker exec "$BOX" psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
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
