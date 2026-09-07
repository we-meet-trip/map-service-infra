#!/usr/bin/env python3
"""GitHub-only disposable Linux/systemd + empty Docker-daemon crash fixture.

No MAP images, real databases, keys, host Docker socket mounts or host volumes.
Inner Docker runs busybox sentinels; production HTTP/digest enrollment is NOT tested.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]


def call(args, *, data=None, timeout=180, check=True):
    p = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError("fixture command failed")
    return p.stdout.decode().strip()


def wait_for(fn, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if fn():
                return
        except Exception:
            pass
        time.sleep(.25)
    raise RuntimeError("fixture deadline exceeded")


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true" or sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("disposable GitHub Linux runner required")
    os.umask(0o077)
    spec = importlib.util.spec_from_file_location("guard", ROOT / "scripts/cutover_watchdog.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    d = guard.receiver()
    outer = "map-cutover-fixture-" + uuid.uuid4().hex[:12]
    report = {"scope": "disposable_github_linux_systemd_and_empty_dind",
              "real_map_data": False, "production_bff_health_tested": False,
              "full_vm_power_cycle": False, "tests": {}}
    outer_created = False
    unit_created = False
    # Do not touch a pre-existing transient unit, even on a disposable runner.
    if call(["systemctl", "show", guard.RECEIVER_UNIT, "-p", "LoadState", "--value"]) != "not-found":
        raise RuntimeError("fixture receiver unit already exists")
    try:
        call(["docker", "pull", "docker:29.8.0-dind"])
        call(["docker", "pull", "busybox:1.37.0"])
        call(["docker", "run", "-d", "--name", outer, "--privileged", "--network", "none",
              "--memory", "1g", "--cpus", "1", "--pids-limit", "256", "-e", "DOCKER_TLS_CERTDIR=",
              "docker:29.8.0-dind", "--iptables=false", "--ip6tables=false", "--bridge=none",
              "--storage-driver=vfs", "--data-root=/fixture-data"])
        outer_created = True
        inner = lambda args, **kw: call(["docker", "exec", outer, "docker", *args], **kw)
        wait_for(lambda: bool(inner(["info", "--format", "{{.ID}}"])))
        assert inner(["ps", "-aq"]) == ""
        assert inner(["volume", "ls", "-q"]) == ""
        report["tests"]["empty_separate_daemon"] = "PASS"
        image = subprocess.run(["docker", "save", "busybox:1.37.0"], capture_output=True, check=True).stdout
        call(["docker", "exec", "-i", outer, "docker", "load"], data=image)
        del image
        sentinels = ("postgres", "redis", "osrm-foot", "osrm-bicycle", "prometheus", "grafana")
        for service in (*guard.PUBLIC, *sentinels):
            inner(["run", "-d", "--name", "fixture-" + service, "--network", "none",
                   "--label", "com.docker.compose.project=map-test",
                   "--label", "com.docker.compose.service=" + service,
                   "--restart", "no" if service in guard.PUBLIC else "unless-stopped",
                   "busybox:1.37.0", "sleep", "3600"])
        with tempfile.TemporaryDirectory(prefix="cutover-acceptance-") as temp:
            d.STATE = Path(temp)
            # Commands remain real; only the transport targets the empty inner daemon.
            original = d.command
            calls = []
            def routed(args, **kwargs):
                calls.append(tuple(args))
                if args[0] == "docker":
                    args = ["docker", "exec", outer, *args]
                return original(args, **kwargs)
            d.command = routed
            d.smoke = lambda **kwargs: None  # Explicit synthetic sentinel scope.
            (d.STATE / "public-restart.yml").write_text(guard.OVERRIDE)
            actual = guard.containers(d)
            before = {s: inner(["inspect", "--format", "{{.Id}}:{{.Image}}", "fixture-" + s]) for s in sentinels}
            digest = next(iter(actual.values()))["image"]
            approved = {s: f"{d.release.REGISTRY}/map-service-{s}@{digest}" for s in d.release.SERVICES}
            policy = {"schema_version": 1, "instance_id": d.INSTANCE_ID,
                      "candidate_allowed": [approved], "rollback_verified": []}
            latch = {"schema_version": 1, "instance_id": d.INSTANCE_ID, "run_id": "123",
                     "infra_sha": "a" * 40, "phase": "complete", "bundle": "/fixture/bundle",
                     "candidate": approved, "prior_rollback_compatible": False}
            ready = {k: latch[k] for k in ("schema_version", "instance_id", "run_id", "infra_sha", "phase")}
            ready.update(approved=approved, public={s: {k: x[k] for k in ("id", "image")} for s, x in actual.items()})
            d.atomic_state(d.STATE / "rollback-policy.json", policy)
            d.atomic_state(d.STATE / "security-cutover.json", latch)
            d.atomic_state(d.STATE / "security-public-ready.json", ready)
            assert guard.recover_once(d) == "completed_public_supervised"
            assert not [a for a in calls if a[:2] in (("docker", "start"), ("docker", "stop"))]
            report["tests"]["complete_no_false_stop"] = "PASS"
            # Restart policies start monitoring after 10s; use a real steady fixture.
            time.sleep(11)
            latch["phase"] = "opening_ingress"
            d.atomic_state(d.STATE / "security-cutover.json", latch)
            script = d.STATE / "orphan-fixture.py"
            script.write_text("""import fcntl, os, pathlib, signal, subprocess, sys, time
