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
#   ./scripts/cloud-up.sh --admin            운영 콘솔까지 함께
#   ./scripts/cloud-up.sh --monitoring       콘솔 + 지표 수집까지 함께
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=./.env
FILES=(-f docker-compose.yml)
PROFILES=(--profile full)
LABEL=운영
MICRO=0
PULL=0
ROUTING=0
VISION=0
EDGE=0
ADMIN=0
MONITORING=0
# 관리자 스택은 프로젝트가 따로다. 서비스 스택이 만든 네트워크에 얹히므로
# 파일도 순서도 따로 세어야 한다.
ADMIN_FILES=(-f docker-compose.admin.yml)
ADMIN_PROFILES=()

for arg in "$@"; do
  case "$arg" in
    --test) ENV_FILE=./.env.test; FILES+=(-f docker-compose.test.yml)
            ADMIN_FILES+=(-f docker-compose.admin.test.yml); LABEL=시험 ;;
    # 덧칠 순서가 중요하다. 나중에 붙은 것이 이긴다.
    --micro) FILES+=(-f docker-compose.micro.yml); MICRO=1 ;;
    --registry) FILES+=(-f docker-compose.registry.yml)
                ADMIN_FILES+=(-f docker-compose.admin.registry.yml); PULL=1 ;;
    --edge) FILES+=(-f docker-compose.edge.yml); PROFILES+=(--profile edge --profile dns); EDGE=1 ;;
    # 운영 콘솔을 함께 올린다. 서비스 스택이 만든 네트워크에 얹히므로 반드시
    # 서비스가 먼저 서고 난 뒤에 세운다 — 아래에서 마지막 단계로 돌린다.
    --admin) ADMIN=1 ;;
    # 지표 수집까지. 콘솔의 모니터링 화면은 여기서 뜨는 것을 창으로 불러온다.
    --monitoring) ADMIN=1; MONITORING=1 ;;
    # 경로 엔진을 함께 올린다. 켜지 않으면 hub 가 주소를 못 찾아 구간마다
    # 실패 왕복을 반복하고, 화면에는 도로를 따르지 않는 직선이 그려진다.
    --routing) PROFILES+=(--profile routing); ROUTING=1 ;;
    # 카메라 인식을 함께 올린다. 켜지 않으면 앱의 카메라 화면이 연결에
    # 실패하는데, 화면에는 그냥 오류 한 줄로만 보인다.
    --vision) PROFILES+=(--profile vision); VISION=1 ;;
    *) echo "모르는 인자: $arg" >&2; exit 2 ;;
  esac
done

[ "$MONITORING" = 1 ] && ADMIN_PROFILES=(--profile monitoring)

# A verified release bundle pins every application to an OCI digest. Keep the
# service and administrator overrides separate: their Compose projects differ.
if [ -n "${RELEASE_BUNDLE:-}" ]; then
  [ "$PULL" = 1 ] || { echo 'RELEASE_BUNDLE requires --registry' >&2; exit 2; }
  for pin in compose.images.yml compose.admin-images.yml; do
    [ -f "$RELEASE_BUNDLE/$pin" ] || { echo 'release image pin missing' >&2; exit 2; }
  done
  FILES+=(-f "$RELEASE_BUNDLE/compose.images.yml")
  ADMIN_FILES+=(-f "$RELEASE_BUNDLE/compose.admin-images.yml")
fi

# Automated application releases preserve the receiver's exact infrastructure
# images. New infrastructure is resolved and pulled before this script runs.
# MAP_INFRA_IMAGE_BUNDLE_VERSION=1
if [ -n "${INFRA_IMAGE_BUNDLE:-}" ]; then
  [ -n "${RELEASE_BUNDLE:-}" ] || { echo 'infrastructure pins require a release bundle' >&2; exit 2; }
  for pin in compose.infrastructure.yml compose.admin-infrastructure.yml; do
    [ -f "$INFRA_IMAGE_BUNDLE/$pin" ] || { echo 'infrastructure image pin missing' >&2; exit 2; }
  done
  FILES+=(-f "$INFRA_IMAGE_BUNDLE/compose.infrastructure.yml")
  ADMIN_FILES+=(-f "$INFRA_IMAGE_BUNDLE/compose.admin-infrastructure.yml")
fi

