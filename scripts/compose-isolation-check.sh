#!/usr/bin/env bash
# 운영 스택과 시험 스택이 실제로 갈라져 있는지 확인한다.
#
# 두 스택을 프로파일 전부 켠 상태로 렌더링해서, 겹치면 안 되는 다섯 축의
# 교집합이 비어 있는지 본다. 프로파일을 전부 켜는 이유는 평소에 뜨지 않는
# 서비스(osrm-*, yolo)가 갈라지지 않은 채로 남아 있어도 통과해 버리기 때문이다.
# 그 상태로 나중에 --profile routing 을 붙이면 시험이 운영 그래프를 물게 된다.
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

render() {
  docker compose --env-file "$1" -f docker-compose.yml ${2:+-f "$2"} \
    "${PROFILES[@]}" config --format json
}

PROD_JSON=$(render "$PROD_ENV" "")
TEST_JSON=$(render "$TEST_ENV" docker-compose.test.yml)

PROD_JSON="$PROD_JSON" TEST_JSON="$TEST_JSON" python3 - <<'PY'
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
print("겹침 0건")
PY
