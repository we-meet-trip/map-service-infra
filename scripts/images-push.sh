#!/usr/bin/env bash
# 이미지를 만들어 받아갈 곳에 올린다.
#
# 메모리가 작은 서버는 이미지를 만들 수 없어(BFF 빌드만 1GB 를 넘게 쓴다)
# 넉넉한 컴퓨터에서 만들어 두고 서버는 받아 쓰게 한다.
#
# 서버가 amd64 인데 만드는 컴퓨터가 애플 실리콘이면, 그냥 만든 이미지는
# arm64 라 서버에서 뜨지 않는다. 그래서 만들 때 목표 구조를 명시한다.
# 그 대신 흉내 내기로 만들게 되어 느리다 — 처음 한 번은 오래 걸린다.
#
# 사용:
#   IMAGE_REGISTRY=ghcr.io/<계정> ./scripts/images-push.sh
#   IMAGE_REGISTRY=... IMAGE_TAG=2026-09-01 ./scripts/images-push.sh
#   IMAGE_REGISTRY=... SERVICES="user hub" ./scripts/images-push.sh
set -euo pipefail

cd "$(dirname "$0")/.."

: "${IMAGE_REGISTRY:?받아갈 곳을 IMAGE_REGISTRY 로 정한다 (예: ghcr.io/이름)}"
IMAGE_TAG=${IMAGE_TAG:-latest}
PLATFORM=${IMAGE_PLATFORM:-linux/amd64}
SERVICES=${SERVICES:-user agent hub}

declare -A CONTEXT=(
  [user]=../map-service-user
  [agent]=../map-service-agent
  [hub]=../map-service-hub
  [yolo]=../map-service-yolo
)

# 만드는 자리를 미리 확인한다. 없는 채로 시작하면 앞의 것들만 올라가고
# 중간에 멈춰, 서버에는 판이 뒤섞인 이미지가 남는다.
for svc in $SERVICES; do
  ctx=${CONTEXT[$svc]:-}
  [ -n "$ctx" ] || { echo "모르는 서비스: $svc" >&2; exit 2; }
  [ -f "$ctx/Dockerfile" ] || { echo "만들 자리가 없다: $ctx" >&2; exit 1; }
done

# buildx 없이는 다른 구조로 만들 수 없다.
docker buildx version >/dev/null 2>&1 || {
  echo "docker buildx 가 필요하다" >&2; exit 1; }

for svc in $SERVICES; do
  ref="$IMAGE_REGISTRY/map-service-$svc:$IMAGE_TAG"
  echo "== $svc → $ref ($PLATFORM)"
  # --push 로 곧바로 올린다. 중간에 로컬로 받아 두면 다른 구조의 이미지가
  # 로컬 이름을 차지해, 이후 로컬 실행이 조용히 그것을 쓴다.
  docker buildx build --platform "$PLATFORM" -t "$ref" --push "${CONTEXT[$svc]}"
done

echo
echo "서버에서는 이렇게 받아 쓴다:"
echo "  IMAGE_REGISTRY=$IMAGE_REGISTRY IMAGE_TAG=$IMAGE_TAG \\"
echo "    docker compose --env-file ./.env \\"
echo "      -f docker-compose.yml -f docker-compose.registry.yml \\"
echo "      --profile full pull"