# 작은 서버 덧칠은 1GB 급을 겨냥한다. 카메라 인식은 모델을 들고 있어 그 위에
# 더 얹을 자리가 없다 — 재 보니 나머지 여섯만으로 부하 중 838 MiB 였고 거기에
# 279 MiB 가 더 붙는다. 뜨기는 하다가 무엇이 먼저 죽을지 모르는 상태가 된다.
if [ "$MICRO" = 1 ] && [ "$VISION" = 1 ]; then
  echo "작은 서버 덧칠과 카메라 인식은 함께 쓸 수 없다." >&2
  echo "  둘 중 하나를 빼거나, 메모리가 더 큰 서버를 쓴다." >&2
  exit 2
fi

# 같은 이유로 콘솔도 막는다. 일곱을 합쳐 상한이 1.4GB 라, 1GB 급을 겨냥한
# 덧칠 위에 얹으면 무엇이 먼저 죽을지 모르는 상태가 된다.
if [ "$MICRO" = 1 ] && [ "$ADMIN" = 1 ]; then
  echo "작은 서버 덧칠과 운영 콘솔은 함께 쓸 수 없다." >&2
  echo "  콘솔을 빼거나, 메모리가 더 큰 서버를 쓴다." >&2
  exit 2
fi

[ -f "$ENV_FILE" ] || { echo "환경파일이 없다: $ENV_FILE" >&2; exit 1; }

dc() { docker compose --env-file "$ENV_FILE" "${FILES[@]}" "$@"; }
dca() { docker compose --env-file "$ENV_FILE" "${ADMIN_FILES[@]}" "$@"; }

# 값이 비면 그 서비스가 부팅하다 멈추는 것들만 미리 본다. 여기서 걸러 내지
# 않으면 컨테이너가 뜨다 죽기를 반복하는 모습으로만 드러난다.
for key in POSTGRES_PASSWORD HUB_DATABASE_URL GEMINI_API_KEY; do
  if ! grep -qE "^${key}=.+" "$ENV_FILE"; then
    echo "$ENV_FILE 에 $key 값이 없다" >&2
    exit 1
  fi
done

if [ "$ADMIN" = 1 ]; then
  # 콘솔이 부팅하다 멈추는 값들. 저장소 접속과 첫 계정이 없으면 뜨더라도
  # 아무도 들어갈 수 없다.
  for key in ADMIN_DATABASE_URL MAP_ADMIN_PASSWORD; do
    if ! grep -qE "^${key}=.+" "$ENV_FILE"; then
      echo "$ENV_FILE 에 $key 값이 없다 — 콘솔이 뜨지 못한다" >&2
      exit 1
    fi
  done
  # 이 둘은 목록을 담는 자리라 빈 값이 곧 형식 오류다. 키를 아예 두지 않으면
  # 기본값으로 도는데, 이름만 적고 값을 비우면 그 자리에서 뜨지 못한다.
  # 그 모습은 다른 기동 실패와 구분되지 않아 여기서 먼저 걸러 낸다.
  for key in MONITORING_PANELS ADMIN_CORS_ORIGINS; do
    if grep -qE "^${key}=[[:space:]]*$" "$ENV_FILE"; then
      echo "$ENV_FILE 의 $key 가 이름만 있고 값이 비었다." >&2
      echo "  쓰지 않을 것이면 그 줄을 통째로 주석 처리한다. 빈 값은 형식 오류다." >&2
      exit 1
    fi
  done
fi

