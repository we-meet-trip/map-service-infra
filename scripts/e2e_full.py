#!/usr/bin/env python3
"""스택 전체가 요구대로 도는지 한 번에 확인한다.

각 항목은 "무엇이 잘못되면 무엇이 보이지 않는가"를 기준으로 골랐다. 화면만
봐서는 드러나지 않는 것들이다.

  기동      — 서비스가 떠 있는지가 아니라 실제 요청을 받는지
  마이그레이션 — 표가 실제로 생겼는지. hub 는 표가 없어도 정상이라고 답한다
  인증      — 토큰 없이 부르면 막히는지. 켰다고 믿는데 열려 있는 상태가 가장 나쁘다
  저장      — 저장소에 실제로 무엇이 들어갔는지. 응답만 보면 알 수 없다
  통신      — 서비스 사이에 무엇이 흐르는지. 응답이 같아도 흐르는 것은 다를 수 있다
  기록      — 로그에 좌표가 남는지. 남아도 아무 증상이 없다

사용:
    ./scripts/e2e_full.py --env test
    ./scripts/e2e_full.py --env prod --base http://127.0.0.1:8090
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

RESULTS: list[tuple[str, str, bool, str]] = []
SECTION = ""


def section(name: str) -> None:
    global SECTION
    SECTION = name
    print(f"\n[{name}]")


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((SECTION, name, ok, detail))
    print(f"  {'통과' if ok else '실패'}  {name}{(' — ' + detail) if detail else ''}")
    return ok


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def _body(raw: str) -> dict:
    """본문을 사전으로 접는다.

    JSON 이 아닌 응답도 있다(상태 확인은 ok 라는 글자만 준다). 그것을 오류로
    보면 서비스가 멀쩡한데도 연결 실패로 읽혀, 엉뚱한 곳을 뒤지게 된다.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def http(method: str, url: str, token: str | None = None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, _body(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, _body(e.read().decode())
    except Exception as e:  # noqa: BLE001 - 연결 자체가 안 되는 경우도 결과다
        return 0, {"error": type(e).__name__}


def env_value(path: str, key: str) -> str:
    try:
        for line in open(path, encoding="utf-8"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["test", "prod"], default="test")
    ap.add_argument("--base", default=None)
    args = ap.parse_args()

    is_test = args.env == "test"
    env_file = "./.env.test" if is_test else "./.env"
    base = args.base or ("http://127.0.0.1:8290" if is_test else "http://127.0.0.1:8090")
    prefix = "map-test" if is_test else "map-service"
    pg = f"{prefix}-postgres-1" if is_test else "map-service-postgres"
    redis = f"{prefix}-redis-1" if is_test else "map-service-redis"
    proxy = f"{prefix}-proxy-1" if is_test else "map-service-proxy"
    hub = f"{prefix}-hub-1" if is_test else "map-service-hub"
    user = f"{prefix}-user-1" if is_test else "map-service-user"

    db = env_value(env_file, "POSTGRES_DB") or "map"
    db_user = env_value(env_file, "POSTGRES_USER") or "map"
    db_pw = env_value(env_file, "POSTGRES_PASSWORD")
    password = env_value(env_file, "TESTER_SEED_PASSWORD") or "admin123!"
    auth_on = env_value(env_file, "AUTH_ENFORCED").lower() == "true"
    wire_on = bool(env_value(env_file, "LOCATION_WIRE_KEY"))
    store_on = env_value(env_file, "LOCATION_ENC_ENABLED").lower() == "true"

    def psql(sql: str) -> str:
        return sh("docker", "exec", "-e", f"PGPASSWORD={db_pw}", pg,
                  "psql", "-U", db_user, "-d", db, "-tAc", sql)

    print(f"환경={args.env}  관문={base}  저장소={db}")

    # ── 기동 ────────────────────────────────────────────────────────────
    section("기동")
    states = sh("docker", "ps", "--filter", f"name={prefix}-",
                "--format", "{{.Names}}:{{.State}}")
    running = [l for l in states.split("\n") if l.endswith(":running")]
    check("컨테이너가 모두 떠 있다", len(running) >= 6, f"{len(running)}개")

    status, _ = http("GET", f"{base}/healthz")
    check("관문이 요청을 받는다", status == 200, f"healthz={status}")

    # 위 /healthz 는 관문이 자답하므로 상류가 죽어도 200 을 준다. 바깥 감시가
    # 그것만 보면 앱이 멎은 것을 영영 모르므로, 상류까지 실제로 닿는 경로가
    # 살아 있는지 여기서 함께 본다. 본문까지 보는 이유는 관문이 502 를 감싸
    # 200 으로 돌려주도록 설정이 바뀌어도 걸리게 하기 위해서다.
    status, payload = http("GET", f"{base}/healthz/app")
    check("상류까지 닿는 상태 확인이 열려 있다",
          status == 200 and payload.get("status") == "UP",
          f"healthz/app={status} {payload.get('status')}")

    status, _ = http("GET", f"{base}/actuator/health")
    # 이 경로가 열려 있으면 설정과 내부 상태가 그대로 나간다.
    check("관리 경로는 바깥에서 막혀 있다", status == 404, f"actuator={status}")

    # ── 마이그레이션 ────────────────────────────────────────────────────
    section("마이그레이션")
    hub_tables = psql(
        "select count(*) from information_schema.tables "
        "where table_schema='hub_data'")
    # hub 는 표가 하나도 없어도 상태 확인에는 정상이라고 답한다.
    check("hub 표가 실제로 있다", hub_tables.isdigit() and int(hub_tables) > 0,
          f"{hub_tables}개")

    applied = psql("select max(version) from user_service.flyway_schema_history")
    source = sh("bash", "-c",
                "ls ../map-service-user/src/main/resources/db/migration/"
                " | sed -n 's/^V\\([0-9]*\\)__.*/\\1/p' | sort -n | tail -1")
    # 저장소가 코드보다 앞서 있으면 되돌린 배포에서 조용히 어긋난 채로 돈다.
    check("저장소 판이 코드와 같다", applied.strip() == source.strip(),
          f"저장소={applied} 코드={source}")

    # ── 인증 ────────────────────────────────────────────────────────────
    section("인증")
    status, body = http("POST", f"{base}/api/v1/auth/login",
                        body={"email": "maptester1@admin.map", "password": password})
    token = body.get("accessToken") if status == 200 else None
    check("시험 계정으로 로그인된다", bool(token), f"status={status}")
    if not token:
        return report()

    status, _ = http("GET", f"{base}/api/v1/schedules")
    if auth_on:
        check("토큰 없이는 막힌다", status == 401, f"status={status}")
    else:
        check("인증이 꺼져 있어 토큰 없이도 열린다", status == 200,
              f"status={status} (운영에서는 켠다)")

    status, _ = http("GET", f"{base}/api/v1/schedules", token)
    check("토큰이 있으면 열린다", status == 200, f"status={status}")

    # ── 저장 ────────────────────────────────────────────────────────────
    section("저장")
    job = str(uuid.uuid4())
    place = {"place_id": 1, "day": 1, "name": "광안리해수욕장",
             "address": "부산 수영구", "lat": 35.1532, "lng": 129.1187,
             "stay_minutes": 60}
    draft = {"job_id": job, "status": "done", "places": [place],
             "visit_order": [1], "legs": []}
    sh("docker", "exec", redis, "redis-cli", "-n", "4",
       "SET", f"recommend:result:{job}", json.dumps(draft, ensure_ascii=False),
       "EX", "3600")

    status, saved = http("POST", f"{base}/api/v1/schedules", token, {
        "job_id": job, "title": "검증 일정", "date_start": "2026-09-20",
        "date_end": "2026-09-20", "transport": "walk"})
    sid = saved.get("schedule_id")
    check("일정이 저장된다", status == 200 and sid is not None, f"status={status}")

    if sid:
        stored = psql(
            f"select payload::text from user_service.schedules where schedule_id={sid}")
        leaked = "35.1532" in stored or "광안리" in stored
        if store_on:
            check("저장된 본문에 평문 좌표가 없다", not leaked,
                  "봉투" if stored.startswith('{"v"') or '"ct"' in stored else "평문")
        else:
            check("저장 암호화가 꺼져 있다", leaked, "운영에서는 켠다")

        status, detail = http("GET", f"{base}/api/v1/schedules/{sid}", token)
        stops = detail.get("stops", []) if status == 200 else []
        got = [(s.get("latitude"), s.get("longitude")) for s in stops]
        check("저장한 좌표를 그대로 되읽는다",
              (35.1532, 129.1187) in got, f"stops={len(stops)}")

    # ── 통신 ────────────────────────────────────────────────────────────
    section("통신")
    status, _ = http("GET",
                     f"{base}/api/v1/mobility/bike-stations?lat=35.1587&lng=129.1604",
                     token)
    check("좌표를 쓰는 조회가 동작한다", status == 200, f"status={status}")

    hub_log = sh("docker", "logs", "--tail", "200", hub)
    if wire_on:
        opened = "location seal opened" in hub_log
        check("hub 가 감싼 좌표를 열어 처리한다", opened)
        # 기록이 또 하나의 위치 저장소가 되면 감싼 의미가 없다.
        leak = re.search(r"seal opened[^\n]*(35\.15|129\.1)", hub_log)
        check("여는 기록에 좌표가 없다", leak is None)
    else:
        check("통신 봉투가 꺼져 있다", True, "운영에서는 켠다")

    # agent 가 감싸서 넣은 결과를 BFF 가 여는지 본다.
    if wire_on:
        job2 = str(uuid.uuid4())
        payload = json.dumps({"job_id": job2, "status": "done",
                              "places": [place], "visit_order": [1], "legs": []},
                             ensure_ascii=False)
        # 본문을 인자에 그대로 실어 agent 자신의 코드로 감싼다.
        token2 = sh("docker", "exec", f"{prefix}-agent-1", "python", "-c",
                    f"from app.crypto import location_seal;"
                    f"print(location_seal.seal({{'payload': {payload!r}}}))")
        check("agent 가 만든 봉투에 좌표가 없다",
              bool(token2) and "35.1532" not in token2 and "광안리" not in token2)
        if token2:
            sh("docker", "exec", redis, "redis-cli", "-n", "2",
               "XADD", "agent:jobs:done", "*", "job_id", job2,
               "status", "done", "payload", token2)
            import time as _t
            _t.sleep(4)
            saved_draft = sh("docker", "exec", redis, "redis-cli", "-n", "4",
                             "GET", f"recommend:result:{job2}")
            check("BFF 가 그 봉투를 열어 저장한다", bool(saved_draft))
            if store_on:
                check("저장된 초안에도 평문 좌표가 없다",
                      "35.1532" not in saved_draft and "광안리" not in saved_draft)

    # ── 기록 ────────────────────────────────────────────────────────────
    section("기록")
    # 부팅 때 실패한 예약 작업이 있는지 본다. hub 는 이것들이 전부 죽어도
    # 상태 확인에는 정상이라고 답하므로, 로그를 보지 않으면 날씨와 코스가
    # 영영 채워지지 않는 것을 아무도 모른다.
    boot_fail = re.findall(r"startup task (\S+) failed: ([^\n]+)", hub_log)
    check("부팅 때 실패한 예약 작업이 없다", not boot_fail,
          "; ".join(f"{n}={m[:60]}" for n, m in boot_fail[:3]))

    # 어느 서비스든 예외 자취가 남았으면 그 자체가 신호다.
    for name, cid in (("hub", hub), ("BFF", user), ("agent", f"{prefix}-agent-1")):
        log = sh("docker", "logs", "--tail", "400", cid)
        check(f"{name} 로그에 예외 자취가 없다",
              "Traceback" not in log and "Exception in thread" not in log)

    proxy_log = sh("docker", "logs", "--tail", "300", proxy)
    check("관문 기록에 좌표 질의가 없다",
          not re.search(r"lat=\d|lng=\d", proxy_log))

    user_log = sh("docker", "logs", "--tail", "400", user)
    check("BFF 기록에 좌표가 없다",
          not re.search(r"35\.1532|129\.1187|광안리", user_log))

    return report()


def report() -> int:
    failed = [(s, n, d) for s, n, ok, d in RESULTS if not ok]
    print(f"\nRESULT {len(RESULTS) - len(failed)}/{len(RESULTS)} 통과")
    for s, n, d in failed:
        print(f"  실패 [{s}] {n} {d}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
