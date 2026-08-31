#!/usr/bin/env python3
"""한 방에 여러 연결이 동시에 붙어도 말이 모두에게 가는지 확인한다.

기기가 여럿인 상황은 두 가지다.
  1) 서로 다른 사람이 각자 기기에서 붙는다.
  2) 같은 사람이 휴대폰과 태블릿처럼 기기 둘로 붙는다.

2번이 특히 조용히 깨진다. 서버가 사람 단위로 연결을 하나만 들고 있으면
나중에 붙은 기기가 앞의 것을 밀어내는데, 밀려난 기기는 그 사실을 모른 채
연결됐다고 믿고 있다가 아무 말도 받지 못한다.

또 하나 본다 — 보낸 사람 자신의 다른 기기에도 그 말이 가야 한다. 가지 않으면
휴대폰에서 보낸 말이 태블릿 화면에는 영영 안 보인다.

사용: ./scripts/e2e_chat_multidevice.py --base http://127.0.0.1:8290 --redis map-test-redis-1
"""
import argparse
import asyncio
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

import websockets

NUL = "\x00"
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  {'통과' if ok else '실패'}  {name}{(' — ' + detail) if detail else ''}")


def http(method, url, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def frame(command, headers, body=""):
    head = "\n".join(f"{k}:{v}" for k, v in headers.items())
    return f"{command}\n{head}\n\n{body}{NUL}"


def parse(raw):
    text = (raw.decode() if isinstance(raw, bytes) else raw).rstrip(NUL)
    head, _, body = text.partition("\n\n")
    lines = head.split("\n")
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k] = v
    return lines[0], headers, body


async def open_device(ws_url, token, room_id, label):
    """기기 하나가 붙어 방을 구독한 상태까지 만든다."""
    sock = await websockets.connect(ws_url, open_timeout=15)
    await sock.send(frame("CONNECT", {
        "accept-version": "1.2", "host": "map",
        "heart-beat": "10000,10000", "Authorization": "Bearer " + token,
    }))
    command, _, _ = parse(await asyncio.wait_for(sock.recv(), 15))
    if command != "CONNECTED":
        raise SystemExit(f"{label} 접속 거절: {command}")
    await sock.send(frame("SUBSCRIBE",
                          {"id": label, "destination": f"/topic/rooms/{room_id}"}))
    return sock


async def saw(sock, marker, seconds):
    async def scan():
        while True:
            command, _, body = parse(await sock.recv())
            if command == "MESSAGE" and marker in body:
                return True
    try:
        return await asyncio.wait_for(scan(), seconds)
    except asyncio.TimeoutError:
        return False


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--redis", default="map-service-redis")
    ap.add_argument("--redis-db", default="4")
    ap.add_argument("--password", default="admin123!")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/chat"

    def login(email):
        status, body = http("POST", f"{base}/api/v1/auth/login",
                            body={"email": email, "password": args.password})
        if status != 200:
            raise SystemExit(f"로그인 실패 {email}: {status} {body}")
        return body["accessToken"]

    token_a = login("maptester1@admin.map")
    token_b = login("maptester2@admin.map")

    # 방을 하나 만들고 두 번째 사람을 들인다.
    job_id = str(uuid.uuid4())
    draft = {"job_id": job_id, "status": "done",
             "places": [{"place_id": 1, "day": 1, "name": "속초해수욕장",
                         "address": "강원 속초시", "lat": 38.1907, "lng": 128.5998,
                         "stay_minutes": 60}],
             "visit_order": [1], "legs": []}
    seeded = subprocess.run(
        ["docker", "exec", args.redis, "redis-cli", "-n", args.redis_db,
         "SET", f"recommend:result:{job_id}", json.dumps(draft, ensure_ascii=False),
         "EX", "3600"], capture_output=True, text=True)
    if seeded.returncode != 0:
        raise SystemExit(f"초안 심기 실패: {seeded.stderr.strip()}")

    _, saved = http("POST", f"{base}/api/v1/schedules", token_a, {
        "job_id": job_id, "title": "다중 접속 검사", "date_start": "2026-09-10",
        "date_end": "2026-09-10", "transport": "walk"})
    _, room = http("POST", f"{base}/api/v1/chat/rooms", token_a,
                   {"schedule_id": saved["schedule_id"]})
    room_id = room["room_id"]
    _, invite = http("POST", f"{base}/api/v1/chat/rooms/{room_id}/invite", token_a)
    http("POST", f"{base}/api/v1/chat/invites/{invite['token']}/join", token_b)

    # A 는 기기 둘, B 는 기기 하나.
    a1 = await open_device(ws_url, token_a, room_id, "a1")
    a2 = await open_device(ws_url, token_a, room_id, "a2")
    b1 = await open_device(ws_url, token_b, room_id, "b1")
    record("한 방에 세 연결이 동시에 붙는다", True, f"room_id={room_id}")
    await asyncio.sleep(1.0)

    # A 의 첫 기기가 보낸다.
    mark = "M-" + uuid.uuid4().hex[:8]
    await a1.send(frame("SEND", {"destination": f"/app/rooms/{room_id}/send"},
                        json.dumps({"content": f"안녕 {mark}", "client_msg_id": mark})))
    got_b, got_a2, got_a1 = await asyncio.gather(
        saw(b1, mark, 10), saw(a2, mark, 10), saw(a1, mark, 10))
    record("다른 사람의 기기에 닿는다", got_b)
    record("같은 사람의 다른 기기에도 닿는다", got_a2)
    record("보낸 기기 자신에게도 되돌아온다", got_a1)

    # 나중에 붙은 기기가 앞의 것을 밀어내지 않는지 본다.
    mark2 = "N-" + uuid.uuid4().hex[:8]
    await b1.send(frame("SEND", {"destination": f"/app/rooms/{room_id}/send"},
                        json.dumps({"content": f"답장 {mark2}", "client_msg_id": mark2})))
    first_alive, second_alive = await asyncio.gather(
        saw(a1, mark2, 10), saw(a2, mark2, 10))
    record("먼저 붙은 기기가 밀려나지 않는다", first_alive)
    record("나중에 붙은 기기도 계속 받는다", second_alive)

    # 접속 표시가 사람 단위로 한 번만 세어지는지 본다.
    status, online = http("GET", f"{base}/api/v1/chat/rooms/{room_id}/presence", token_a)
    ids = online if isinstance(online, list) else online.get("data", [])
    record("기기가 둘이어도 접속자는 사람 수로 센다", len(set(ids)) == len(ids),
           f"접속자={ids}")

    # 한 기기만 끊었을 때 나머지가 계속 받는지 본다.
    await a1.close()
    await asyncio.sleep(1.0)
    mark3 = "O-" + uuid.uuid4().hex[:8]
    await b1.send(frame("SEND", {"destination": f"/app/rooms/{room_id}/send"},
                        json.dumps({"content": f"이어서 {mark3}", "client_msg_id": mark3})))
    record("한 기기를 꺼도 남은 기기는 계속 받는다", await saw(a2, mark3, 10))

    for sock in (a2, b1):
        await sock.close()

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\nRESULT {len(RESULTS) - len(failed)}/{len(RESULTS)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