# 경로 데이터는 이미지 안이 아니라 따로 만들어 둔 저장 자리에 있다. 없으면
# 엔진이 뜨자마자 죽는데, 그 모습은 다른 기동 실패와 구분되지 않는다.
#
# korea.osrm 이라는 파일은 없다. 그건 접두사이고 실제로는 korea.osrm.* 여러
# 개다. 접두사를 파일로 알고 확인하면 데이터가 온전해도 늘 없다고 답한다.
#
# 손질은 세 단계로 나뉘고 중간에 끊길 수 있다. 앞 단계 산출물은 남고 뒤
# 단계 것만 없는데 엔진은 뒤 단계 것을 읽으므로, 단계마다 하나씩 본다.
if [ "$ROUTING" = 1 ]; then
  # 볼륨 이름을 렌더링에서 가져온다. 못박아 두면 시험 스택에서 운영 볼륨을
  # 보고 판단해, 시험 쪽 자리가 비어 있어도 통과시킨다.
  osrm_vol=$(dc --profile routing config --format json 2>/dev/null \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['volumes']['osrm-data']['name'])" \
    2>/dev/null || echo osrm-data)
  missing=$(docker run --rm -v "$osrm_vol:/data" alpine sh -c \
    'for p in foot bicycle; do
       for f in edges partition cells mldgr; do
         [ -e "/data/$p/korea.osrm.$f" ] || echo "/data/$p/korea.osrm.$f"
       done
     done' 2>/dev/null)
  if [ -n "$missing" ]; then
    echo "경로 데이터가 없다: $missing" >&2
    echo "먼저 ./scripts/osrm-rebuild.sh 로 만든다. 내려받기와 손질에 시간이 걸린다." >&2
    exit 1
  fi
fi

if [ "$PULL" = 1 ]; then
  echo "[$LABEL] 0/4 이미지 받기"
  pull_services=()
  admin_pull_services=()
  if [ -n "${INFRA_IMAGE_BUNDLE:-}" ]; then
    pull_services=(user agent hub)
    [ "$VISION" = 0 ] || pull_services+=(yolo)
    admin_pull_services=(admin admin-web)
  fi
  # 받는 곳은 네 가지 사정을 모두 같은 글자(denied)로 답한다 — 로그인을 안
  # 했을 때, 토큰이 만료됐을 때, 판 이름을 잘못 적었을 때, 이름이 바뀌었을 때.
  # 그 넷을 구분해 주지 않으므로 여기서 무엇을 봐야 하는지 대신 적어 준다.
  # Bash 3.2 treats an empty array as unset under nounset (macOS manual startup).
  if ! dc "${PROFILES[@]}" pull ${pull_services[@]+"${pull_services[@]}"}; then
    echo "이미지를 받지 못했다. 아래를 차례로 본다." >&2
    echo "  1) 이 계정으로 받을 수 있는가 — docker login ghcr.io (패키지가 비공개면 필요하다)" >&2
    echo "  2) 판 이름이 실제로 올라간 이름인가 — $ENV_FILE 의 IMAGE_TAG" >&2
    echo "  3) sudo 로 돌리고 있다면 로그인한 계정과 같은 계정인가" >&2
    exit 1
  fi
  # 콘솔 이미지도 같은 자리에서 받는다. 뒤늦게 기동 단계에서 받으면 그때
  # 없다는 것을 알게 되는데, 그 시점에는 서비스가 이미 서 있어 되돌릴 것이
  # 늘어난다.
  if [ "$ADMIN" = 1 ] && ! dca "${ADMIN_PROFILES[@]}" pull ${admin_pull_services[@]+"${admin_pull_services[@]}"}; then
    echo "콘솔 이미지를 받지 못했다. 위의 셋에 하나를 더 본다." >&2
    echo "  4) 콘솔 이름 둘이 공개인가 — 처음 만들어진 이름은 비공개로 생긴다" >&2
    exit 1
  fi
fi

echo "[$LABEL] 1/4 저장소 기동"
if [ -n "${INFRA_IMAGE_BUNDLE:-}" ]; then
  dc --profile infra up -d --no-recreate postgres redis
else
  dc --profile infra up -d
fi

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
if ! dc exec -T postgres pg_isready -h 127.0.0.1 -U "$db_user" -d "$db_name" >/dev/null 2>&1; then
  echo 'database readiness failed; migration is blocked' >&2
  exit 1
fi

# 아래 단계는 표를 바꾸고 되돌아가지 않는다. 백업의 옳은 자리는 여기 하나뿐이다 —
# 지나간 뒤에 뜨면 이미 바뀐 것을 뜬다. 뜨지 못하면 되돌릴 자리가 없다는 뜻이므로
# 표를 건드리지 않고 멈춘다.
echo "[$LABEL] 표를 바꾸기 전에 지금 상태를 떠 둔다"
backup_args=(--prod)
[ "$ENV_FILE" = ./.env.test ] && backup_args=(--test)
if ! ./scripts/pg-backup.sh "${backup_args[@]}"; then
  echo "백업하지 못했다. 되돌릴 자리가 없으므로 표를 바꾸는 단계로 넘어가지 않는다." >&2
  exit 1
fi

# 저장소 초기화를 다시 적용한다.
#
# 이 두 파일은 데이터 자리가 빌 때만 저절로 돈다. 두 번째 배포부터는 스키마나
# 권한이 바뀌어도 반영되지 않는데, 아래의 표 개수 확인은 그대로 통과한다.
# 둘 다 몇 번을 돌려도 같은 결과라 매번 건다.
#
# psql 은 파일 안에서 오류가 나도 0 으로 끝난다. 그대로 두면 초기화가 실패해도
# 다음 단계로 넘어가, 이 스크립트가 없애려는 조용한 통과를 새로 하나 만든다.
#
# 비밀번호는 환경파일에서 다시 준다. 컨테이너가 들고 있는 값은 만들어질 때
# 박힌 것이라, 환경파일만 고친 상태에서 재적용하면 옛 값으로 되맞춰 버린다.
echo "[$LABEL] 저장소 초기화 다시 적용"
dc exec -T postgres psql -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" \
  -f /docker-entrypoint-initdb.d/00-create-schemas.sql
dc exec -T \
  -e MAP_ADMIN_PASSWORD="$(grep -E '^MAP_ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)" \
  postgres bash /docker-entrypoint-initdb.d/10-admin.sh

echo "[$LABEL] 3/4 hub 표 만들기"
# shellcheck source=scripts/lib/migrations.sh
source ./scripts/lib/migrations.sh
verify_hub_migration || exit 1

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
application_up() {
  if [ -n "${INFRA_IMAGE_BUNDLE:-}" ]; then
    # PostgreSQL/Redis were checked above. Updating applications must not recreate
    # their containers merely because the image spelling changed from tag to ID.
    local services=(user agent hub proxy)
    [ "$VISION" = 0 ] || services+=(yolo)
    [ "$EDGE" = 0 ] || services+=(edge dns)
    dc "${PROFILES[@]}" up -d --no-deps --wait --wait-timeout 180 "${services[@]}"
  else
    dc "${PROFILES[@]}" up -d --wait --wait-timeout 180
  fi
}
if ! application_up; then
  echo "정해진 시간 안에 정상이 되지 않았다. 어느 서비스인지 아래에서 보고" >&2
  echo "그 서비스의 기록을 본다: dc logs <서비스>" >&2
  dc ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}' >&2
  exit 1
