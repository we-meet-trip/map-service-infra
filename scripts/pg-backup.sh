#!/usr/bin/env bash
# =========================================================================
# map postgres 백업 스크립트.
#
# 무엇을 하는가:
#   compose 의 postgres 컨테이너에서 pg_dump 로 전체 DB 를 떠서 gzip 압축해
#   ~/backups/ 아래에 날짜별로 저장하고, 보존기간(기본 7일) 초과분을 지운다.
#
# 언제 실행하는가:
#   compose 를 띄워 둔 호스트에서 cron 으로 매일 1회(예: 04:00). 배포처와
#   무관하게 동작하며 외부 스토리지 서비스에 의존하지 않는다. 단일 노드
#   구성이므로 주 1회는 산출물을 오프박스(다른 머신/외장 디스크)로 복사해
#   호스트 소실에 대비한다.
#
# 사용:
#   ./scripts/pg-backup.sh                 # 기본값으로 실행
#   BACKUP_DIR=/data/backups RETAIN_DAYS=14 ./scripts/pg-backup.sh
#
# 복원(요약):
#   gunzip -c ~/backups/map-YYYY-MM-DD.sql.gz | \
#     docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
#
# cron 등록 예(매일 04:00):
#   0 4 * * * cd /path/to/map-service-infra && ./scripts/pg-backup.sh >> ~/backups/pg-backup.log 2>&1
# =========================================================================
set -euo pipefail

# 스크립트 위치 기준으로 infra 루트로 이동(compose 파일이 있는 곳).
cd "$(dirname "$0")/.."

# .env 에서 POSTGRES_USER/DB 를 읽는다(없으면 기본값).
if [ -f ./.env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
: "${POSTGRES_USER:=map}"
: "${POSTGRES_DB:=map}"

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"
STAMP="$(date +%F)"
OUT="${BACKUP_DIR}/map-${STAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[pg-backup] dumping ${POSTGRES_DB} as ${POSTGRES_USER} -> ${OUT}"
# -T: TTY 비할당(cron 안전). pg_dump 를 컨테이너 안에서 실행하고 stdout 을 gzip.
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  | gzip -c > "${OUT}"

echo "[pg-backup] done: $(du -h "${OUT}" | cut -f1)"

echo "[pg-backup] pruning backups older than ${RETAIN_DAYS} days"
find "${BACKUP_DIR}" -name 'map-*.sql.gz' -type f -mtime +"${RETAIN_DAYS}" -print -delete || true

echo "[pg-backup] complete"
