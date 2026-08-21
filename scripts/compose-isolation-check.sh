#!/usr/bin/env bash
# 운영 스택과 시험 스택이 정말 겹치지 않는지 확인한다.
#
# 왜 필요한가:
#   두 스택을 가르는 축이 다섯 개(프로젝트명·볼륨·네트워크·컨테이너명·포트)인데,
#   원본 compose 가 볼륨 이름과 컨테이너명을 값으로 못박아 두어 프로젝트명만
#   바꾸면 갈라지지 않는다. 특히 볼륨이 겹치면 시험 스택이 운영 데이터베이스를
#   물고 올라오고, 그 상태의 마이그레이션은 되돌릴 수 없다.
#   다섯 축을 매번 사람이 빠짐없이 대조하는 것은 기대할 수 없어 기계가 본다.
#
# 무엇을 하는가:
#   두 스택을 각각 렌더링해서 컨테이너명·네트워크·볼륨·호스트포트를 뽑고,
#   교집합이 비어 있는지 본다. 하나라도 겹치면 그 항목을 찍고 실패한다.
#
# 도커 데몬이 없어도 된다 — 파일을 읽어 합치는 일만 한다.
#
# 사용: ./scripts/compose-isolation-check.sh
set -euo pipefail

cd "$(dirname "$0")/.."

BASE="docker-compose.yml"
TEST_OVERLAY="docker-compose.test.yml"
PROD_ENV="./.env"
TEST_ENV="./.env.test"

# 프로파일을 전부 켠다. 안 켜면 그 프로파일 서비스가 렌더링에서 빠져,
# 실제로는 겹치는데 검사만 통과하는 상태가 된다.
PROFILES=(--profile infra --profile backend --profile full --profile vision --profile routing)

for f in "$BASE" "$TEST_OVERLAY"; do
  [ -f "$f" ] || { echo "✗ $f 가 없다"; exit 1; }
done
[ -f "$PROD_ENV" ] || { echo "✗ $PROD_ENV 가 없다"; exit 1; }
[ -f "$TEST_ENV" ] || { echo "✗ $TEST_ENV 가 없다 — 먼저 만들어야 한다"; exit 1; }

render() {
  # $1: --env-file 값, 나머지: -f 파일들
  local envfile="$1"; shift
  local args=()
  for f in "$@"; do args+=(-f "$f"); done
  docker compose --env-file "$envfile" "${args[@]}" "${PROFILES[@]}" config --format json
}

echo "== 렌더링 =="
PROD_JSON="$(render "$PROD_ENV" "$BASE")" || { echo "✗ 운영 스택 렌더링 실패"; exit 1; }
TEST_JSON="$(render "$TEST_ENV" "$BASE" "$TEST_OVERLAY")" || { echo "✗ 시험 스택 렌더링 실패"; exit 1; }

echo "== 겹침 검사 =="
PROD_JSON="$PROD_JSON" TEST_JSON="$TEST_JSON" python3 - <<'PY'
import json, os, sys

def keys(raw):
    """렌더링 결과에서 겹치면 안 되는 것들을 한 집합으로 모은다."""
    doc = json.loads(raw)
    out = set()
    project = doc.get("name", "")
    out.add(("project", project))
    for svc, body in (doc.get("services") or {}).items():
        # container_name 을 비워 두면 compose 가 <프로젝트>-<서비스>-<번호> 로
        # 짓는다. 그 이름을 여기서 똑같이 만들어 비교한다 — 렌더링에 없다는
        # 이유로 건너뛰면, 이름을 비운 쪽은 늘 통과해 검사가 무의미해진다.
        cn = body.get("container_name") or f"{project}-{svc}-1"
        out.add(("container", cn))
        for p in (body.get("ports") or []):
            # 렌더링 결과의 ports 는 published/host_ip 를 가진 객체다.
            host_ip = p.get("host_ip") or "0.0.0.0"
            published = str(p.get("published") or "")
            if published:
                out.add(("port", f"{host_ip}:{published}"))
    for _, body in (doc.get("volumes") or {}).items():
        name = (body or {}).get("name")
        if name:
            out.add(("volume", name))
    for _, body in (doc.get("networks") or {}).items():
        name = (body or {}).get("name")
        if name:
            out.add(("network", name))
    return out

prod = keys(os.environ["PROD_JSON"])
test = keys(os.environ["TEST_JSON"])
shared = sorted(prod & test)

label = {"project": "프로젝트명", "container": "컨테이너명",
         "port": "호스트 포트", "volume": "볼륨", "network": "네트워크"}

if shared:
    print(f"✗ 겹치는 항목 {len(shared)}건")
    for kind, value in shared:
        print(f"    {label.get(kind, kind)}: {value}")
    sys.exit(1)

print(f"✓ 겹침 0건 (운영 {len(prod)}개 · 시험 {len(test)}개 항목 대조)")
PY

echo "== 통과 =="
