#!/usr/bin/env bash
# MAP PoC 스택 정리: 외부 노출 → flutter 세션 → 에뮬레이터 → 로컬 BFF(8080) →
# 도커 백엔드 순 정지. 데이터 볼륨(postgres/redis)은 보존한다.
set -uo pipefail

# 경로는 스크립트 위치 기준으로 계산(실행 cwd 무관).
# INFRA_DIR = 이 파일의 상위 디렉터리 = map-service-infra (docker-compose.yml 소유).
INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADB="$HOME/Library/Android/sdk/platform-tools/adb"
COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml")
APP_PKG="kr.mapservice.client"

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
# 로컬 bootRun 은 8080 을 점유하는 JVM(gradle 자식). 포트 점유 PID 를 직접 종료한다.
pids="$(lsof -ti:8080 2>/dev/null)"
if [ -n "$pids" ]; then echo "$pids" | xargs kill -9 2>/dev/null; echo "  8080 점유 프로세스 종료"; else echo "  8080 점유 없음"; fi
pkill -f "bootRun" 2>/dev/null || true
pkill -f "ServiceUserApplication" 2>/dev/null || true

echo "== [5/5] 도커 스택 종료 (컨테이너/네트워크 제거, 볼륨 유지) =="
# 관리자 스택을 먼저 내린다. map-net 은 서비스 스택이 소유하므로 순서가 바뀌면
# 네트워크가 사용 중이라 제거되지 않는다.
echo "  관리자 스택..."
docker compose -f "$INFRA_DIR/docker-compose.admin.yml" --profile monitoring down --remove-orphans 2>&1 | sed 's/^/    /'
# 서비스가 profiles(infra/backend/full/vision/routing)로 묶여 있어, 활성 프로파일
# 없이 `down` 하면 대상 서비스가 0개로 해석돼 아무것도 제거되지 않는다 →
# 전 프로파일을 활성화해야 한다.
# --remove-orphans: 과거 잔여 컨테이너(예: 도커로 띄웠던 user)까지 정리.
echo "  서비스 스택..."
COMPOSE_PROFILES=infra,backend,full,vision,routing "${COMPOSE[@]}" down --remove-orphans 2>&1 | sed 's/^/    /'

echo ""
echo "== ✅ 정리 완료. 다시 띄우려면: map-up-1-backend.sh =="
