#!/usr/bin/env bash
# MAP PoC 스택 정리: 외부 노출 → flutter 세션 → 에뮬레이터 → 로컬 BFF(8080) →
# 도커 백엔드 순 정지. 데이터 볼륨(postgres/redis)은 보존한다.
set -uo pipefail

# 경로는 스크립트 위치 기준으로 계산(실행 cwd 무관).
# INFRA_DIR = 이 파일의 상위 디렉터리 = map-service-infra (docker-compose.yml 소유).
INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADB="$HOME/Library/Android/sdk/platform-tools/adb"
SERVICE_COMPOSE=(docker compose -p map-service -f "$INFRA_DIR/docker-compose.yml")
ADMIN_COMPOSE=(docker compose -p map-admin -f "$INFRA_DIR/docker-compose.admin.yml")
APP_PKG="kr.mapservice.client"
cleanup_failed=0

# 8080 은 토폴로지 A에서 Docker Desktop 백엔드가 대신 listen 할 수 있다. 따라서
# 포트 소유자라는 이유만으로 종료하지 않고, 정확한 Spring Boot main class가
# 명령행에 함께 보이는 Java 프로세스만 호스트 BFF로 인정한다.
is_map_host_bff_pid() {
  local pid="$1" command
  command="$(ps -ww -p "$pid" -o command= 2>/dev/null)" || return 1
  [[ "$command" == *"java"* ]] &&
    [[ "$command" == *"map.service.user.ServiceUserApplication"* ]]
}

echo "== [1/5] 외부 노출 종료 =="
# 스택을 내리면 터널이 가리킬 곳이 없어진다. 남겨 두면 폰의 앱이 죽은 주소로
# 계속 찾아가고, 터널을 지켜보던 잠자기 방지도 풀리지 않는다.
"$INFRA_DIR/scripts/map-serve-down.sh" 2>&1 | sed 's/^/  /'

echo "== [2/5] flutter run / 앱 종료 =="
# flutter run 은 dart VM(flutter_tools.snapshot)으로 동작 → 해당 프로세스 패턴으로 종료.
pkill -f "flutter_tools.snapshot" 2>/dev/null && echo "  flutter run 세션 종료" || echo "  flutter run 세션 없음"
# 앱은 force-stop 만 한다(설치는 유지 → 다음 기동 시 재설치 불필요).
if [ -x "$ADB" ]; then
  "$ADB" -s emulator-5554 shell am force-stop "$APP_PKG" 2>/dev/null && echo "  앱 force-stop(설치 유지)" || true
fi

echo "== [3/5] 에뮬레이터 종료 =="
if [ -x "$ADB" ] && "$ADB" devices 2>/dev/null | grep -q "emulator-5554"; then
  "$ADB" -s emulator-5554 emu kill 2>/dev/null && echo "  에뮬레이터 종료 신호 전송" || true
else
  echo "  실행 중 에뮬레이터 없음"
fi

echo "== [4/5] BFF(8080) 종료 =="
# 로컬 bootRun 의 실제 LISTEN JVM만 선별한다. 컨테이너 BFF를 쓸 때 8080을
# 소유하는 com.docker.backend 등 다른 프로세스는 그대로 둔다.
listener_pids="$(lsof -nP -tiTCP:8080 -sTCP:LISTEN 2>/dev/null || true)"
host_bff_pids=()
for pid in $listener_pids; do
  if is_map_host_bff_pid "$pid"; then host_bff_pids+=("$pid"); fi
done

if [ "${#host_bff_pids[@]}" -eq 0 ]; then
  if [ -n "$listener_pids" ]; then
    echo "  8080 사용 프로세스는 MAP 호스트 BFF가 아님 — 종료하지 않음"
  else
    echo "  호스트 BFF 없음"
  fi
