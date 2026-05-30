#!/usr/bin/env bash
# OSRM 1회 빌드 스크립트 (foot · bicycle 프로파일).
#
# 무엇을 하는가:
#   OSM(OpenStreetMap) PBF 파일을 내려받아, OSRM 의 MLD 알고리즘이 요구하는
#   3단계 전처리를 컨테이너로 실행한다.
#     ① osrm-extract  — PBF → 내부 그래프 변환 (프로파일 lua 적용)
#     ② osrm-partition — 그래프 셀 분할
#     ③ osrm-customize — 비용/가중치 적용
#   산출물 *.osrm 파일들은 ./osrm-data/{foot,bicycle}/ 에 저장되어,
#   docker-compose.yml 의 osrm-foot / osrm-bicycle 서비스가 read-only 볼륨으로
#   마운트하여 사용한다.
#
# 언제 실행하는가:
#   인프라 최초 구축 시 1회. 이후 지도 데이터를 갱신할 때마다 재실행한다.
#
# 사용:
#   GEOFABRIK_PBF=<PBF URL> ./scripts/osrm-rebuild.sh
#   환경변수 미지정 시 기본 PBF(한국 전역)를 다운로드한다.
#
# 도메인 용어:
#   PBF       — Protocolbuffer Binary Format, OSM 의 압축 바이너리 포맷
#   MLD       — Multi-Level Dijkstra, OSRM 의 대규모 그래프용 라우팅 알고리즘
#   프로파일   — 이동 수단별 비용 함수(.lua). foot=도보, bicycle=자전거

# set -e: 명령 실패 시 즉시 중단
# set -u: 미정의 변수 참조 시 에러
# set -o pipefail: 파이프라인 중간 실패도 감지
set -euo pipefail

# PBF_URL — 다운로드할 OSM 데이터의 URL. 환경변수로 덮어쓸 수 있다.
PBF_URL="${GEOFABRIK_PBF:-https://download.geofabrik.de/asia/south-korea-latest.osm.pbf}"
# DATA_DIR — 산출물을 적재할 절대 경로. 스크립트 위치 기준 상위 디렉터리의 osrm-data.
DATA_DIR="$(cd "$(dirname "$0")/.." && pwd)/osrm-data"
# OSRM_IMAGE — extract/partition/customize 를 실행할 컨테이너 이미지 태그.
OSRM_IMAGE="osrm/osrm-backend:v5.27.1"

mkdir -p "$DATA_DIR"

# 1단계: 원본 PBF 다운로드.
# 같은 PBF 를 foot/bicycle 두 프로파일이 공유하므로 한 번만 받는다.
echo "[1/3] Downloading PBF from $PBF_URL"
curl -L --fail -o "$DATA_DIR/korea.osm.pbf" "$PBF_URL"

# 2~3단계: 두 프로파일(foot, bicycle) 각각에 대해 동일한 전처리를 반복.
# 컨테이너 내부의 /opt/foot.lua / /opt/bicycle.lua 가 비용 함수.
for profile in foot bicycle; do
  echo "[2/3] Preparing $profile profile"
  mkdir -p "$DATA_DIR/$profile"
  # 프로파일별 디렉터리에 PBF 사본을 두어야 osrm-extract 가 같은 디렉터리에
  # 산출물(.osrm*)을 생성할 수 있다.
  cp "$DATA_DIR/korea.osm.pbf" "$DATA_DIR/$profile/korea.osm.pbf"

  # extract → partition → customize 순서는 OSRM 의 규약이며 변경 불가.
  # 각 단계는 새 컨테이너로 일회성 실행 후 --rm 으로 제거된다.
  echo "[3/3] Running osrm-extract / partition / customize for $profile"
  docker run --rm -v "$DATA_DIR/$profile:/data" "$OSRM_IMAGE" \
    osrm-extract -p "/opt/${profile}.lua" /data/korea.osm.pbf
  docker run --rm -v "$DATA_DIR/$profile:/data" "$OSRM_IMAGE" \
    osrm-partition /data/korea.osrm
  docker run --rm -v "$DATA_DIR/$profile:/data" "$OSRM_IMAGE" \
    osrm-customize /data/korea.osrm
done

# 완료 안내. 위 절차가 끝난 후 osrm-foot / osrm-bicycle 컨테이너를 띄울 수 있다.
echo
echo "OSRM 빌드 완료. 다음 명령으로 OSRM 서비스를 가동한다:"
echo "  docker compose --profile infra up -d osrm-foot osrm-bicycle"
