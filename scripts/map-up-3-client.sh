#!/usr/bin/env bash
# MAP PoC Stage 3: Android 에뮬레이터 부팅 + 앱 실행(flutter run, 포그라운드).
# API_BASE_URL 을 호스트(에뮬레이터에서 10.0.2.2:8080)로 주입한다. 종료 q 또는 Ctrl+C.
set -uo pipefail

# INFRA_DIR = map-service-infra(.env). MAP_ROOT = 그 상위(형제 레포 map-service-client 접근용).
INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAP_ROOT="$(cd "$INFRA_DIR/.." && pwd)"
ADB="$HOME/Library/Android/sdk/platform-tools/adb"
EMU_ID="${EMU_ID:-Pixel_7}"
# 지도 키: 명시 환경변수 우선, 없으면 infra/.env 의 NAVER_CLIENT_ID(없으면 빈 값 → 타일만 미인증).
NAVER_KEY="${NAVER_MAP_CLIENT_ID:-$(grep -E '^NAVER_CLIENT_ID=' "$INFRA_DIR/.env" 2>/dev/null | cut -d= -f2-)}"

# 앱은 호스트의 BFF 를 10.0.2.2 로 호출하므로 BFF 준비를 선검사.
if [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/actuator/health)" != "200" ]; then
  echo "✗ BFF(8080) 미응답. 먼저 Stage 1·2(map-up-1-backend.sh, map-up-2-bff.sh) 실행."; exit 1
fi

# 이미 부팅된 에뮬레이터가 있으면 재사용, 없으면 부팅.
if [ -x "$ADB" ] && "$ADB" devices 2>/dev/null | grep -q "emulator-5554[[:space:]]*device"; then
  echo "  ✓ 에뮬레이터(emulator-5554) 실행 중"
else
  echo "== 에뮬레이터($EMU_ID) 부팅 =="
  # `flutter emulators | grep` 사전검사는 비-TTY(nohup)+pipefail 환경에서 오탐(없음)이 날 수
  # 있으므로 쓰지 않는다. 직접 --launch 하고 종료코드로만 판정(파이프 없음 → pipefail 영향 없음).
  if ! flutter emulators --launch "$EMU_ID"; then
    echo "  ✗ 에뮬레이터 '$EMU_ID' 실행 실패. 사용 가능 목록:"
    flutter emulators 2>&1 | sed 's/^/    /'
    exit 1
  fi
  echo "  부팅 대기..."
  for i in $(seq 1 40); do
    [ "$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && { echo "  ✓ 부팅 완료"; break; }
    sleep 3
  done
fi

echo "== flutter run (API_BASE_URL=http://10.0.2.2:8080) — 종료 q =="
[ -z "$NAVER_KEY" ] && echo "  (NAVER 지도 키 없음 → 타일 미인증, 경로/마커는 표시)"
cd "$MAP_ROOT/map-service-client"
# exec 로 현재 셸을 대체 → q/Ctrl+C 가 flutter 프로세스에 직접 전달된다.
exec flutter run -d emulator-5554 \
  --dart-define=API_BASE_URL=http://10.0.2.2:8080 \
  --dart-define=NAVER_MAP_CLIENT_ID="$NAVER_KEY"
