#!/usr/bin/env python3
"""두 사람이 서로 다른 연결로 같은 방에서 대화가 되는지 관문 너머로 확인한다.

기기 두 대를 쓰지 않고도 "다른 기기끼리 통신이 되는가"를 가를 수 있는 이유는,
서버가 보는 것이 기기가 아니라 연결이기 때문이다. 서로 다른 토큰으로 각자
연결을 맺고, 한쪽이 보낸 말이 다른 쪽 소켓으로 오는지를 보면 된다.

STOMP 라이브러리를 쓰지 않고 프레임을 직접 만든다. 라이브러리는 관문이
업그레이드를 잘못 넘기거나 인증 헤더를 흘리는 상황을 자기 재시도로 덮어
버려서, 정작 확인하려던 실패가 성공처럼 보인다.

사용: ./scripts/e2e_chat.py [--base http://127.0.0.1:8090]
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
    RESULTS.append((name, ok, detail))
    print(f"  {'통과' if ok else '실패'}  {name}{(' — ' + detail) if detail else ''}")
    return ok


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


def login(base, email, password):
    status, body = http("POST", f"{base}/api/v1/auth/login",
                        body={"email": email, "password": password})
    if status != 200 or "accessToken" not in body:
        raise SystemExit(f"로그인 실패 {email}: {status} {body}")
    return body["accessToken"]


def frame(command, headers, body=""):
    head = "\n".join(f"{k}:{v}" for k, v in headers.items())
    return f"{command}\n{head}\n\n{body}{NUL}"


def parse(raw):
    """수신 프레임을 (명령, 헤더, 본문) 으로 가른다."""
    text = raw.decode() if isinstance(raw, bytes) else raw
    text = text.rstrip(NUL)
    head, _, body = text.partition("\n\n")
    lines = head.split("\n")
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k] = v
    return lines[0], headers, body


async def connect(ws_url, token):
    """연결을 맺고 CONNECTED 프레임을 돌려준다."""
    sock = await websockets.connect(ws_url, open_timeout=15)
    await sock.send(frame("CONNECT", {
        "accept-version": "1.2", "host": "map",
        "heart-beat": "10000,10000", "Authorization": "Bearer " + token,
    }))
    command, headers, _ = parse(await asyncio.wait_for(sock.recv(), 15))
    if command != "CONNECTED":
        await sock.close()
        raise SystemExit(f"접속 거절: {command} {headers}")
    return sock, headers


async def wait_for(sock, marker, seconds):
    """방 방송에서 표식이 든 말이 올 때까지 기다린다."""
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
    # 예비값을 두지 않는다. 코드에 남은 비밀번호는 저장소에 남고,
    # 시험 계정이 켜진 스택이 고정 주소로 열리면 그것만으로 들어올 수 있다.
    ap.add_argument("--password", required=True)
    ap.add_argument("--redis", default="map-service-redis",
                    help="초안을 심을 redis 컨테이너 이름")
    ap.add_argument("--redis-db", default="4")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/chat"

    print(f"관문: {base}")
    token_a = login(base, "maptester1@admin.map", args.password)
    token_b = login(base, "maptester2@admin.map", args.password)
    record("서로 다른 두 사람이 각자 토큰을 받는다", token_a != token_b)

    # 방은 저장된 일정에 1:1 로 붙는다. 초안을 하나 만들어 일정으로 저장한다.
    job_id = str(uuid.uuid4())
    draft = {"job_id": job_id, "status": "done",
             "places": [{"place_id": 1, "day": 1, "name": "속초해수욕장",
                         "address": "강원 속초시", "lat": 38.1907, "lng": 128.5998,
                         "stay_minutes": 60}],
             "visit_order": [1], "legs": []}
    # 초안은 본래 agent 가 만들어 넣는다. 여기서는 모델 호출 없이 채팅만 보려고
    # 같은 자리에 직접 심는다. BFF 가 읽을 때 봉투가 아니면 그대로 쓰므로,
    # 평문으로 심어도 저장 단계에서 감싸진다.
    seeded = subprocess.run(
        ["docker", "exec", args.redis, "redis-cli", "-n", args.redis_db,
         "SET", f"recommend:result:{job_id}", json.dumps(draft, ensure_ascii=False),
         "EX", "3600"],
        capture_output=True, text=True)
    if seeded.returncode != 0:
        raise SystemExit(f"초안 심기 실패({args.redis}): {seeded.stderr.strip()}")

    status, body = http("POST", f"{base}/api/v1/schedules", token_a, {
        "job_id": job_id, "title": "채팅 검사", "date_start": "2026-09-10",
        "date_end": "2026-09-10", "transport": "walk"})
    if status != 200:
        raise SystemExit(f"일정 저장 실패: {status} {body}")
    schedule_id = body["schedule_id"]

    status, room = http("POST", f"{base}/api/v1/chat/rooms", token_a,
                        {"schedule_id": schedule_id})
    room_id = room.get("room_id") or room.get("roomId")
    record("일정으로 방이 만들어진다", room_id is not None, f"room_id={room_id}")

    status, invite = http("POST", f"{base}/api/v1/chat/rooms/{room_id}/invite", token_a)
    token_link = invite.get("token")
    status, _ = http("POST", f"{base}/api/v1/chat/invites/{token_link}/join", token_b)
    record("초대 링크로 두 번째 사람이 들어온다", status in (200, 201, 409))

    sock_a, headers_a = await connect(ws_url, token_a)
    sock_b, _ = await connect(ws_url, token_b)
    record("두 연결이 관문을 넘어 각각 맺어진다", True)

    beat = headers_a.get("heart-beat", "0,0")
    record("서버가 하트비트를 협상한다", beat not in ("0,0", "0, 0"),
           f"heart-beat={beat} (0,0 이면 유휴 연결이 조용히 끊긴다)")

    await sock_a.send(frame("SUBSCRIBE", {"id": "a", "destination": f"/topic/rooms/{room_id}"}))
    await sock_b.send(frame("SUBSCRIBE", {"id": "b", "destination": f"/topic/rooms/{room_id}"}))
    # 구독 등록이 비동기라 곧바로 보내면 첫 말을 놓친다.
    await asyncio.sleep(1.0)

    mark_a = "A-" + uuid.uuid4().hex[:8]
    await sock_a.send(frame("SEND", {"destination": f"/app/rooms/{room_id}/send"},
                            json.dumps({"content": f"안녕 {mark_a}",
                                        "client_msg_id": mark_a})))
    record("A 가 보낸 말이 B 에게 닿는다", await wait_for(sock_b, mark_a, 10))

    mark_b = "B-" + uuid.uuid4().hex[:8]
    await sock_b.send(frame("SEND", {"destination": f"/app/rooms/{room_id}/send"},
                            json.dumps({"content": f"반가워 {mark_b}",
                                        "client_msg_id": mark_b})))
    record("B 가 보낸 말이 A 에게 닿는다", await wait_for(sock_a, mark_b, 10))

    # 패턴 구독이 열려 있으면 참가하지 않은 방의 대화까지 함께 온다.
    sock_c, _ = await connect(ws_url, token_b)
    denied = []
    for dest in ["/topic/rooms/*", "/topic/{a}/{b}", f"/topic/{{x}}/{room_id}"]:
        await sock_c.send(frame("SUBSCRIBE", {"id": "c", "destination": dest}))
        try:
            command, _, _ = parse(await asyncio.wait_for(sock_c.recv(), 5))
            denied.append(command == "ERROR")
        except asyncio.TimeoutError:
            denied.append(False)
        if denied[-1]:
            sock_c, _ = await connect(ws_url, token_b)
    record("패턴 구독은 전부 거절된다", all(denied), f"{denied}")

    status, history = http("GET", f"{base}/api/v1/chat/rooms/{room_id}/messages", token_b)
    text = json.dumps(history, ensure_ascii=False)
    record("주고받은 말이 기록에 남는다", mark_a in text and mark_b in text)

    for sock in (sock_a, sock_b, sock_c):
        await sock.close()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT {len(RESULTS) - len(failed)}/{len(RESULTS)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
