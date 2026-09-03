#!/usr/bin/env bash
# 운영 스택과 시험 스택이 실제로 갈라져 있는지 확인한다.
#
# 두 스택을 프로파일 전부 켠 상태로 렌더링해서, 겹치면 안 되는 다섯 축의
# 교집합이 비어 있는지 본다. 프로파일을 전부 켜는 이유는 평소에 뜨지 않는
# 서비스(osrm-*, yolo)가 갈라지지 않은 채로 남아 있어도 통과해 버리기 때문이다.
# 그 상태로 나중에 --profile routing 을 붙이면 시험이 운영 그래프를 물게 된다.
#
# 만들어 쓰는 조합과 받아 쓰는 조합을 둘 다 본다. 실제 배포는 받아 쓰는
# 쪽인데 그 조합을 한 번도 안 보면, 정작 서버에 올라가는 구성이 검사 밖에
# 남는다.
#
# 도커 데몬은 필요 없다. 렌더링만 한다.
set -euo pipefail

cd "$(dirname "$0")/.."

PROD_ENV=${PROD_ENV:-./.env}
TEST_ENV=${TEST_ENV:-./.env.test}
PROFILES=(--profile infra --profile backend --profile full --profile vision --profile routing)

for f in "$PROD_ENV" "$TEST_ENV"; do
  [ -f "$f" ] || { echo "환경파일이 없다: $f" >&2; exit 1; }
done

# 받아 쓰는 조합은 어느 판을 받을지가 비어 있으면 렌더링 자체가 멈춘다.
# 이 검사는 이름이 겹치는지만 보므로 값의 내용은 상관없다. 여기서만 세운다.
export IMAGE_REGISTRY="${IMAGE_REGISTRY:-겹침검사}"
export IMAGE_TAG="${IMAGE_TAG:-겹침검사}"

# 받아 쓰는 조합에서는 카메라 갈래를 빼고 본다. 그 갈래는 만들어 올리는
# 목록에 없어서, 켜면 렌더링이 멈추는 것이 정상 동작이다. 그 사실 자체는
# 아래에서 따로 확인한다.
RENDER_PROFILES=("${PROFILES[@]}")

render() {  # $1=환경파일  $2..=덧칠 파일들
  local env=$1; shift
  local files=(-f docker-compose.yml)
  local f
  for f in "$@"; do files+=(-f "$f"); done
  docker compose --env-file "$env" "${files[@]}" "${RENDER_PROFILES[@]}" config --format json
}

compare() {  # $1=이름  $2=운영 json  $3=시험 json
  echo "### $1"
  LABEL="$1" PROD_JSON="$2" TEST_JSON="$3" python3 - <<'AXES'
import json, os, sys

def axes(doc):
    """겹치면 안 되는 다섯 축을 뽑는다."""
    name = doc.get("name", "")
    out = {"프로젝트명": {name}, "컨테이너명": set(), "호스트포트": set(),
           "볼륨": set(), "네트워크": set()}
    for svc, body in (doc.get("services") or {}).items():
        # container_name 을 풀어 두면 compose 가 <프로젝트>-<서비스>-<번호> 로 만든다.
        # 그 자동 이름까지 비교해야 "이름을 풀었을 뿐 여전히 겹치는" 경우를 잡는다.
        out["컨테이너명"].add(body.get("container_name") or f"{name}-{svc}-1")
        for p in body.get("ports") or []:
            pub = p.get("published") if isinstance(p, dict) else None
            if pub:
                out["호스트포트"].add(str(pub))
    for _, body in (doc.get("volumes") or {}).items():
        out["볼륨"].add((body or {}).get("name") or "")
    for _, body in (doc.get("networks") or {}).items():
        out["네트워크"].add((body or {}).get("name") or "")
    for k in out:
        out[k].discard("")
    return out

prod = axes(json.loads(os.environ["PROD_JSON"]))
test = axes(json.loads(os.environ["TEST_JSON"]))

bad = 0
for axis in prod:
    dup = sorted(prod[axis] & test[axis])
    if dup:
        bad += len(dup)
        print(f"  겹침 [{axis}] {', '.join(dup)}")
    else:
        print(f"  통과 [{axis}] 운영 {len(prod[axis])}개 · 시험 {len(test[axis])}개")

if bad:
    print(f"겹침 {bad}건 — 시험 스택이 운영을 침범한다", file=sys.stderr)
    sys.exit(1)
AXES
}

compare "만들어 쓰기" \
  "$(render "$PROD_ENV")" \
  "$(render "$TEST_ENV" docker-compose.test.yml)"

RENDER_PROFILES=(--profile infra --profile backend --profile full --profile routing)
compare "받아 쓰기" \
  "$(render "$PROD_ENV" docker-compose.registry.yml)" \
  "$(render "$TEST_ENV" docker-compose.test.yml docker-compose.registry.yml docker-compose.micro.yml)"

# 받아 쓰는 조합에 카메라 갈래를 켜면 받을 이름이 있어야 한다. 이름이 없으면
# 만드는 자리로 되돌아가, 작은 서버가 모델까지 든 이미지를 그 자리에서 짓는다.
vision_render=$(docker compose --env-file "$PROD_ENV" \
  -f docker-compose.yml -f docker-compose.registry.yml \
  --profile vision config --format json 2>/dev/null) || {
  echo "받아 쓰기에서 카메라 갈래가 그려지지 않는다" >&2
  exit 1
}
printf '%s' "$vision_render" | python3 -c "
import json,sys
y=json.load(sys.stdin)['services']['yolo']
if y.get('build'):
    print('카메라 갈래가 아직 만드는 자리를 들고 있다', file=sys.stderr); sys.exit(1)
if 'map-service-yolo:' not in (y.get('image') or ''):
    print('카메라 갈래에 받아올 이름이 없다', file=sys.stderr); sys.exit(1)
" || exit 1

echo "겹침 0건"
