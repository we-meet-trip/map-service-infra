#!/usr/bin/env python3
"""채팅방의 한살이를 실제 소켓으로 확인한다.

보는 것: 대화가 양쪽에 닿는지 / 읽음 알림이 되먹임으로 돌지 않는지 /
방장이 나가도 방이 남고 넘어가는지 / 나간 사람도 기록을 볼 수 있는지.

방은 일정에만 매이므로 추천 흐름을 거치지 않고 일정을 직접 심는다. 검증용 데이터베이스를
대상으로만 쓴다 — 사용자 데이터가 든 스택에 겨누지 않는다.

사용: ./scripts/e2e_chat_lifecycle.py --base http://127.0.0.1:8081 --password <값>
"""
import argparse, asyncio, json, subprocess, sys, urllib.error, urllib.request, uuid

NUL = "\x00"
RESULTS = []

def record(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  {'통과' if ok else '실패'}  {name}{(' — ' + detail) if detail else ''}")

def http(method, url, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw)
        except json.JSONDecodeError: return e.code, {"raw": raw}

def frame(command, headers, body=""):
    head = "\n".join(f"{k}:{v}" for k, v in headers.items())
    return f"{command}\n{head}\n\n{body}{NUL}"

def parse(raw):
    text = (raw.decode() if isinstance(raw, bytes) else raw).rstrip(NUL)
    head, _, body = text.partition("\n\n")
    lines = head.split("\n")
    return lines[0], body

def sql(container, db, statement):
    out = subprocess.run(["docker", "exec", container, "psql", "-U", "verifyop", "-d", db,
                          "-tAc", statement], capture_output=True, text=True)
    if out.returncode != 0: raise SystemExit("sql 실패: " + out.stderr.strip())
    # RETURNING 은 값 뒤에 명령 태그가 한 줄 더 붙는다. 첫 줄만 쓴다.
    return out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""

async def open_socket(ws_url, token, room_id, label):
    import websockets
    sock = await websockets.connect(ws_url, open_timeout=15)
    await sock.send(frame("CONNECT", {"accept-version": "1.2", "host": "map",
                                      "heart-beat": "10000,10000",
                                      "Authorization": "Bearer " + token}))
    command, _ = parse(await asyncio.wait_for(sock.recv(), 15))
    if command != "CONNECTED": raise SystemExit(f"{label} 접속 거절: {command}")
    await sock.send(frame("SUBSCRIBE", {"id": label, "destination": f"/topic/rooms/{room_id}"}))
    return sock

async def collect(sock, seconds):
    """주어진 시간 동안 받은 프레임을 모두 모은다."""
    seen = []
    async def scan():
        while True:
            command, body = parse(await sock.recv())
            if command == "MESSAGE": seen.append(body)
    try:
        await asyncio.wait_for(scan(), seconds)
    except asyncio.TimeoutError:
        pass
    return seen

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--pg", default="map-verify-pg")
    ap.add_argument("--db", default="map_chat_verify")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/chat"

    def login(email):
        status, body = http("POST", f"{base}/api/v1/auth/login",
                            body={"email": email, "password": args.password})
        if status != 200: raise SystemExit(f"로그인 실패 {email}: {status} {body}")
        return body["accessToken"]

    token_a, token_b = login("maptester1@admin.map"), login("maptester2@admin.map")
    uid_a = sql(args.pg, args.db, "select id from user_service.users where email='maptester1@admin.map'")
    uid_b = sql(args.pg, args.db, "select id from user_service.users where email='maptester2@admin.map'")

    # 추천 흐름을 거치지 않고 일정만 직접 심는다. 방은 일정에만 매인다.
    schedule_id = sql(args.pg, args.db,
        "insert into user_service.schedules (user_id, job_id, title, date_start, date_end, payload, created_at, transport) "
        f"values ({uid_a}, gen_random_uuid(), '소켓 확인', current_date, current_date + 1, '{{}}'::jsonb, now(), 'walk') "
        "returning schedule_id")

    status, room = http("POST", f"{base}/api/v1/chat/rooms", token_a, {"schedule_id": int(schedule_id)})
    room_id = room.get("room_id")
    record("일정으로 방이 열린다", room_id is not None, f"room_id={room_id} status={status}")
    if room_id is None: raise SystemExit(json.dumps(room, ensure_ascii=False))

    _, invite = http("POST", f"{base}/api/v1/chat/rooms/{room_id}/invite", token_a)
    http("POST", f"{base}/api/v1/chat/invites/{invite['token']}/join", token_b)

    a = await open_socket(ws_url, token_a, room_id, "a")
    b = await open_socket(ws_url, token_b, room_id, "b")
    await asyncio.sleep(0.5)

    # 1) 대화가 상대에게 닿는가
    mark = "M-" + uuid.uuid4().hex[:8]
    http("POST", f"{base}/api/v1/chat/rooms/{room_id}/messages", token_a,
         {"content": f"안녕 {mark}", "client_msg_id": mark})
    got_b, got_a = await asyncio.gather(collect(b, 6), collect(a, 6))
    record("보낸 말이 상대 기기에 닿는다", any(mark in f for f in got_b))
    record("보낸 말이 자기 기기에도 돌아온다", any(mark in f for f in got_a))

    # 2) 읽음이 되먹임으로 돌지 않는가
    _, latest = http("GET", f"{base}/api/v1/chat/rooms/{room_id}/messages", token_b)
    top = max((m["seq"] for m in latest.get("messages", [])), default=0)
    http("POST", f"{base}/api/v1/chat/rooms/{room_id}/read", token_b, {"last_read_seq": top})
    first = await collect(a, 3)
    read_first = [f for f in first if '"READ"' in f]
    # 같은 자리를 다시 확인한다 — 바뀐 것이 없으므로 알림도 없어야 한다.
    http("POST", f"{base}/api/v1/chat/rooms/{room_id}/read", token_b, {"last_read_seq": top})
    again = await collect(a, 3)
    read_again = [f for f in again if '"READ"' in f]
    record("읽음이 앞으로 가면 한 번 알린다", len(read_first) == 1, f"{len(read_first)}건")
    record("같은 자리를 다시 확인하면 알리지 않는다", len(read_again) == 0, f"{len(read_again)}건")

    # 3) 방장이 나가도 방이 남는가
    status, _ = http("DELETE", f"{base}/api/v1/chat/rooms/{room_id}/participants/me", token_a)
    record("방장이 나간다", status == 204, f"status={status}")
    owner_now = sql(args.pg, args.db, f"select owner_id from user_service.chat_rooms where room_id={room_id}")
    read_only = sql(args.pg, args.db, f"select read_only from user_service.chat_rooms where room_id={room_id}")
    roles = sql(args.pg, args.db,
        f"select string_agg(user_id||':'||role||':'||status, ',' order by user_id) "
        f"from user_service.chat_participants where room_id={room_id}")
    record("방이 살아 있다", read_only == "f", f"read_only={read_only}")
    record("방장이 남은 사람에게 넘어간다", owner_now == uid_b, f"owner={owner_now} 기대={uid_b}")
    record("떠난 사람의 역할이 내려간다", f"{uid_a}:MEMBER:LEFT" in roles, roles)

    # 4) 남은 사람이 계속 말할 수 있는가
    mark2 = "N-" + uuid.uuid4().hex[:8]
    status, _ = http("POST", f"{base}/api/v1/chat/rooms/{room_id}/messages", token_b,
                     {"content": f"계속 {mark2}", "client_msg_id": mark2})
    record("남은 사람이 계속 말할 수 있다", status == 201, f"status={status}")

    # 5) 나간 사람도 기록을 볼 수 있는가
    status, history = http("GET", f"{base}/api/v1/chat/rooms/{room_id}/messages", token_a)
    record("나간 사람도 지난 기록을 볼 수 있다", status == 200 and len(history.get("messages", [])) > 0,
           f"status={status} 건수={len(history.get('messages', []))}")

    for sock in (a, b):
        await sock.close()

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\nRESULT {len(RESULTS) - len(failed)}/{len(RESULTS)} 통과")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
