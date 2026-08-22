#!/usr/bin/env python3
"""시연에서 쓸 조건을 미리 돌려 캐시에 넣어 둔다.

왜 필요한가:
  무료 등급의 분당 한도가 10 이고 일정 한 건이 3회를 쓴다. 서로 다른 조건으로
  동시에 4명이 누르면 한도를 넘고, 넘친 요청은 최대 60초 기다리다 실패한다.
  시연 자리에서 이것이 나면 되돌릴 방법이 없다.

  미리 돌려 두면 그 조건은 캐시로 답한다. 모델을 부르지 않으므로 한도와
  무관하고, 응답도 즉시다. 몇 명이 동시에 눌러도 같다.

★ 날짜가 캐시 키에 들어간다 ★
  지역·테마·이동수단만 맞아서는 걸리지 않는다. **시연에서 실제로 고를 날짜와
  같은 날짜로 미리 돌려야 한다.** 날짜가 하루라도 다르면 캐시를 비껴간다.
  예산은 5만 원 단위, 시각은 1시간 단위로 뭉뚱그려지므로 그 둘은 조금 달라도
  걸린다.

캐시는 7일 보관하므로 시연 전날 돌려 두면 된다.

사용:
  python3 scripts/demo-prewarm.py --scenarios scripts/demo-scenarios.json
  python3 scripts/demo-prewarm.py --scenarios ... --check-only
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# 일정 한 건이 모델을 3회 쓴다. 분당 10 이므로 한 건에 20초를 두면 넘지 않는다.
# 넘으면 예열하는 도중에 한도를 태워, 정작 시연 때 쓸 몫이 줄어든다.
DEFAULT_PACE_SECONDS = 20.0


def post(url: str, body: dict, timeout: float) -> tuple[int, dict | None, float]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return resp.status, payload, time.monotonic() - started
    except urllib.error.HTTPError as e:
        return e.code, None, time.monotonic() - started
    except Exception:
        return 0, None, time.monotonic() - started


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--scenarios", required=True)
    ap.add_argument("--pace", type=float, default=DEFAULT_PACE_SECONDS)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--check-only", action="store_true",
                    help="돌리지 않고, 이미 캐시에 있는지만 본다")
    args = ap.parse_args()

    scenarios = json.loads(open(args.scenarios, encoding="utf-8").read())
    if not scenarios:
        print("✗ 시나리오가 비어 있다")
        return 1

    url = f"{args.base}/api/v1/trip/generate"
    print(f"대상 {url} · 시나리오 {len(scenarios)}건 · 간격 {args.pace}초\n")

    warm = cold = failed = 0
    for i, sc in enumerate(scenarios, 1):
        label = sc.pop("_label", f"시나리오{i}")
        status, payload, elapsed = post(url, sc, args.timeout)

        if status != 200:
            print(f"  ✗ {label}: HTTP {status} ({elapsed:.1f}초)")
            failed += 1
        else:
            stops = len(payload.get("stops") or []) if payload else 0
            # 캐시로 답하면 모델을 부르지 않아 눈에 띄게 빠르다. 시간만으로
            # 단정하지는 않고 참고로만 표시한다.
            hint = "캐시로 답한 듯" if elapsed < 1.5 else "새로 만듦"
            print(f"  ✓ {label}: 장소 {stops}개 · {elapsed:.1f}초 ({hint})")
            if elapsed < 1.5:
                warm += 1
            else:
                cold += 1

        if args.check_only:
            continue
        if i < len(scenarios):
            time.sleep(args.pace)

    print(f"\n예열 {cold}건 · 이미 더움 {warm}건 · 실패 {failed}건")
    if failed:
        print("실패한 시나리오는 시연에서 그대로 실패한다. 원인을 보고 다시 돌릴 것.")
        return 1
    print("같은 날짜·지역·테마·이동수단으로 눌러야 캐시에 걸린다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