fi

# 성공한 배포를 한 줄씩 덧붙인다. 시각 형식은 양쪽 date 가 같게 받는 것을 쓴다. 되돌릴 때 이전 판 이름을 찾을 데가 없어서,
# 지금까지는 실행 기록을 뒤지는 수밖에 없었다. 바로 앞 줄이 되돌릴 자리다.
{
  printf '%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LABEL" \
    "$(grep -E '^IMAGE_TAG=.+' "$ENV_FILE" | cut -d= -f2- || echo 만들어씀)" \
    "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
} >> ./.deploy-history

# 서비스 스택은 여기서 끝났다. 콘솔은 그 위에 얹는 별개의 스택이라, 배포
# 기록을 남긴 뒤에 세운다 — 콘솔이 서지 못해도 서비스 배포 자체는 성립한
# 것이고, 그 사실이 기록에 남아야 되돌릴 자리를 찾을 수 있다.
#
# 콘솔에 필요한 저장소 역할과 스키마 소유권은 위의 초기화 재적용 단계에서
# 이미 맞춰졌다. 콘솔 자신의 표는 컨테이너가 뜨면서 스스로 손질한다.
if [ "$ADMIN" = 1 ]; then
  echo
  echo "[$LABEL] 콘솔 기동"
  if ! dca "${ADMIN_PROFILES[@]}" up -d --wait --wait-timeout 180; then
    echo "콘솔이 정해진 시간 안에 정상이 되지 않았다." >&2
    echo "서비스 스택은 이미 서 있다 — 콘솔만 다시 보면 된다:" >&2
    echo "  docker compose --env-file $ENV_FILE ${ADMIN_FILES[*]} logs admin" >&2
    dca ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}' >&2
    exit 1
  fi
fi

echo
dc ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}\t{{.Image}}'
if [ "$ADMIN" = 1 ]; then
  dca ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}\t{{.Image}}'
fi

if [ "$MICRO" = 1 ]; then
  echo
  echo "메모리가 작은 서버다. 상태가 정상이어도 실제 요청을 한 번 보내 본다 —"
  echo "메모리가 모자라 앱이 죽어도 컨테이너는 살아 있고 자원 한도에 걸린"
  echo "표시도 남지 않아, 겉으로는 정상으로 보인다."
fi
