#!/usr/bin/env python3
"""채팅방을 두 사람으로 실제로 열어 본다.

왜 따로 떼어 놓았나:
    나머지 검사는 요청 한 번에 답 한 번이라 셸로 충분한데, 채팅은 한쪽이 보낸
    것이 다른 쪽에 닿는지를 봐야 한다. 보낸 사람에게 돌아온 응답만으로는
    아무것도 확인되지 않는다 — 방송이 끊겨 있어도 보낸 쪽은 성공으로 보인다.

왜 소켓을 직접 다루나:
    앱은 STOMP 라이브러리를 쓰지만, 검사가 그 라이브러리를 쓰면 라이브러리가
    맞춰 주는 부분까지 함께 가려진다. 관문이 Upgrade 를 넘기는지, 서버가
    토큰을 CONNECT 에서 보는지, 방 토픽 인가가 걸리는지를 그대로 보려면
    프레임을 직접 쓰는 편이 낫다.

두 사람인 이유:
    참가자 한 명으로는 방송 경로가 검증되지 않는다. A 가 보낸 것을 B 가
    받아야 브로커와 인가가 함께 통과한 것이다.

사용:
    python3 scripts/e2e_chat.py --base http://127.0.0.1:8090 --schedule-id 12
"""
import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
import uuid

import websockets