root=pathlib.Path(sys.argv[1])
lock=(root/'deploy.lock').open('a')
fcntl.flock(lock,fcntl.LOCK_EX)
child=subprocess.Popen([sys.executable,'-c',"import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(4); pathlib.Path("+repr(str(root/'orphan-ran'))+").write_text('unsafe')"],start_new_session=True)
(root/'main-ready').write_text(str(os.getpid()))
time.sleep(60)
""")
            call(["systemd-run", "--quiet", "--unit=" + guard.RECEIVER_UNIT, "--service-type=exec",
                  "--property=KillMode=control-group", "--property=TimeoutStopSec=1",
                  "--property=SendSIGKILL=yes", "--property=RuntimeMaxSec=90",
                  sys.executable, str(script), temp])
            unit_created = True
            wait_for(lambda: (d.STATE / "main-ready").is_file())
            calls.clear()
            try:
                guard.recover_once(d)
            except d.DeployError:
                pass  # Real flock blocks concurrent deployment supervision.
            assert not [a for a in calls if a[:2] == ("docker", "stop")]
            killed_at = time.monotonic()
            call(["systemctl", "kill", "--signal=SIGKILL", "--kill-whom=main", guard.RECEIVER_UNIT])
            wait_for(lambda: call(["systemctl", "show", guard.RECEIVER_UNIT, "-p", "ActiveState", "--value"]) in ("failed", "inactive"))
            assert guard.recover_once(d) == "public_quarantined"
            report["sigkill_to_four_stopped_seconds"] = round(time.monotonic() - killed_at, 3)
            time.sleep(4.2)
            assert not (d.STATE / "orphan-ran").exists()
            assert not any(x["running"] for x in guard.containers(d).values())
            report["tests"]["sigkill_main_reaps_detached_child_and_quarantines_four"] = "PASS"
            # Abruptly kill a DIFFERENT, empty-fixture Docker daemon. Existing host
            # Docker/systemd/data services are never restarted by this drill.
            for service in guard.PUBLIC:
                inner(["start", "fixture-" + service])
            call(["docker", "kill", "--signal=KILL", outer])
            call(["docker", "start", outer])
            wait_for(lambda: bool(inner(["info", "--format", "{{.ID}}"])))
            assert not any(x["running"] for x in guard.containers(d).values())
            assert guard.recover_once(d) == "public_quarantined"
            for service in sentinels:
                wait_for(lambda s=service: inner(["inspect", "--format", "{{.State.Running}}", "fixture-" + s]) == "true")
                assert inner(["inspect", "--format", "{{.Id}}:{{.Image}}", "fixture-" + service]) == before[service]
            report["tests"]["daemon_crash_pending_public_stays_closed_sentinels_resume"] = "PASS"
            latch["phase"] = "complete"
            d.atomic_state(d.STATE / "security-cutover.json", latch)
            assert guard.recover_once(d) == "completed_public_supervised"
            assert all(x["running"] for x in guard.containers(d).values())
            report["tests"]["completed_exact_receipt_resumes_public"] = "PASS"
        report["status"] = "PASS"
    finally:
        if unit_created:
            call(["systemctl", "stop", guard.RECEIVER_UNIT], check=False)
            call(["systemctl", "reset-failed", guard.RECEIVER_UNIT], check=False)
        if outer_created:
            call(["docker", "rm", "-fv", outer], check=False)
        evidence = ROOT / "cutover-linux-acceptance.json"
        evidence.write_text(json.dumps(report, indent=2) + "\n")
        # Only fixed outcome fields and elapsed seconds; no private fixture state.
        # The unprivileged artifact action must be able to read this report.
        evidence.chmod(0o644)
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
