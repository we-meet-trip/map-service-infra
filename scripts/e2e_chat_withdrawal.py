#!/usr/bin/env python3
"""방장이 탈퇴해도 남은 사람의 대화가 남는지 본다.

탈퇴는 소유 방을 owner_id 로 찾아 일정 연결을 끊는다. 방장이 나가면서 방장이 넘어가면
그 조건은 한 행도 잡지 못하고, 연결이 남은 채 일정이 지워지면 외래키 연쇄가 방과 대화를
함께 지운다. 검증용 데이터베이스를 대상으로만 쓴다 — 계정을 실제로 지우기 때문이다.

사용: ./scripts/e2e_chat_withdrawal.py --base <주소> --password <값> \
        --owner maptester3@admin.map --member maptester4@admin.map
"""
import argparse, json, subprocess, sys, urllib.error, urllib.request, uuid

def http(method, url, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw)
        except json.JSONDecodeError: return e.code, {"raw": raw}

def sql(container, db, statement):
    out = subprocess.run(["docker","exec",container,"psql","-U","verifyop","-d",db,"-tAc",statement],
                         capture_output=True, text=True)
    if out.returncode != 0: raise SystemExit("sql 실패: " + out.stderr.strip())
    return out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""

ap = argparse.ArgumentParser()
ap.add_argument("--base", required=True); ap.add_argument("--password", required=True)
ap.add_argument("--pg", default="map-verify-pg"); ap.add_argument("--db", default="map_chat_verify")
ap.add_argument("--owner", required=True); ap.add_argument("--member", required=True)
a = ap.parse_args(); base = a.base.rstrip("/")
R = []
def record(n, ok, d=""):
    R.append(ok); print(f"  {'통과' if ok else '실패'}  {n}{(' — '+d) if d else ''}")

def login(email):
    s, b = http("POST", f"{base}/api/v1/auth/login", body={"email": email, "password": a.password})
    if s != 200: raise SystemExit(f"로그인 실패 {email}: {s} {b}")
    return b["accessToken"]

t_owner, t_member = login(a.owner), login(a.member)
uid_o = sql(a.pg, a.db, f"select id from user_service.users where email='{a.owner}'")
uid_m = sql(a.pg, a.db, f"select id from user_service.users where email='{a.member}'")

sched = sql(a.pg, a.db,
    "insert into user_service.schedules (user_id, job_id, title, date_start, date_end, payload, created_at, transport) "
    f"values ({uid_o}, gen_random_uuid(), '탈퇴 확인', current_date, current_date + 1, '{{}}'::jsonb, now(), 'walk') "
    "returning schedule_id")
s, room = http("POST", f"{base}/api/v1/chat/rooms", t_owner, {"schedule_id": int(sched)})
room_id = room.get("room_id"); record("방이 열린다", room_id is not None, f"room_id={room_id}")
_, inv = http("POST", f"{base}/api/v1/chat/rooms/{room_id}/invite", t_owner)
http("POST", f"{base}/api/v1/chat/invites/{inv['token']}/join", t_member)

mark = "K-" + uuid.uuid4().hex[:8]
http("POST", f"{base}/api/v1/chat/rooms/{room_id}/messages", t_member,
     {"content": f"남는 말 {mark}", "client_msg_id": mark})

s, _ = http("DELETE", f"{base}/api/v1/users/me", t_owner)
record("방장이 탈퇴한다", s in (200, 204), f"status={s}")

alive = sql(a.pg, a.db, f"select count(*) from user_service.chat_rooms where room_id={room_id}")
record("방이 지워지지 않는다", alive == "1", f"rooms={alive}")
kept = sql(a.pg, a.db,
    f"select count(*) from user_service.chat_messages where room_id={room_id} and content like '%{mark}%'")
record("남은 사람의 말이 살아 있다", kept == "1", f"messages={kept}")
sched_link = sql(a.pg, a.db, f"select coalesce(schedule_id::text,'NULL') from user_service.chat_rooms where room_id={room_id}")
record("일정 연결이 끊겨 있다", sched_link == "NULL", f"schedule_id={sched_link}")
owner_now = sql(a.pg, a.db, f"select coalesce(owner_id::text,'NULL') from user_service.chat_rooms where room_id={room_id}")
record("방장이 남은 사람에게 있다", owner_now == uid_m, f"owner={owner_now} 기대={uid_m}")

s, hist = http("GET", f"{base}/api/v1/chat/rooms/{room_id}/messages", t_member)
record("남은 사람이 기록을 읽는다", s == 200 and any(mark in (m.get("content") or "") for m in hist.get("messages", [])),
       f"status={s}")

print(f"\nRESULT {sum(R)}/{len(R)} 통과")
sys.exit(0 if all(R) else 1)
