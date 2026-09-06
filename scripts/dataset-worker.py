#!/usr/bin/env python3
"""Offline synthetic-only job executor; receives dataset artifacts, never serving secrets."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time

# Reviewed existing Agent aggregator. A program update requires a source review and new pin.
PROGRAM_SHA256 = "019fabbf99318f0d0a6970e7476cf1f8047866eedfa2a9dab09e9842a4c034fb"
MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 10000


def require(condition, message):
    if not condition: raise ValueError(message)


def unique(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def read_json(path, limit=65536):
    require(path.stat().st_size <= limit, "manifest size exceeded")
    return json.loads(path.read_text(), object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, ensure_ascii=False, allow_nan=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(name, path)


def run(job_path, dataset_root, output_root, program):
    started = time.monotonic()
    forbidden = ("POSTGRES_", "SPRING_DATASOURCE_", "REDIS_", "LOCATION_", "JWT_", "SSH_", "INTERNAL_SERVICE_TOKEN")
    require(not any(name.startswith(forbidden) and value for name, value in os.environ.items()), "serving credentials forbidden")
    require(job_path.is_file() and not job_path.is_symlink(), "invalid job file")
    job = read_json(job_path)
    require(set(job) == {"schema_version", "job_id", "data_scope", "approved", "dataset", "dataset_sha256", "rows", "max_bytes", "max_seconds", "min_support"}, "invalid job fields")
    require(job["schema_version"] == 1 and type(job["schema_version"]) is int, "invalid job version")
    require(job["data_scope"] == "synthetic" and job["approved"] is True, "real-user learning HOLD")
    require(isinstance(job["job_id"], str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", job["job_id"]), "invalid job id")
    require(isinstance(job["dataset"], str) and re.fullmatch(r"[a-zA-Z0-9_-]+\.jsonl", job["dataset"]), "invalid dataset name")
    require(isinstance(job["dataset_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", job["dataset_sha256"]), "invalid dataset digest")
    for key, minimum, maximum in (("rows", 0, MAX_ROWS), ("max_bytes", 1, MAX_BYTES), ("max_seconds", 1, 300), ("min_support", 3, 10000)):
        require(type(job[key]) is int and minimum <= job[key] <= maximum, "invalid job budget")
    require(hashlib.sha256(program.read_bytes()).hexdigest() == PROGRAM_SHA256, "program digest mismatch")
    dataset = dataset_root / job["dataset"]
    require(dataset.is_file() and not dataset.is_symlink() and dataset.resolve().parent == dataset_root.resolve(), "dataset outside input directory")
    require(dataset.stat().st_size <= job["max_bytes"], "dataset byte budget exceeded")
    content = dataset.read_bytes()
    require(hashlib.sha256(content).hexdigest() == job["dataset_sha256"], "dataset digest mismatch")
    require(output_root.is_dir() and not output_root.is_symlink(), "private output directory required")
    require(output_root.stat().st_mode & 0o077 == 0, "private output permissions required")
    dest = output_root / job["job_id"]
    dest.mkdir(mode=0o700, exist_ok=True)
    require(not dest.is_symlink(), "invalid output job directory")
    with (dest / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        job_hash = hashlib.sha256(job_path.read_bytes()).hexdigest()
        receipt = dest / "receipt.json"
        if receipt.exists():
            old = read_json(receipt)
            require(old["job_sha256"] == job_hash and old["status"] == "candidate", "conflicting or terminal job id")
            require(hashlib.sha256((dest / "candidate.json").read_bytes()).hexdigest() == old["candidate_sha256"], "existing candidate corrupted")
            return {"status": "already_completed", "job_id": job["job_id"]}
        if (dest / "started.json").exists():
            require(read_json(dest / "started.json")["job_sha256"] == job_hash, "conflicting interrupted job id")
        atomic_json(dest / "started.json", {"job_id": job["job_id"], "job_sha256": job_hash, "status": "running"})
        def cancelled(signum, _frame): raise InterruptedError("job cancelled" if signum != signal.SIGALRM else "job time budget exceeded")
        previous = {s: signal.signal(s, cancelled) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
        signal.alarm(job["max_seconds"])
        try:
            rows = []
            for line in content.splitlines():
                if not line.strip(): continue
                require(len(rows) < MAX_ROWS, "dataset row budget exceeded")
                row = json.loads(line, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
                require(isinstance(row, dict) and type(row.get("schema_version")) is int and row["schema_version"] == 1, "unsupported dataset row")
                require(type(row.get("l1_eligible")) is bool and isinstance(row.get("candidates", []), list), "invalid row labels")
                rows.append(row)
            require(len(rows) == job["rows"], "dataset row count mismatch")
            spec = importlib.util.spec_from_file_location("approved_segment_stats", program)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            candidate = module.build(rows, job["min_support"])
            atomic_json(dest / "candidate.json", candidate)
            result = {"job_id": job["job_id"], "job_sha256": job_hash, "data_scope": "synthetic", "status": "candidate",
                      "dataset_sha256": job["dataset_sha256"], "program_sha256": PROGRAM_SHA256,
                      "candidate_sha256": hashlib.sha256((dest / "candidate.json").read_bytes()).hexdigest(),
                      "rows": len(rows), "seconds": round(time.monotonic() - started, 3),
                      "finished_at": datetime.now(timezone.utc).isoformat(), "serving_promotion": False}
            atomic_json(receipt, result)
            return result
        except Exception as error:
            atomic_json(receipt, {"job_id": job["job_id"], "job_sha256": job_hash, "status": "cancelled" if isinstance(error, InterruptedError) else "failed", "error_type": type(error).__name__})
            raise
        finally:
            signal.alarm(0)
            for number, handler in previous.items(): signal.signal(number, handler)


def main():
    parser = argparse.ArgumentParser()
    for key in ("job", "dataset-root", "output-root", "program"): parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.job, args.dataset_root, args.output_root, args.program)))
    except Exception as error:
        print(json.dumps({"status": "rejected", "error_type": type(error).__name__}))
        return 1
    return 0

if __name__ == "__main__": sys.exit(main())
