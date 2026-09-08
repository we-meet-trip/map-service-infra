#!/usr/bin/env bash
# 이미지를 만들어 받아갈 곳에 올린다.
#
# 평소에는 사람이 직접 부르지 않는다 — 레포의 배포 실행이 이 스크립트를
# 부른다. 만드는 절차를 그쪽에 다시 쓰지 않고 여기 하나만 두는 이유는,
# 두 벌이 되면 한쪽만 고쳐진 채 서로 다른 결과를 내기 때문이다.
# 직접 부르는 것은 그 실행을 쓸 수 없을 때다 — 아직 올리지 않은 로컬
# 상태로 만들어야 하거나, 받아갈 곳에 닿지 못할 때.
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
#
# 여섯 서비스를 모두 만든다. 카메라 인식이 배포 구성에 들어가면서 그 이미지도
# 서버가 받아 쓸 것이 되었고, 운영 콘솔 둘도 같은 이유로 들어왔다.
#
# 콘솔은 앞뒤 두 이미지로 나뉜다. 뒤(admin)는 파이썬 API 이고 앞(admin-web)은
# 화면을 만들어 nginx 에 얹은 것이라, 만드는 자리가 서로 다르다. 한 이미지로
# 합칠 수 없어 둘로 둔다.
set -euo pipefail

cd "$(dirname "$0")/.."

: "${IMAGE_REGISTRY:?받아갈 곳을 IMAGE_REGISTRY 로 정한다 (예: ghcr.io/이름)}"
IMAGE_TAG=${IMAGE_TAG:-latest}
PLATFORM=${IMAGE_PLATFORM:-linux/amd64}
SERVICES=${SERVICES:-user agent hub yolo admin admin-web}

declare -A CONTEXT=(
  [user]=../map-service-user
  [agent]=../map-service-agent
  [hub]=../map-service-hub
  [yolo]=../map-service-yolo
  [admin]=../map-service-admin
  [admin-web]=../map-service-admin/web
)

# 커밋을 읽을 자리. 화면 쪽은 만드는 자리가 레포 안쪽 폴더라 그 자리에서
# 커밋을 물으면 레포 뿌리의 값이 나오는데, 그 값이 맞다 — 같은 레포다.
declare -A REV_DIR=(
  [admin-web]=../map-service-admin
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

reuse_dir=""
cleanup_reuse() { if [ -n "$reuse_dir" ]; then rm -rf -- "$reuse_dir"; fi; }
trap cleanup_reuse EXIT
if [ -n "${RELEASE_REUSE_BUNDLE:-}" ]; then
  [ "$PLATFORM" = linux/amd64 ] || { echo 'Verified reuse requires linux/amd64' >&2; exit 2; }
  [ "$IMAGE_REGISTRY" = ghcr.io/we-meet-trip ] || { echo 'Verified reuse requires the release registry' >&2; exit 2; }
  # This UUID temp directory is created by this process and contains only the
  # generated one-line Dockerfiles and public provenance, never serving data.
  reuse_dir=$(mktemp -d)
  python3 scripts/prepare-image-reuse.py --bundle "$RELEASE_REUSE_BUNDLE" \
    --source-root .. --output "$reuse_dir/plan"
fi

for svc in $SERVICES; do
  ref="$IMAGE_REGISTRY/map-service-$svc:$IMAGE_TAG"
  echo "== $svc → $ref ($PLATFORM)"
  # --push 로 곧바로 올린다. 중간에 로컬로 받아 두면 다른 구조의 이미지가
  # 로컬 이름을 차지해, 이후 로컬 실행이 조용히 그것을 쓴다.
  # 서버에서 무엇이 도는지 되짚을 유일한 자리다. 판 이름은 셋이 같아서 어느
  # 이미지가 어느 커밋인지 가리지 못한다. 만든 자리의 커밋을 이미지 안에 박는다.
  #
  # 만든 자리가 저장소가 아니면(압축 해제본 등) 되짚을 값이 없다는 사실만
  # 남기고 계속 간다 — 그것 때문에 만들기 자체가 멈추면 안 된다.
  # 손으로 부를 때 작업 트리가 더러우면 이 값과 실제로 만든 내용이 다르다.
  rev_dir=${REV_DIR[$svc]:-${CONTEXT[$svc]}}
  rev=$(git -C "$rev_dir" rev-parse HEAD 2>/dev/null || echo unknown)
  src=$(git -C "$rev_dir" remote get-url origin 2>/dev/null || echo unknown)
  build_context=${CONTEXT[$svc]}
  if [ -n "$reuse_dir" ] && [ -f "$reuse_dir/plan/$svc/Dockerfile" ]; then
    build_context="$reuse_dir/plan/$svc"
  fi
  docker buildx build --platform "$PLATFORM" -t "$ref" \
    --label "org.opencontainers.image.revision=$rev" \
    --label "org.opencontainers.image.version=$IMAGE_TAG" \
    --label "org.opencontainers.image.source=$src" \
    --push "$build_context"
done

echo
echo "서버에서는 이렇게 받아 쓴다:"
echo "  IMAGE_REGISTRY=$IMAGE_REGISTRY IMAGE_TAG=$IMAGE_TAG \\"
echo "    docker compose --env-file ./.env \\"
echo "      -f docker-compose.yml -f docker-compose.registry.yml \\"
echo "      --profile full pull"
