#!/usr/bin/env bash
# OSRM 1회 빌드 스크립트 (foot · bicycle 프로파일).
# OSM PBF를 다운로드하여 extract → partition → customize 단계를 실행하고,
# 산출물을 ./osrm-data/{foot,bicycle}/korea.osrm* 에 적재한다.
#
# 사용:
#   GEOFABRIK_PBF=<PBF URL> ./scripts/osrm-rebuild.sh

set -euo pipefail

PBF_URL="${GEOFABRIK_PBF:-https://download.geofabrik.de/asia/south-korea-latest.osm.pbf}"
DATA_DIR="$(cd "$(dirname "$0")/.." && pwd)/osrm-data"
OSRM_IMAGE="osrm/osrm-backend:latest"

mkdir -p "$DATA_DIR"

echo "[1/3] Downloading PBF from $PBF_URL"
curl -L --fail -o "$DATA_DIR/korea.osm.pbf" "$PBF_URL"

for profile in foot bicycle; do
  echo "[2/3] Preparing $profile profile"
  mkdir -p "$DATA_DIR/$profile"
  cp "$DATA_DIR/korea.osm.pbf" "$DATA_DIR/$profile/korea.osm.pbf"

  echo "[3/3] Running osrm-extract / partition / customize for $profile"
  docker run --rm -v "$DATA_DIR/$profile:/data" "$OSRM_IMAGE" \
    osrm-extract -p "/opt/${profile}.lua" /data/korea.osm.pbf
  docker run --rm -v "$DATA_DIR/$profile:/data" "$OSRM_IMAGE" \
    osrm-partition /data/korea.osrm
  docker run --rm -v "$DATA_DIR/$profile:/data" "$OSRM_IMAGE" \
    osrm-customize /data/korea.osrm
done

echo
echo "OSRM 빌드 완료. 다음 명령으로 OSRM 서비스를 가동한다:"
echo "  docker compose --profile infra up -d osrm-foot osrm-bicycle"
