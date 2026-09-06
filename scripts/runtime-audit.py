#!/usr/bin/env python3
"""Read-only Docker metadata and retained-log inventory. Never print raw logs/env."""
import collections
import json
import re
import subprocess
import sys


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def severity(line):
    # Structured log levels take precedence over words in the message body.
    body = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", line)
    try:
        value = json.loads(body)
        if isinstance(value, dict):
            level = str(value.get("level", value.get("severity", ""))).upper()
            if level:
                return {"WARN": "WARNING", "ERR": "ERROR"}.get(level, level)
    except ValueError:
        pass
    patterns = [r"\[(debug|info|notice|warn|warning|error|crit|alert|emerg)\]",
                r"\b(DEBUG|INFO|WARN|WARNING|ERROR|FATAL|PANIC)\s*(?=:|\s+\d|\s+---|\s+\[)"]
    for pattern in patterns:
        match = re.search(pattern, body, re.I)
        if match:
            level = match.group(1).upper()
            return "WARNING" if level == "WARN" else level
    return "UNCLASSIFIED"


SIGNATURES = {
    "rate_limit": r"limiting requests|rate.?limit|RESOURCE_EXHAUSTED",
    "db_authentication": r"password authentication failed",
    "db_missing_column": r"column .* does not exist|UndefinedColumn",
    "db_missing_table": r"relation .* does not exist|UndefinedTable",
    "connection_refused": r"connection refused|ConnectionRefusedError",
    "migration_revision": r"can.t locate revision",
    "timeout": r"TimeoutError|ReadTimeout|ConnectTimeout|timed out",
    "memory": r"out of memory|OutOfMemory|OOMKilled",
    "permission": r"permission denied",
}


def log_summary(raw):
    levels, signatures, latest = collections.Counter(), collections.Counter(), {}
    lines = raw.splitlines()
    for line in lines:
        level = severity(line)
        levels[level] += 1
        if level not in {"ERROR", "FATAL", "PANIC", "CRIT", "ALERT", "EMERG"}:
            continue
        found = False
        stamp = re.match(r"^(\d{4}-\d\d-\d\dT\S+)", line)
        for name, pattern in SIGNATURES.items():
            if re.search(pattern, line, re.I):
                signatures[name] += 1
                latest[name] = stamp.group(1) if stamp else "timestamp_unavailable"
                found = True
        if not found:
            signatures["other_error"] += 1
    return {"retained_lines": len(lines), "retained_bytes": len(raw.encode()),
            "severity_counts": dict(levels), "error_signatures": dict(signatures),
            "last_signature_at": latest}


def main():
    found = docker("ps", "-a", "--format", "{{json .}}")
    if found.returncode:
        print(json.dumps({"error": "docker_inventory_unavailable", "exit_code": found.returncode}))
        return 1
    rows = [json.loads(line) for line in found.stdout.splitlines() if line]
    ids = [r["ID"] for r in rows if "map" in r.get("Names", "").lower()
           or "map-service" in r.get("Image", "")]
    if not ids:
        print(json.dumps({"containers": 0}))
        return 0
    result = docker("inspect", *ids)
    if result.returncode:
        print(json.dumps({"error": "docker_inspect_unavailable"}))
        return 1
    items = json.loads(result.stdout)
    for item in items:
        state, host = item.get("State", {}), item.get("HostConfig", {})
        logs = docker("logs", "--timestamps", item["Id"])
        record = {"name": item["Name"].lstrip("/"), "id": item["Id"][:12],
                  "image": item.get("Config", {}).get("Image"),
                  "state": state.get("Status"), "exit_code": state.get("ExitCode"),
                  "oom_killed": state.get("OOMKilled"), "restart_count": item.get("RestartCount"),
                  "started_at": state.get("StartedAt"), "finished_at": state.get("FinishedAt"),
                  "health": state.get("Health", {}).get("Status", "not_configured"),
                  "memory_limit_bytes": host.get("Memory"), "nano_cpus": host.get("NanoCpus"),
                  "log_driver": host.get("LogConfig", {}).get("Type"),
                  "log_limits": {k: v for k, v in host.get("LogConfig", {}).get("Config", {}).items()
                                 if k in {"max-size", "max-file"}},
                  "logs_available": logs.returncode == 0}
        if logs.returncode == 0:
            record.update(log_summary(logs.stdout + "\n" + logs.stderr))
        print(json.dumps(record, ensure_ascii=False))
    running = [i["Id"] for i in items if i.get("State", {}).get("Running")]
    if running:
        stats = docker("stats", "--no-stream", "--format", "{{json .}}", *running)
        for line in stats.stdout.splitlines():
            row = json.loads(line)
            print(json.dumps({"stats": {k: row.get(k) for k in
                                       ("Name", "CPUPerc", "MemUsage", "MemPerc", "NetIO", "BlockIO", "PIDs")}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