PASS = 0
FAIL = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  \033[32m✓\033[0m {msg}")


def no(msg: str, why: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  \033[31m✗\033[0m {msg} — {why}")


def http(base, method, path, token=None, body=None):
    """REST 한 번. (상태코드, 파싱된 본문) 을 준다.

    본문이 JSON 이 아니거나 비어 있으면 None 을 준다 — 실패 경로에서 본문
    형식까지 맞으리라고 기대하면 검사가 엉뚱한 곳에서 죽는다.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, None
    except Exception as e:  # 연결 자체가 안 될 때
        return 0, {"error": str(e)}


def login(base, email, password):
    st, body = http(base, "POST", "/api/v1/auth/login",
                    body={"email": email, "password": password})
    if st != 200 or not body:
        return None
    return body.get("access_token") or body.get("accessToken")


# ---- STOMP: 프레임을 직접 짓고 읽는다 -------------------------------------

def frame(command: str, headers: dict, body: str = "") -> str:
    head = "".join(f"{k}:{v}\n" for k, v in headers.items())
    return f"{command}\n{head}\n{body}\0"


def parse(raw: str):
    """STOMP 프레임 하나를 (명령, 헤더, 본문) 으로 나눈다."""
    raw = raw.rstrip("\0")
    head, _, body = raw.partition("\n\n")
    lines = head.split("\n")
    command = lines[0]
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers[k] = v
    return command, headers, body


class Stomp:
    """한 사람의 소켓. 받은 프레임은 큐에 쌓아 두고 필요할 때 꺼낸다.

    쌓아 두는 이유: 구독 직후 서버가 먼저 보내는 것(입장 알림 등)이 있어,
    보내고 바로 읽으면 내가 기다리는 것이 아닌 프레임을 집게 된다.
    """

    def __init__(self, ws):
        self.ws = ws
        self.queue: list[tuple] = []
        self._buf = ""

    async def _pump(self, timeout: float) -> bool:
        try:
            msg = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        self._buf += msg if isinstance(msg, str) else msg.decode()
        # 한 WS 메시지에 프레임이 여러 개 실려 올 수 있다.
        while "\0" in self._buf:
            one, _, self._buf = self._buf.partition("\0")
            one = one.lstrip("\n")  # 하트비트로 오는 개행
            if one:
                self.queue.append(parse(one))
        return True

    async def expect(self, command: str, timeout: float = 10.0, match=None):
        """원하는 프레임이 올 때까지 기다린다. 못 받으면 None."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for i, f in enumerate(self.queue):
                if f[0] == command and (match is None or match(f)):
                    return self.queue.pop(i)
            left = deadline - asyncio.get_event_loop().time()
            if left <= 0:
                return None
            await self._pump(left)

    async def send(self, command, headers, body=""):
        await self.ws.send(frame(command, headers, body))


async def connect(url, token) -> Stomp | None:
    ws = await websockets.connect(url, open_timeout=10)
    s = Stomp(ws)
    await s.send("CONNECT", {
        "accept-version": "1.2",
        "host": "localhost",
        # 하트비트를 끈다. 검사는 짧게 끝나고, 켜 두면 읽기에 빈 개행이 섞인다.
        "heart-beat": "0,0",
        "Authorization": f"Bearer {token}",
    })
    if await s.expect("CONNECTED", timeout=10) is None:
        await ws.close()
        return None
    return s


async def peer(base, ws_url, args) -> int:
    """기기 시험의 상대역. 방에 붙어 표식을 되풀이해 보내고 기기 것을 기다린다.

    기기를 두 대 함께 돌릴 수 있으면 그렇게 하는 편이 낫지만, 두 시험 도구를
    한 기계에서 동시에 띄우면 도구와 기기를 잇는 연결이 서로 방해한다.
    그때는 한쪽을 여기로 대신한다 — 기기 입장에서는 남이 보낸 것을 방송으로
    받는 것이라, 확인하려던 것은 그대로 확인된다.

    되풀이해 보내는 이유: 기기는 빌드부터 하고 오므로 언제 붙을지 모른다.
    한 번만 보내고 말면 기기가 붙기 전에 지나가 버린다.
    """
    token = login(base, args.email_b, args.password)
    if not token:
        no("상대역이 로그인된다", "실패")
        return 1
    s = await connect(ws_url, token)
    if not s:
        no("상대역이 붙는다", "CONNECTED 없음")
        return 1
    ok("상대역이 방에 붙었다")
    await s.send("SUBSCRIBE",
                 {"id": "p0", "destination": f"/topic/rooms/{args.schedule_id}"})

    seen = False
    for i in range(args.peer_seconds // 5):
        await s.send("SEND",
                     {"destination": f"/app/rooms/{args.schedule_id}/send",
                      "content-type": "application/json"},
                     json.dumps({"content": f"상대역 {args.peer_mark} #{i}",
                                 "client_msg_id": f"peer-{args.peer_mark}-{i}"}))
        got = await s.expect("MESSAGE", timeout=5,
                             match=lambda f: args.expect_mark in f[2])
        if got and not seen:
            seen = True
            ok(f"기기가 보낸 것({args.expect_mark})을 상대역이 받았다")
    if not seen:
        no("기기가 보낸 것을 상대역이 받았다", f"{args.peer_seconds}초 동안 못 받음")
    await s.ws.close()
    print(f"RESULT pass={PASS} fail={FAIL}")
    return 1 if FAIL else 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--schedule-id", type=int, required=True)
    ap.add_argument("--email-a", default="maptester1@admin.map")
    ap.add_argument("--email-b", default="maptester2@admin.map")
    ap.add_argument("--password", default="admin123!")
    # 상대역 모드에서는 --schedule-id 자리에 방 번호를 그대로 준다.
    ap.add_argument("--peer", action="store_true",
                    help="기기 시험의 상대역으로 돈다(방은 이미 있어야 한다)")
    ap.add_argument("--peer-mark", default="py-mark")
    ap.add_argument("--expect-mark", default="")
    ap.add_argument("--peer-seconds", type=int, default=240)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/chat"

    if args.peer:
        return await peer(base, ws_url, args)

    ta = login(base, args.email_a, args.password)
    tb = login(base, args.email_b, args.password)
    if not ta or not tb:
        no("두 사람이 로그인된다", f"A={bool(ta)} B={bool(tb)}")
        return 1
    ok("두 사람이 로그인된다")

    # 방 — 일정 하나에 방 하나다. 두 번 불러도 같은 방이 나와야 한다.
    st, room = http(base, "POST", "/api/v1/chat/rooms", ta,
                    {"schedule_id": args.schedule_id})
    if st not in (200, 201) or not room:
        no("일정으로 방이 열린다", f"{st} {room}")
        return 1
    room_id = room.get("room_id") or room.get("roomId")
    ok(f"일정으로 방이 열린다 ({st}, room_id={room_id})")

    st2, again = http(base, "POST", "/api/v1/chat/rooms", ta,
                      {"schedule_id": args.schedule_id})
    same = again and (again.get("room_id") or again.get("roomId")) == room_id
    if st2 == 200 and same:
        ok("같은 일정은 같은 방으로 온다 (200)")
    else:
        no("같은 일정은 같은 방으로 온다", f"{st2}, 같은방={same}")

    # B 를 들인다. 초대 토큰을 받아 참가시키는 것이 앱이 하는 방식이다.
    st, inv = http(base, "POST", f"/api/v1/chat/rooms/{room_id}/invite", ta)
    token = (inv or {}).get("token") or (inv or {}).get("invite_token")
    if st in (200, 201) and token:
        ok("초대 링크가 만들어진다")
    else:
        no("초대 링크가 만들어진다", f"{st} {inv}")
        return 1

    st, _ = http(base, "POST", f"/api/v1/chat/invites/{token}/join", tb)
    if st in (200, 201):
        ok("초대로 다른 사람이 들어온다")
    else:
        no("초대로 다른 사람이 들어온다", f"{st}")
        return 1

    st, parts = http(base, "GET", f"/api/v1/chat/rooms/{room_id}/participants", ta)
    n = len(parts if isinstance(parts, list) else (parts or {}).get("participants", []))
    if st == 200 and n >= 2:
        ok(f"참가자가 두 명이다 ({n}명)")
    else:
        no("참가자가 두 명이다", f"{st}, {n}명")

    # 소켓 — 여기부터가 이 검사의 목적이다.
    sa = await connect(ws_url, ta)
    sb = await connect(ws_url, tb)
    if not sa or not sb:
        no("관문을 지나 소켓이 열린다", f"A={bool(sa)} B={bool(sb)}")
        return 1
    ok("관문을 지나 소켓이 열린다 (CONNECTED)")

    await sa.send("SUBSCRIBE", {"id": "a0", "destination": f"/topic/rooms/{room_id}"})
    await sb.send("SUBSCRIBE", {"id": "b0", "destination": f"/topic/rooms/{room_id}"})
    # 구독이 브로커에 등록되기 전에 보내면 그 한 건을 놓친다.
    await asyncio.sleep(1.0)

    mark = uuid.uuid4().hex[:8]
    await sa.send("SEND",
                  {"destination": f"/app/rooms/{room_id}/send",
                   "content-type": "application/json"},
                  json.dumps({"content": f"A→B {mark}", "client_msg_id": f"a-{mark}"}))
    got = await sb.expect("MESSAGE", timeout=10,
                          match=lambda f: mark in f[2])
    if got:
        ok("A 가 보낸 것이 B 에게 닿는다")
    else:
        no("A 가 보낸 것이 B 에게 닿는다", "10초 안에 못 받음")

    mark2 = uuid.uuid4().hex[:8]
    await sb.send("SEND",
                  {"destination": f"/app/rooms/{room_id}/send",
                   "content-type": "application/json"},
                  json.dumps({"content": f"B→A {mark2}", "client_msg_id": f"b-{mark2}"}))
    got2 = await sa.expect("MESSAGE", timeout=10, match=lambda f: mark2 in f[2])
    if got2:
        ok("B 가 보낸 것이 A 에게 닿는다")
    else:
        no("B 가 보낸 것이 A 에게 닿는다", "10초 안에 못 받음")

    # 남의 방을 엿볼 수 없어야 한다. 와일드카드 구독은 전면 거절이 규약이다.
    sc = await connect(ws_url, tb)
    if sc:
        await sc.send("SUBSCRIBE", {"id": "c0", "destination": "/topic/rooms/*"})
        err = await sc.expect("ERROR", timeout=5)
        if err:
            ok("와일드카드 구독은 거절된다")
        else:
            no("와일드카드 구독은 거절된다", "ERROR 프레임 없음")
        await sc.ws.close()

    # 기록 — 소켓으로 보낸 것이 실제로 남아야 한다.
    st, msgs = http(base, "GET", f"/api/v1/chat/rooms/{room_id}/messages", tb)
    items = msgs if isinstance(msgs, list) else (msgs or {}).get("messages", [])
    text = json.dumps(items, ensure_ascii=False)
    if st == 200 and mark in text and mark2 in text:
        ok(f"주고받은 것이 기록에 남는다 ({len(items)}건)")
    else:
        no("주고받은 것이 기록에 남는다", f"{st}, {len(items)}건")

    # 안 읽은 수 — 읽음을 새기면 0 이 되어야 한다.
    seqs = [m.get("seq") for m in items if isinstance(m, dict) and m.get("seq")]
    last = max(seqs) if seqs else 1
    http(base, "POST", f"/api/v1/chat/rooms/{room_id}/read", tb,
         {"last_read_seq": last})
    st, unread = http(base, "GET", f"/api/v1/chat/rooms/{room_id}/unread", tb)
    cnt = (unread or {}).get("unread_count", (unread or {}).get("unreadCount"))
    if st == 200 and cnt == 0:
        ok("읽음을 새기면 안 읽은 수가 0 이 된다")
    else:
        no("읽음을 새기면 안 읽은 수가 0 이 된다", f"{st}, unread={cnt}")

    st, rooms = http(base, "GET", "/api/v1/chat/rooms", tb)
    ids = [r.get("room_id") or r.get("roomId")
           for r in (rooms if isinstance(rooms, list) else [])]
    if st == 200 and room_id in ids:
        ok("들어온 사람의 방 목록에 보인다")
    else:
        no("들어온 사람의 방 목록에 보인다", f"{st}, {ids}")

    await sa.ws.close()
    await sb.ws.close()

    print(f"RESULT pass={PASS} fail={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
