#!/usr/bin/env python3
"""스택의 로그와 지표를 빠짐없이 훑는다.

세 검증 스크립트가 요청·저장·통신을 본다면 이것은 **부작용과 침묵**을 본다.
겹치는 것이 없어 따로 둔다.

왜 grep 으로는 안 되는가. 여섯 가지가 동시에 깨진다.

  1) 정상 동작이 오류로 남는다. 관문이 요청 상한을 걸면 [error] 로 적힌다 —
     검증이 성공할수록 개수가 올라간다.
  2) 진짜 실패가 그 단어를 안 쓴다. 저장소의 FATAL 에는 error 도 exception 도
     없고, "키가 없어 스텁으로 동작" 은 INFO 로 남는다.
  3) 개수만 세면 종류를 못 본다. 같은 줄 300번과 다른 줄 3종은 다른 사건이다.
  4) 창이 신호를 밀어낸다. 상태 확인이 몇 초마다 줄을 남기는 서비스에서는
     끝 200줄이 그것으로 가득 차 부팅 기록이 사라진다.
  5) 두 스트림을 섞는다. 관문은 접근 기록과 오류를 다른 곳으로 낸다.
  6) 못 읽은 것과 없는 것을 구분하지 않는다. 이름이 틀리면 빈 문자열이 오고,
     "그 단어가 없다" 는 참이 된다.

그래서 이렇게 한다. 이름이 아니라 딱지로 대상을 고르고, 이번에 뜬 뒤로 남긴
것을 전부 읽고, 스트림을 나누고, 부팅과 운전을 가르고, **오류를 찾기 전에
일을 했는지부터 보고**, 어느 규칙에도 안 걸린 줄은 전부 보여 준다.

소음은 지우지 않고 접는다. 지우면 그 줄이 사라진 날도 아무 일 없어 보인다.
접으면 개수가 남아, 0 이 되거나 갑자기 늘어난 것이 그대로 신호가 된다.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

RESULTS: list[tuple[str, bool, str]] = []
ENV = "test"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'통과' if ok else '실패'}  {name}" + (f" — {detail}" if detail else ""))


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


# 서비스마다 부팅이 끝났음을 알리는 표식. 이 줄 앞이 부팅, 뒤가 운전이다.
# 표식이 아예 없으면 기동이 끝나지 않은 것이라 그 자체가 실패다.
BOOT_END = {
    "postgres": "database system is ready to accept connections",
    "redis": "Ready to accept connections tcp",
    "hub": "Application startup complete.",
    "agent": "Application startup complete.",
    "user": "Started ServiceUserApplication in",
    "proxy": "start worker process",
    "osrm-foot": "running and waiting for requests",
    "osrm-bicycle": "running and waiting for requests",
}

# 부팅 구간에 반드시 있어야 하는 줄. 없으면 그 일을 안 한 것이다.
# 오류를 찾기 전에 이것부터 본다 — 아무 일도 안 했으면 오류도 없다.
MUST_HAVE = {
    "postgres": ["database system is ready to accept connections"],
    "redis": ["Ready to accept connections tcp"],
    "hub": ["Scheduler started", "Application startup complete."],
    "agent": ["Application startup complete."],
    "user": ["Started ServiceUserApplication in", "HikariPool-1 - Start completed."],
    "proxy": ["start worker process"],
    # 뿌리 주소는 그래프를 안 읽고도 답하므로 "떴다" 만으로는 부족하다.
    # 길을 낼 준비가 끝났다는 줄까지 있어야 그래프를 실제로 연 것이다.
    "osrm-foot": ["Listening on:", "running and waiting for requests"],
    "osrm-bicycle": ["Listening on:", "running and waiting for requests"],
}

# 경로 엔진 두 대가 공유하는 규칙. 프로파일 이름만 다르고 형식은 같다.
OSRM_FOLD = [
    # 조회 기록. 4xx 는 좌표가 길에 붙지 않은 경우라 정상 범위이고,
    # 5xx 는 아래 숫자·치명 검사가 따로 센다.
    (r"\d+(\.\d+)?ms .* (2\d\d|3\d\d|40[0-4]) /(nearest|route|table|match)/",
     "any", "경로 조회 기록"),
    (r"starting up engines|Threads:|IP (address|port):|Keepalive timeout"
     r"|Maximum header size|HTTP/1\.1 server using|Listening on:"
     r"|running and waiting for requests",
     "boot", "기동 안내"),
]

# 접어 둘 줄. (정규식, 어느 구간, 왜 접는가) 세 칸을 모두 채운다.
# 문구만으로 등록하지 않는다 — 같은 문구가 다른 구간에서 나오면 뜻이 달라진다.
FOLD = {
    "postgres": [
        (r"checkpoint (starting|complete)", "run", "쓰기를 모아 내보내는 정상 동작"),
        (r"Skipping initialization", "boot", "자리가 이미 차 있다 — 두 번째 배포의 정상 모습"),
        (r"starting PostgreSQL|autovacuum launcher|database system is ready",
         "boot", "기동 안내"),
        (r"^\s*$|^\s*20\d\d-\d\d-\d\d .*LOG:  (redo|restartpoint)", "boot", "기동 안내"),
        (r'enabling "trust" authentication', "boot", "첫 초기화 중에만 나온다"),
        (r"logical replication launcher.*exited with exit code 1", "boot", "종료 절차의 일부"),
        (r"received fast shutdown request|shutting down|shut down", "boot", "초기화 뒤 재기동 절차"),
        (r"listening on (IPv4|IPv6|Unix)", "boot", "기동 안내"),
        (r"database system was (shut down|interrupted)", "boot", "이전 종료 상태 보고"),
    ],
    "redis": [
        (r"Background saving|Fork CoW|DB (loaded|saved)", "run", "저장 절차"),
        (r"monotonic clock|Server initialized|Reading the configuration|Increased maximum",
         "boot", "기동 안내"),
        (r"changes in \d+ seconds\. Saving|Saving the final RDB|Redis is now ready|User requested shutdown|ready to exit",
         "any", "저장·종료 절차"),
        (r"^\s*\d+:[CM] |^\s*_+|^\s*\|", "boot", "기동 배너"),
        (r"oO0OoO0OoO0Oo|Redis version|Running mode|Configuration loaded", "boot", "기동 배너"),
    ],
    "hub": [
        (r"GET /health(/ready)? .*200", "run", "상태 확인. 개수가 0 이 되면 그 자체가 신호다"),
        (r"GET /health/ready .*503|cache ping failed", "any",
         "저장소가 끊긴 동안의 준비 확인. 되살아났는지는 위에서 짝으로 본다"),
        (r'INFO:\s+[\d.]+:\d+ - "(GET|POST|PUT|DELETE|PATCH) [^"]*" (2\d\d|3\d\d|40[0-4])',
         "any", "접근 기록. 5xx 는 위에서 실패로 센다"),
        (r"polling skipped — 키가 없어", "any", "시험은 발급처를 안 부른다. 운영이면 위에서 실패로 센다"),
        (r"sync skipped|Started server process|Waiting for application|Uvicorn running",
         "any", "기동·건너뜀 안내"),
        (r"Run time of job .* was missed by", "run", "이 기계가 잠들었을 때. 서버면 위에서 실패로 센다"),
        (r"INFO:\s+Application startup|INFO:\s+Started|Uvicorn running|Shutting down|Waiting for application shutdown",
         "any", "기동·종료 안내"),
        (r"location seal opened path=", "any", "좌표 봉투가 실제로 열리고 있다는 자취"),
        (r"apscheduler\.executors.*(Running job|Job .* executed)", "any", "예약이 도는 자취"),
        (r"places_sync|weather_sync|nowcast|air_quality|short_term|mid_term", "any", "수집 절차"),
        (r"housekeeping deleted=|forecast_repo|cache (hit|miss|purge)", "any", "정리·캐시 절차"),
        (r"Scheduler has been shut down|Removed job", "any", "종료 절차"),
        (r"Adding job tentatively", "boot", "예약 등록 절차"),
        (r"Added job |Scheduler started|Application startup complete", "boot", "기동 안내"),
    ],
    "yolo": [
        (r"GET /health .*200", "run", "상태 확인. 개수가 0 이 되면 그 자체가 신호다"),
        (r'INFO:\s+[\d.]+:\d+ - "(GET|POST) [^"]*" (2\d\d|3\d\d|40[0-4])',
         "any", "접근 기록. 5xx 는 위에서 실패로 센다"),
        (r"YoloDetector loaded model=|yolo-vision-agent: initialized", "boot", "모델 적재 안내"),
        (r"Ultralytics Settings|yolo settings|View Ultralytics", "boot", "모델 꾸러미가 처음 자리를 잡는 안내"),
        (r'WebSocket /ws/vision" \[accepted\]|connection (open|closed)', "any", "카메라 연결 오갔다"),
        (r"vision_ws: client (connected|disconnected)", "any", "카메라 연결 오갔다"),
        (r"Application startup complete|Started server process|Waiting for application|Uvicorn running",
         "boot", "기동 안내"),
    ],
    "agent": [
        (r"GET /health(/ready)? .*200", "run", "상태 확인. 개수가 0 이 되면 그 자체가 신호다"),
        (r"GET /health/ready .*503|streams ping failed", "any",
         "저장소가 끊긴 동안의 준비 확인. 되살아났는지는 위에서 짝으로 본다"),
        (r'INFO:\s+[\d.]+:\d+ - "(GET|POST|PUT|DELETE|PATCH) [^"]*" (2\d\d|3\d\d|40[0-4])',
         "any", "접근 기록. 5xx 는 위에서 실패로 센다"),
        (r"Application startup complete|Started server process|Waiting for application", "boot", "기동 안내"),
        (r"Uvicorn running|INFO:\s+Started|Shutting down|Waiting for application shutdown|lifespan disposed",
         "any", "기동·종료 안내"),
        (r"langgraph checkpointer ready|lifespan initialized", "boot", "기동 안내"),
    ],
    "user": [
        (r"^java\.net\.UnknownHostException: redis\b", "any",
         "저장소 이름이 잠시 풀리지 않았다. 되살아났는지는 아래에서 짝으로 본다"),
        (r"^Caused by: io\.lettuce\.core\.RedisCommandTimeoutException", "any",
         "위 예외의 뿌리. 저장소가 끊긴 동안의 증상"),
        (r"QueryTimeoutException: Redis command timed out", "any",
         "저장소가 끊긴 동안의 증상. 되살아났는지는 아래에서 짝으로 본다"),
        # 자취 줄만 접는다. 머리 줄은 따로 판정되므로, 모르는 예외는 그대로 드러난다.
        (r"^\s+at [\w.$/]+\(|^\s+\.\.\. \d+ (more|common frames omitted)", "any",
         "위 예외에 딸린 자취"),
        (r"Cannot reconnect to|Reconnected to", "any",
         "저장소가 끊긴 동안의 재접속 시도. 되살아났는지는 아래에서 짝으로 본다"),
        (r"stream 구독이 비활성 상태여서 재구독함", "any",
         "지켜보던 쪽이 끊긴 구독을 스스로 되살린 자취"),
        (r"Redis health check failed|reclaim pending query failed", "any",
         "저장소가 끊긴 동안의 증상. 되살아났는지는 아래에서 짝으로 본다"),
        (r"spring\.jpa\.open-in-view is enabled", "boot", "설정 안내"),
        # 기동 배너. 앞머리에 날짜가 없고 콜론도 없는 줄은 이 배너뿐이다 —
        # 스프링이 남기는 진짜 기록은 전부 날짜로 시작하고, 그 밖의 오류도
        # 대개 콜론을 담는다.
        (r"^(?!\d{4}-)(?!Picked up)[^:]*$|:: Spring Boot ::", "boot", "기동 배너"),
        (r"Picked up JAVA_TOOL_OPTIONS|Starting ServiceUserApplication|No active profile|The following \d+ profile",
         "boot", "기동 안내"),
        (r"Bootstrapping Spring Data|Finished Spring Data|Repository scanning|HHH\d+|Hibernate",
         "boot", "기동 안내"),
        (r"Commencing graceful shutdown|Graceful shutdown complete|Shutdown initiated|Shutdown completed|Destroying",
         "any", "종료 절차"),
        # 수준으로 가른다. 정보는 접고 경고·오류는 남긴다 — 로거 이름을
        # 늘어놓으면 새 이름이 생길 때마다 조용히 통과하게 된다.
        (r"\s+INFO \d+ --- ", "any", "정보 기록. 경고와 오류는 접지 않는다"),
        # 앞줄에 딸린 여러 줄 설명. 그 앞줄의 수준을 따른다.
        (r"^\t", "any", "앞줄에 딸린 설명"),
        (r"rateLimitFilterRegistration was not registered", "boot",
         "앱 상한이 꺼져 있다. 운영이면 위에서 실패로 센다"),
        (r"stream group already exists|stream placeholder discarded|재구독",
         "any", "스트림 준비 절차"),
        (r"closed abnormally \(0\)|WebSocketMessageBrokerStats", "run", "소켓 지표. 값은 위에서 본다"),
        (r"Using generated security password|UserDetailsServiceAutoConfiguration", "boot",
         "스프링 기본 사용자 안내. 우리 인증은 따로 있고 이 계정은 쓰이지 않는다"),
        (r"ConnectionWatchdog|ReconnectionHandler", "any",
         "저장소 재접속 절차. 되돌아왔는지는 위의 짝 검사가 본다"),
        (r"More than one TaskScheduler", "boot", "설정 안내"),
        (r"schema \"user_service\" already exists", "boot", "초기화가 먼저 만들어 둔 것"),
        (r"tester seed enabled", "any", "시험 스택에서만. 운영이면 위에서 실패로 센다"),
        # 저장소가 다시 뜨면 쥐고 있던 연결이 끊긴다. 풀이 스스로 버리고 새로
        # 맺는다. 저장소를 안 건드렸는데 되풀이되면 그때는 신호다.
        (r"Failed to validate connection.*connection has been closed", "any",
         "저장소가 다시 뜬 뒤의 연결 정리"),
        # 짝이 있는지는 위의 짝 검사가 본다.
        (r"stream poll error", "any", "스트림 읽기 실패. 되살아났는지는 짝 검사가 본다"),
        (r"Tomcat (initialized|started)|Starting Servlet|Initializing Servlet", "boot", "기동 안내"),
        (r"HikariPool-\d+ - (Starting|Start completed|Added connection)", "boot", "연결 준비"),
        (r"Flyway|Migrating schema|Successfully (validated|applied)|Schema .* is up to date",
         "boot", "표 손질"),
    ],
    "proxy": [
        (r"limiting requests", "any", "요청 상한이 걸린 증거. 0 이 되면 상한이 안 걸리는 것"),
        (r'"(GET|POST|PUT|DELETE|PATCH|HEAD) [^"]*" (10\d|2\d\d|3\d\d|40[0-4]|429)',
         "any", "접근 기록. 5xx 는 아래에서 실패로 센다"),
        (r"can not modify /etc/nginx/conf\.d/default\.conf", "boot", "설정을 읽기전용으로 붙인 결과"),
        (r"/docker-entrypoint\.sh|Configuration complete|using the \"epoll\"|nginx/\d",
         "boot", "기동 안내"),
        (r"\[notice\]|built by gcc|OS: Linux|getrlimit|signal process started|exiting|gracefully shutting down",
         "any", "기동·종료 안내"),
        (r"start worker process|worker process \d+ exited", "boot", "일꾼 관리"),
    ],
    # 경로 엔진 둘은 같은 형식으로 적는다. 여기에 규칙이 없으면 상태 확인이
    # 15초마다 남기는 줄이 통째로 미분류로 쌓여, 정작 봐야 할 줄이 묻힌다.
    "osrm-foot": OSRM_FOLD,
    "osrm-bicycle": OSRM_FOLD,
}

# 보이면 곧바로 실패로 세는 줄. 문구만으로 판정할 수 있는 것만 넣는다.
FATAL = [
    (r"FATAL:", "저장소가 연결을 거절했다"),
    (r"ERROR:  syntax error", "질의가 통째로 실패했다 — 부른 쪽은 빈 결과를 정상으로 읽는다"),
    (r"could not fork|out of shared memory|deadlock detected", "저장소 자원 문제"),
    (r"\[emerg\]", "관문이 뜨지 못했다"),
    (r"no live upstreams|upstream timed out|connect\(\) failed", "상류에 닿지 못했다"),
    (r"Traceback \(most recent call last\)", "처리하지 못한 예외"),
    (r"Exception in thread", "처리하지 못한 예외"),
    (r"startup task \S+ failed", "부팅 때 예약한 일이 실패했다"),
    (r"osrm route failed", "경로 엔진이 답하지 못했다"),
    (r"shutdown grace exceeded|did not settle within grace", "정리 절차가 시간 안에 못 끝났다"),
    (r"MISCONF", "저장이 막혀 있다"),
    (r'"(GET|POST|PUT|DELETE|PATCH) (?!/health/ready )[^"]*" 5\d\d',
     "관문이 5xx 를 돌려줬다"),
]

# 운영에서만 실패로 세는 것. 시험에서는 정상이라 접는다.
FATAL_PROD_ONLY = [
    (r"polling skipped — 키가 없어", "발급처 키가 없어 수집이 통째로 멎었다"),
    (r"tester seed enabled", "비밀번호를 공유하는 계정이 만들어졌다"),
    (r"Run time of job .* was missed by", "예약이 제때 돌지 못했다"),
    (r"rateLimitFilterRegistration was not registered", "앱 쪽 요청 상한이 꺼져 있다"),
]

# 문구는 늘 같고 값만 바뀌는 것들. 값을 봐야 한다.
NUMERIC = [
    (r"checkpoint complete.*write=(\d+\.\d+) s", 10.0, "쓰기 모으기가 오래 걸린다"),
    (r"closed abnormally \((\d+)\)", 0.0, "비정상으로 끊긴 소켓이 있다"),
]

# 짝이 있어야 하는 줄. 앞이 나오면 뒤가 따라와야 한다.
PAIRED = {
    "hub": [
        (r"cache ping failed", r"GET /health/ready .*200", "캐시가 끊긴 뒤 준비 확인이 돌아오지 않았다"),
        (r"GET /health/ready .*503", r"GET /health/ready .*200", "준비 확인이 접힌 채로 남았다"),
    ],
    "agent": [
        (r"streams ping failed", r"GET /health/ready .*200", "저장소가 끊긴 뒤 준비 확인이 돌아오지 않았다"),
        (r"GET /health/ready .*503", r"GET /health/ready .*200", "준비 확인이 접힌 채로 남았다"),
    ],
    "user": [
        (r"Cannot reconnect to", r"Reconnected to", "저장소에 다시 붙지 못했다"),
        (r"stream poll error", r"재구독", "스트림 구독이 되살아나지 않았다"),
    ],
}


def containers(project: str) -> dict[str, str]:
    out = sh("docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
             "--format", "{{.Label \"com.docker.compose.service\"}}\t{{.Names}}")
    return dict(l.split("\t", 1) for l in out.split("\n") if "\t" in l)


def read(cid: str) -> tuple[str, str, bool]:
    """이번에 뜬 뒤로 남긴 것을 전부 읽는다. 표준출력과 오류를 나눈다.

    끝에서 몇 줄만 읽으면 상태 확인이 창을 채워 부팅 기록이 밀려난다.
    다시 뜬 적이 있으면 도커가 이전 생애도 들고 있으므로 시작 시각으로 자른다.
    """
    started = sh("docker", "inspect", cid, "--format", "{{.State.StartedAt}}")
    args = ["docker", "logs", "--timestamps", cid] + (["--since", started] if started else [])
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout, r.stderr, r.returncode == 0


def merged(out: str, err: str) -> list[str]:
    """두 스트림을 시각 순서로 합친다.

    그냥 이어 붙이면 한쪽 전체가 다른 쪽 앞에 놓여 시간 순서가 깨진다. 그러면
    부팅 표식이 뒤로 밀려 운전 중에 남은 줄까지 전부 부팅 구간으로 읽힌다 —
    구간을 나누는 의미가 사라진다.
    """
    rows = [l for l in (out + err).splitlines() if l.strip()]
    rows.sort(key=lambda l: l.split(" ", 1)[0])
    # 시각 앞머리를 떼고 돌려준다. 규칙은 본문만 본다.
    return [l.split(" ", 1)[1] if " " in l else l for l in rows]


def phase_of(line: str, svc: str, seen_end: bool) -> str:
    return "run" if seen_end else "boot"


def audit_service(svc: str, cid: str) -> dict:
    out, err, ok = read(cid)
    rows = merged(out, err)
    body = "\n".join(rows)
    if not ok or not body.strip():
        check(f"{svc} 기록을 읽었다", False, "읽지 못했다 — 통과로 세지 않는다")
        return {"folded": {}, "unknown": []}
    check(f"{svc} 기록을 읽었다", True, f"{len(body.splitlines())}줄")

    marker = BOOT_END.get(svc, "")
    seen_end = False
    folded: dict[str, int] = {}
    unknown: list[str] = []
    fatals: list[str] = []
    numeric_bad: list[str] = []

    rules = FOLD.get(svc, [])
    for raw in rows:
        line = raw.rstrip()
        if not line.strip():
            continue
        ph = phase_of(line, svc, seen_end)
        if marker and marker in line:
            seen_end = True

        for pat, why in FATAL + (FATAL_PROD_ONLY if ENV == "prod" else []):
            if re.search(pat, line):
                fatals.append(f"{why}: {line[:110]}")
                break
        else:
            for pat, limit, why in NUMERIC:
                m = re.search(pat, line)
                if m and float(m.group(1)) > limit:
                    numeric_bad.append(f"{why}({m.group(1)}): {line[:90]}")
                    break
            else:
                for pat, want_ph, _why in rules:
                    if (want_ph == ph or want_ph == "any") and re.search(pat, line):
                        folded[pat] = folded.get(pat, 0) + 1
                        break
                else:
                    unknown.append(line[:140])

    for want in MUST_HAVE.get(svc, []):
        check(f"{svc} 부팅에 '{want[:34]}' 가 있다", want in body,
              "" if want in body else "그 일을 하지 않았다")

    for pat, mate, why in PAIRED.get(svc, []):
        # 횟수로 견주면 안 된다. 다시 붙으려는 시도는 여러 줄 남기고 성공은
        # 한 줄만 남기므로, 제대로 되살아났는데도 모자란 것으로 읽힌다.
        # 마지막 사고 뒤에 되살아난 줄이 있는지만 본다.
        bad = [m.end() for m in re.finditer(pat, body)]
        if bad:
            good = [m.end() for m in re.finditer(mate, body)]
            check(f"{svc} '{pat[:24]}' 뒤에 되살아난 줄이 있다",
                  bool(good) and max(good) > max(bad), why)

    check(f"{svc} 곧바로 실패로 세는 줄이 없다", not fatals,
          "; ".join(fatals[:2]))
    check(f"{svc} 수치가 임계를 넘지 않는다", not numeric_bad,
          "; ".join(numeric_bad[:2]))
    check(f"{svc} 분류되지 않은 줄이 없다", not unknown,
          f"{len(unknown)}줄 (첫 줄: {unknown[0][:80]})" if unknown else "")
    return {"folded": folded, "unknown": unknown}


def audit_metrics(svc: str, cid: str) -> None:
    """로그에 한 줄도 안 남는 실패를 본다.

    자원 상한에 부딪히는 것은 어느 로그에도 안 적힌다. 상태 확인은 계속
    정상이라고 답하고, 죽기 전까지는 겉으로 아무 일도 없어 보인다.
    """
    info = sh("docker", "inspect", cid, "--format",
              "{{.RestartCount}}|{{.State.OOMKilled}}|{{.State.Health.Status}}")
    parts = info.split("|") if info else ["", "", ""]
    check(f"{svc} 스스로 죽은 적이 없다", parts[0] in ("0", ""), f"재시작 {parts[0]}회")
    check(f"{svc} 메모리로 죽지 않았다", parts[1] != "true")

    ev = sh("docker", "exec", cid, "cat", "/sys/fs/cgroup/memory.events")
    hit = re.search(r"^max (\d+)", ev, re.M)
    kill = re.search(r"^oom_kill (\d+)", ev, re.M)
    if kill and kill.group(1) != "0":
        check(f"{svc} 메모리 상한에 걸려 죽은 적이 없다", False, f"{kill.group(1)}회")
    if hit and hit.group(1) != "0":
        peak = sh("docker", "exec", cid, "cat", "/sys/fs/cgroup/memory.peak")
        cap = sh("docker", "exec", cid, "cat", "/sys/fs/cgroup/memory.max")
        # 상한에 닿는 것 자체는 죽음이 아니다. 다만 여유가 없다는 뜻이고,
        # 그 사실이 로그에도 상태 확인에도 나타나지 않는다.
        mb = lambda v: f"{int(v)//1048576}MiB" if v.isdigit() else v
        check(f"{svc} 메모리 여유가 있다", False,
              f"상한 접촉 {hit.group(1)}회, 정점 {mb(peak)}/{mb(cap)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="map-test")
    # 같은 문구가 환경에 따라 뜻이 갈리는 것들이 있다. 발급처 키가 없어
    # 스텁으로 도는 것은 시험에서는 정상이고 운영에서는 데이터가 영영
    # 채워지지 않는다는 뜻이다.
    ap.add_argument("--env", choices=["test", "prod"], default="test")
    args = ap.parse_args()

    global ENV
    ENV = args.env
    found = containers(args.project)
    print(f"[대상] {args.project} — {len(found)}개: {' '.join(sorted(found))}\n")
    if not found:
        print("대상이 없다", file=sys.stderr)
        return 1

    all_folded: dict[str, dict[str, int]] = {}
    for svc in sorted(found):
        print(f"[{svc}]")
        r = audit_service(svc, found[svc])
        audit_metrics(svc, found[svc])
        all_folded[svc] = r["folded"]
        print()

    print("[접어 둔 것 — 개수가 0 이 되거나 갑자기 늘면 그 자체가 신호다]")
    for svc, folded in all_folded.items():
        for pat, n in sorted(folded.items(), key=lambda x: -x[1])[:4]:
            print(f"  {svc:9} {n:5}건  {pat[:60]}")

    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"\nRESULT {len(RESULTS) - len(failed)}/{len(RESULTS)} 통과")
    for n, d in failed:
        print(f"  실패  {n} {d}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
