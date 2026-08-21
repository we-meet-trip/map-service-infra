#!/usr/bin/env bash
# =========================================================================
# map postgres 백업 스크립트.
#
# 무엇을 하는가:
#   compose 의 postgres 컨테이너에서 pg_dump 로 전체 DB 를 떠서 gzip 압축해
#   ~/backups/ 아래에 시각별로 저장하고, 보존기간(기본 7일) 초과분을 지운다.
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
#   COMPOSE_PROJECT=map-test ENV_FILE=./.env.test ./scripts/pg-backup.sh  # 시험 스택
#
# 복원(요약):
#   gunzip -c ~/backups/map-<시각>.sql.gz | \
#     docker compose -p map-service exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
#   복원이 실제로 되는지는 scripts/pg-restore-check.sh 가 확인한다.
#
# cron 등록 예(매일 04:00):
#   0 4 * * * cd /path/to/map-service-infra && ./scripts/pg-backup.sh >> ~/backups/pg-backup.log 2>&1
#
# 안전 장치가 세 가지 있다:
#
#   (1) 임시 파일에 받아 검증한 뒤에야 최종 이름으로 옮긴다.
#       예전에는 최종 이름으로 바로 리다이렉트했는데, 리다이렉트는 pg_dump 가
#       실행되기 전에 파일을 연다. 그래서 같은 날 다시 돌리면 덤프가 실패해도
#       그 순간 이미 기존 백업이 잘려 나갔다. 장애가 난 뒤 "백업부터 뜨자" 가
#       마지막 정상 백업을 지우는 셈이었다.
#
#   (2) 파일명에 시각까지 넣는다. 날짜만 쓰면 하루에 두 번 돌린 것이 서로를
#       덮는다.
#
#   (3) 프로젝트를 이름으로 못박는다. 지정하지 않으면 어느 스택의 postgres 에
#       붙을지 현재 디렉터리에 따라 달라져, 시험 스택을 운영 백업으로 남길 수
#       있다.
# =========================================================================
set -euo pipefail

# 스크립트 위치 기준으로 infra 루트로 이동(compose 파일이 있는 곳).
cd "$(dirname "$0")/.."

# POSTGRES_USER/DB 를 읽는다(없으면 기본값). 시험 스택을 뜰 때는
# ENV_FILE 로 그쪽 파일을 지정한다 — 프로젝트만 바꾸고 계정을 운영 것으로
# 두면 어느 쪽에 붙는지가 다시 흐려진다.
ENV_FILE="${ENV_FILE:-./.env}"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1091
  set -a; . "$ENV_FILE"; set +a
fi
: "${POSTGRES_USER:=map}"
: "${POSTGRES_DB:=map}"

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-map-service}"
STAMP="$(date +%F_%H%M%S)"
OUT="${BACKUP_DIR}/map-${STAMP}.sql.gz"
TMP="${OUT}.partial"
OUT_GLOBALS="${BACKUP_DIR}/map-globals-${STAMP}.sql.gz"
TMP_GLOBALS="${OUT_GLOBALS}.partial"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

# 중간에 죽어도 반쪽짜리 파일을 남기지 않는다. 최종 이름은 검증을 통과한
# 뒤에만 생기므로, 디렉터리에 보이는 파일은 전부 복원 가능한 것이다.
cleanup() { rm -f "$TMP" "$TMP_GLOBALS"; }
trap cleanup EXIT

# 압축이 온전한지, 그리고 안에 내용이 있는지 함께 본다. gzip -t 만으로는
# 부족하다 — 빈 입력을 압축해도 gzip 으로는 멀쩡한 파일이 되기 때문이다.
# 실제로 그렇게 만들어진 20바이트짜리 "백업" 이 남아 있던 적이 있다.
verify_dump() {
  local path="$1" label="$2"
  gzip -t "$path"
  if [ ! -s "$path" ] || [ "$(gzip -dc "$path" | head -c 1 | wc -c)" -eq 0 ]; then
    echo "[pg-backup] ✗ ${label} 덤프가 비어 있다 — 최종 파일을 만들지 않는다"
    exit 1
  fi
}

echo "[pg-backup] project=${COMPOSE_PROJECT} db=${POSTGRES_DB} user=${POSTGRES_USER}"

# 역할·권한을 먼저 뜬다. pg_dump 에는 역할 정의가 들어가지 않아서, 이것이
# 없으면 빈 클러스터에 복원할 때 map_admin 같은 역할을 찾지 못해 멈춘다.
# 예전에는 이 파일을 따로 한 번만 떠 두었는데, 그러면 역할이 바뀐 뒤의
# 데이터 덤프와 짝이 맞지 않는다. 같은 시각으로 함께 뜬다.
echo "[pg-backup] dumping globals -> ${OUT_GLOBALS}"
docker compose -p "${COMPOSE_PROJECT}" exec -T postgres \
  pg_dumpall -U "${POSTGRES_USER}" --globals-only | gzip -c > "${TMP_GLOBALS}"
verify_dump "${TMP_GLOBALS}" "globals"

echo "[pg-backup] dumping -> ${OUT}"
# -T: TTY 비할당(cron 안전). pg_dump 를 컨테이너 안에서 실행하고 stdout 을 gzip.
docker compose -p "${COMPOSE_PROJECT}" exec -T postgres \
  pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" | gzip -c > "${TMP}"
echo "[pg-backup] verifying"
verify_dump "${TMP}" "data"

# 둘 다 통과한 뒤에 함께 내놓는다. 한쪽만 남으면 복원할 수 없는 짝이 된다.
chmod 600 "${TMP_GLOBALS}" "${TMP}"
mv "${TMP_GLOBALS}" "${OUT_GLOBALS}"
mv "${TMP}" "${OUT}"
trap - EXIT

# 무결성 확인용. 오프박스로 옮긴 뒤에도 같은 파일인지 이것으로 본다.
if command -v shasum >/dev/null 2>&1; then
  ( cd "${BACKUP_DIR}" && shasum -a 256 \
      "$(basename "${OUT_GLOBALS}")" "$(basename "${OUT}")" >> "map-checksums.txt" )
  chmod 600 "${BACKUP_DIR}/map-checksums.txt" 2>/dev/null || true
fi

echo "[pg-backup] done: globals $(du -h "${OUT_GLOBALS}" | cut -f1) · data $(du -h "${OUT}" | cut -f1)"

# 정리는 새 백업이 검증까지 끝난 뒤에만 한다. 순서를 뒤집으면 이번 백업이
# 실패한 날에 과거 백업만 지우게 된다.
#
# 역할 덤프와 데이터 덤프는 같은 시각으로 짝을 이루므로 같은 기준으로 지운다.
# 반쪽짜리 짝은 애초에 만들어지지 않으니(위에서 둘 다 검증한 뒤 함께 내놓는다)
# 남은 파일은 언제나 짝이 맞는다.
echo "[pg-backup] pruning backups older than ${RETAIN_DAYS} days"
find "${BACKUP_DIR}" -name 'map-*.sql.gz' -type f -mtime +"${RETAIN_DAYS}" -print -delete || true

echo "[pg-backup] complete"
