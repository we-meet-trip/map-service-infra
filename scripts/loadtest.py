#!/usr/bin/env python3
"""읽기 경로에 부하를 걸어 어디서 먼저 무너지는지 본다.

왜 필요한가:
  이 스택의 동시 수용량은 한 번도 재어진 적이 없다. 재지 않으면 컨테이너에
  걸 상한도, 목표 규모도 감으로 정하게 된다.

무엇을 때리는가 (기본값):
  GET /api/v1/recommend/{임의 UUID}
  이 경로는 Redis 를 보고 없으면 PG 를 보고 202 로 답한다. 바깥을 부르지도,
  모델을 돌리지도 않는다. 그래서 재는 것이 순수하게 "앱 + 커넥션 풀 + 저장소"
  가 된다.

무엇을 재지 않는가:
  일정 생성 경로. 한 건이 모델을 두 번 쓰고 하루 한도가 정해져 있어, 여기에
  부하를 걸면 재는 행위 자체가 서비스를 멈춘다. 그 경로의 한계는 스레드 점유
  방식에서 따로 따져야 한다(README 참조).

사용:
  python3 scripts/loadtest.py --url http://127.0.0.1:8090/api/v1/recommend
  python3 scripts/loadtest.py --levels 1,5,10,25,50 --seconds 8
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time
import uuid

import aiohttp


async def _worker(session, url, deadline, lat, errs, codes):
    while time.monotonic() < deadline:
        started = time.monotonic()
        try:
            async with session.get(f"{url}/{uuid.uuid4()}") as resp:
                await resp.read()
                codes[resp.status] = codes.get(resp.status, 0) + 1
        except Exception as e:  # 연결 거절·타임아웃 전부 실패로 센다
            errs.append(type(e).__name__)
        else:
            lat.append((time.monotonic() - started) * 1000)


async def run_level(url: str, concurrency: int, seconds: float) -> dict:
    lat: list[float] = []
    errs: list[str] = []
    codes: dict[int, int] = {}
    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=concurrency + 10)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        deadline = time.monotonic() + seconds
        await asyncio.gather(*[
            _worker(session, url, deadline, lat, errs, codes)
            for _ in range(concurrency)
        ])
    done = len(lat)
    ordered = sorted(lat)

    def pct(p: float) -> float:
        if not ordered:
            return float("nan")
        return ordered[min(len(ordered) - 1, int(len(ordered) * p))]

    return {
        "concurrency": concurrency,
        "requests": done,
        "rps": round(done / seconds, 1),
        "p50": round(statistics.median(ordered), 1) if ordered else None,
        "p95": round(pct(0.95), 1),
        "p99": round(pct(0.99), 1),
        "errors": len(errs),
        "error_kinds": sorted(set(errs))[:3],
        "codes": codes,
    }


async def main_async(args) -> None:
    levels = [int(x) for x in args.levels.split(",")]
    print(f"대상: {args.url}/<uuid>  ·  각 단계 {args.seconds}초\n")
    header = f"{'동시':>5}{'요청':>9}{'RPS':>9}{'p50':>9}{'p95':>9}{'p99':>9}{'오류':>7}  응답코드"
    print(header)
    print("-" * len(header))
    for c in levels:
        r = await run_level(args.url, c, args.seconds)
        codes = ",".join(f"{k}:{v}" for k, v in sorted(r["codes"].items()))
        print(f"{r['concurrency']:>5}{r['requests']:>9}{r['rps']:>9}"
              f"{r['p50']:>9}{r['p95']:>9}{r['p99']:>9}{r['errors']:>7}  {codes}")
        if r["errors"]:
            print(f"      오류 유형: {', '.join(r['error_kinds'])}")
        # 단계 사이에 숨을 돌린다. 앞 단계의 여운이 다음 단계 숫자에 섞이면
        # 어느 지점에서 무너졌는지가 흐려진다.
        await asyncio.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8090/api/v1/recommend")
    parser.add_argument("--levels", default="1,5,10,25,50,100")
    parser.add_argument("--seconds", type=float, default=8.0)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
