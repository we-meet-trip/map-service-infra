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
#     docker compose --env-file ./.env -f docker-compose.yml \
#       exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
#
#   되돌리는 순서가 중요하다. pg_dump 는 DB 하나만 담고 역할과 비밀번호는
#   담지 않는데, 스키마 하나는 소유자가 따로 있다. 그래서 빈 저장소를 먼저
#   띄워 db/init 이 역할을 만들게 한 뒤 이 덤프를 붓는다. 덤프가 지울 것을
#   먼저 지우므로 초기화가 만들어 둔 스키마와 부딪히지 않는다.
#
#   실제로 되살려 본 적 없는 백업은 백업이 아니다. 분기에 한 번은 빈 저장소에
#   부어 표 개수와 행 수를 원본과 대조한다.
#
# cron 등록 예(매일 04:00):
#   0 4 * * * cd /path/to/map-service-infra && BACKUP_REMOTE=사용자@호스트:/경로 \
#     ./scripts/pg-backup.sh >> ~/backups/pg-backup.log 2>&1
#
#   시험 스택을 뜰 때는 --test 를 준다. 옮길 곳과 쌓을 곳은 환경파일에 두지
#   않는다 — 이 스크립트가 환경파일을 나중에 읽어 호출자가 준 값을 덮는다.
# =========================================================================
set -euo pipefail

# 스크립트 위치 기준으로 infra 루트로 이동(compose 파일이 있는 곳).
cd "$(dirname "$0")/.."

# 어느 스택을 뜰지는 환경파일과 덧칠이 함께 정한다. 둘 중 하나만 주면 도는
# 것과 다른 프로젝트를 가리켜 "그런 서비스가 없다" 로 끝난다.
ENV_FILE=./.env
FILES=(-f docker-compose.yml)
if [ "${1:-}" = "--test" ]; then
  ENV_FILE=./.env.test
  FILES+=(-f docker-compose.test.yml)
fi
[ -f "$ENV_FILE" ] || { echo "[pg-backup] 환경파일이 없다: $ENV_FILE" >&2; exit 1; }

# 환경파일에서 계정과 DB 이름을 읽는다(없으면 기본값).
#
# 이 읽기는 호출자가 준 값도 덮는다. 그래서 BACKUP_DIR·BACKUP_REMOTE 는 이
# 파일에 두지 않는다 — 넣으면 명령줄로 준 값이 조용히 무시되고, 그 파일은
# 앱 컨테이너 넷에 통째로 들어가므로 옮길 곳 주소까지 함께 새어 나간다.
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
: "${POSTGRES_USER:=map}"
: "${POSTGRES_DB:=map}"

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"

# 시각까지 넣는다. 날짜만으로는 같은 날 두 번째 배포가 첫 배포 직전에 떠 둔
# 것을 덮어써, 첫 배포가 데이터를 망쳤을 때 되돌릴 자리가 사라진다. DB 이름을
# 넣는 것은 한 기계에서 두 스택을 뜰 때 서로를 덮지 않게 하려는 것이다.
STAMP="$(date +%F-%H%M%S)"
OUT="${BACKUP_DIR}/map-${POSTGRES_DB}-${STAMP}.sql.gz"
PART="${OUT}.part"

mkdir -p "$BACKUP_DIR"
trap 'rm -f "$PART"' EXIT

dc() { docker compose --env-file "$ENV_FILE" "${FILES[@]}" "$@"; }

echo "[pg-backup] dumping ${POSTGRES_DB} as ${POSTGRES_USER} -> ${OUT}"
# 받는 파일을 먼저 만들고 다 뜬 것만 제자리로 옮긴다. 곧바로 최종 이름에 쓰면
# 뜨다 실패했을 때 이미 잘라 둔 빈 파일이 성공한 백업 자리에 남는다.
# -T: TTY 비할당(cron 안전).
# --clean --if-exists 로 뜬다. 이것이 없으면 되살릴 수 없다.
#
# 빈 저장소는 처음 뜰 때 초기화가 스키마를 먼저 만든다. 그 위에 덤프를 부으면
# 첫 줄 CREATE SCHEMA 에서 이미 있다며 멈추고, 아무것도 복원되지 않는다.
# 오류를 무시하고 밀어 넣으면 이번엔 실패를 못 보게 된다.
# 지울 것을 먼저 지우게 만들면 초기화가 무엇을 만들어 두었든 그대로 덮인다.
dc exec -T postgres pg_dump --clean --if-exists \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  | gzip -c > "${PART}"
mv "${PART}" "${OUT}"

# 같은 기계에만 쌓으면 그 기계가 사라질 때 함께 사라진다. 옮길 곳을 주면 옮기고
# 크기까지 맞춰 본다 — 옮기다 끊긴 파일은 생겨 있어서 성공과 구분되지 않는다.
# 형태는 사용자@호스트:/디렉터리 로 고정한다.
#
# 여기서 실패해도 끝내 성공으로 둔다. 이 자리까지 왔으면 되돌릴 자리는 이미 이
# 기계에 확보돼 있고, 원격이 잠깐 안 된다고 배포를 막을 이유가 없다.
if [ -n "${BACKUP_REMOTE:-}" ]; then
  if scp -q "$OUT" "${BACKUP_REMOTE}/"; then
    here=$(wc -c < "$OUT" | tr -d " ")
    there=$(ssh "${BACKUP_REMOTE%%:*}" \
      "wc -c < '${BACKUP_REMOTE#*:}/$(basename "$OUT")'" 2>/dev/null | tr -d " " || echo 0)
    if [ "$here" = "$there" ]; then
      echo "[pg-backup] 옮김: ${BACKUP_REMOTE}/$(basename "$OUT")"
    else
      echo "[pg-backup] 옮긴 크기가 다르다(${here} / ${there}) — 이 기계 것만 믿는다" >&2
    fi
  else
    echo "[pg-backup] 옮기지 못했다 — 이 기계에만 남는다" >&2
  fi
else
  echo "[pg-backup] BACKUP_REMOTE 가 비어 있다 — 이 기계에만 남는다" >&2
fi

echo "[pg-backup] done: $(du -h "${OUT}" | cut -f1)"

echo "[pg-backup] pruning backups older than ${RETAIN_DAYS} days"
find "${BACKUP_DIR}" -name 'map-*.sql.gz' -type f -mtime +"${RETAIN_DAYS}" -print -delete || true
find "${BACKUP_DIR}" -name 'map-*.sql.gz.part' -type f -mtime +1 -print -delete || true

echo "[pg-backup] complete"
