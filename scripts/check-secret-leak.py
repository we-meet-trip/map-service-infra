#!/usr/bin/env python3
"""환경파일의 비밀값이 소스에 들어가 있는지 본다.

값 대조로 본다. 어떤 경로로 새든 걸린다 — 코드에 직접 적었든, 시험의 본보기로
붙여 넣었든, 로그 예시로 남겼든 상관없다.

앞부분만 쓰인 경우도 본다. 실제로 그렇게 샌 적이 있다. 로그에서 키가 가려지는지
확인하는 시험이, 가려질 대상으로 진짜 키의 앞 열여섯 자를 적어 두고 있었다.
전체 일치만 보면 그런 것이 하나도 안 걸린다. 퍼센트 인코딩된 모양도 함께 본다.

이 검사는 환경파일이 있는 곳에서만 돌 수 있다. 밀 때마다 도는 자리에서는
그 파일을 볼 수 없으므로, 판을 올리기 전에 손으로 부른다.

  python3 scripts/check-secret-leak.py [--env-file .env]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.parse

# 이름에 이 낱말이 있으면 비밀로 다룬다.
SECRETISH = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")

# 값이 짧으면 우연히 겹친다. 조각도 이 길이 아래로는 보지 않는다.
MIN_LEN = 12

# 큰 파일은 소스가 아니다.
MAX_BYTES = 3_000_000


def env_values(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if len(v) >= MIN_LEN and any(s in k for s in SECRETISH):
                out[k] = v
    return out


def fragments(value: str) -> list[tuple[str, str]]:
    """찾아볼 조각들. 긴 것부터."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    enc = urllib.parse.quote(value, safe="")
    for src, label in ((value, "그대로"), (enc, "인코딩")):
        for n in (len(src), 32, 16, MIN_LEN):
            if MIN_LEN <= n <= len(src):
                frag = src[:n]
                if frag not in seen:
                    seen.add(frag)
                    out.append((frag, label if n == len(src) else f"{label} 앞{n}자"))
    return out


def tracked(repo: str) -> list[str]:
    r = subprocess.run(["git", "-C", repo, "ls-files"],
                       capture_output=True, text=True)
    return [f for f in r.stdout.split("\n") if f]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--root", default="..",
                    help="레포들이 나란히 있는 자리")
    args = ap.parse_args()

    if not os.path.isfile(args.env_file):
        print(f"환경파일이 없다: {args.env_file}", file=sys.stderr)
        return 2

    targets = env_values(args.env_file)
    if not targets:
        print("검사할 값이 없다.")
        return 0

    repos = sorted(
        d for d in os.listdir(args.root)
        if os.path.isdir(os.path.join(args.root, d, ".git"))
    )

    leaks: list[tuple[str, str, str]] = []   # 소스에 들어간 값
    sames: list[tuple[str, str]] = []        # 본보기와 같은 값

    for repo in repos:
        base = os.path.join(args.root, repo)
        for rel in tracked(base):
            path = os.path.join(base, rel)
            try:
                if os.path.getsize(path) > MAX_BYTES:
                    continue
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except (OSError, IsADirectoryError):
                continue
            for name, value in targets.items():
                for frag, label in fragments(value):
                    if frag in text:
                        where = f"{repo}/{rel}"
                        # 본보기 파일에만 있으면, 샌 것이 아니라 운영 값을
                        # 아직 본보기 그대로 두고 있다는 뜻이다.
                        if os.path.basename(rel).startswith(".env.example"):
                            sames.append((name, where))
                        else:
                            leaks.append((name, where, label))
                        break

    for name, where, label in sorted(set(leaks)):
        print(f"✗ {name} 의 값이 {where} 에 있다 ({label})")
    for name, where in sorted(set(sames)):
        print(f"✗ {name} 이 공개된 본보기와 같은 값이다 — {where}")

    total = len(set(leaks)) + len(set(sames))
    print(f"\nRESULT {'통과' if total == 0 else f'{total}건'}"
          f" (값 {len(targets)}개 · 레포 {len(repos)}개)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