else
  echo "  호스트 BFF 종료 신호 전송(pid ${host_bff_pids[*]})"
  for pid in "${host_bff_pids[@]}"; do
    if is_map_host_bff_pid "$pid" && ! kill -TERM "$pid" 2>/dev/null; then
      # 판정과 kill 사이에 정상 종료된 경합은 실패가 아니다.
      if is_map_host_bff_pid "$pid"; then
        echo "  · 호스트 BFF TERM 실패 — 강제 종료 단계에서 재확인(pid $pid)"
      fi
    fi
  done

  # Spring 종료 유예 후에도 같은 프로세스가 살아 있을 때만 강제 종료한다.
  attempt=0
  while [ "$attempt" -lt 10 ]; do
    alive=0
    for pid in "${host_bff_pids[@]}"; do
      if is_map_host_bff_pid "$pid"; then alive=1; break; fi
    done
    [ "$alive" -eq 0 ] && break
    sleep 1
    attempt=$((attempt + 1))
  done
  for pid in "${host_bff_pids[@]}"; do
    if is_map_host_bff_pid "$pid"; then
      echo "  · 종료 유예 초과 — 확인된 호스트 BFF 강제 종료(pid $pid)"
      kill -KILL "$pid" 2>/dev/null || true
      sleep 1
      if is_map_host_bff_pid "$pid"; then
        echo "  ✗ 호스트 BFF 종료 실패(pid $pid)"
        cleanup_failed=1
      fi
    fi
  done
fi

echo "== [5/5] 도커 스택 종료 (컨테이너/네트워크 제거, 볼륨 유지) =="
# 관리자 스택을 먼저 내린다. map-net 은 서비스 스택이 소유하므로 순서가 바뀌면
# 네트워크가 사용 중이라 제거되지 않는다.
if ! docker info >/dev/null 2>&1; then
  echo "  ✗ Docker 데몬 미응답 — 컨테이너를 제거하지 못함"
  echo "    Docker Desktop을 시작한 뒤 이 스크립트를 다시 실행한다."
  cleanup_failed=1
else
  echo "  관리자 스택..."
  "${ADMIN_COMPOSE[@]}" --profile monitoring down --remove-orphans 2>&1 | sed 's/^/    /'
  admin_down_status="${PIPESTATUS[0]}"
  if [ "$admin_down_status" -ne 0 ]; then
    echo "  ✗ 관리자 스택 종료 실패(exit $admin_down_status)"
    cleanup_failed=1
  fi

  # 서비스가 profiles(infra/backend/full/vision/routing)로 묶여 있어, 활성 프로파일
  # 없이 `down` 하면 대상 서비스가 0개로 해석돼 아무것도 제거되지 않는다 →
  # 전 프로파일을 활성화해야 한다.
  # --remove-orphans: 과거 잔여 컨테이너(예: 도커로 띄웠던 user)까지 정리.
  echo "  서비스 스택..."
  COMPOSE_PROFILES=infra,backend,full,vision,routing \
    "${SERVICE_COMPOSE[@]}" down --remove-orphans 2>&1 | sed 's/^/    /'
  service_down_status="${PIPESTATUS[0]}"
  if [ "$service_down_status" -ne 0 ]; then
    echo "  ✗ 서비스 스택 종료 실패(exit $service_down_status)"
    cleanup_failed=1
  fi

  # down 명령이 0이어도 프로젝트 컨테이너가 남으면 성공으로 보고하지 않는다.
  for project in map-admin map-service; do
    remaining="$(docker ps -a \
      --filter "label=com.docker.compose.project=$project" \
      --format '{{.Names}}' 2>/dev/null)"
    ps_status=$?
    if [ "$ps_status" -ne 0 ]; then
      echo "  ✗ $project 잔여 컨테이너 확인 실패"
      cleanup_failed=1
    elif [ -n "$remaining" ]; then
      echo "  ✗ $project 잔여 컨테이너:"
      echo "$remaining" | sed 's/^/    - /'
      cleanup_failed=1
    fi
  done
fi

echo ""
if [ "$cleanup_failed" -ne 0 ]; then
  echo "== ❌ 정리 미완료. 위 실패 원인을 해결한 뒤 다시 실행한다. =="
  exit 1
fi
echo "== ✅ 정리 완료. 다시 띄우려면: map-up-1-backend.sh =="
