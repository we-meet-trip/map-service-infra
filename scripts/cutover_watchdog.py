#!/usr/bin/env python3
"""Supervise only four MAP public services; no secrets, pulls or Compose execution.

The installed receiver, this file and release_manifest.py form one reviewed unit.
All decisions read durable state AFTER obtaining the receiver's deployment lock.
Docker restart remains unchanged for every other service.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

PUBLIC = ("edge", "proxy", "user", "yolo")
TERMINAL = ("complete", "rolled_back")
# A replacement in progress is a state the supervisor waits out rather than
# closes: the containers it would stop are the ones still serving.
TOLERATED = TERMINAL + ("rollover",)
UPSTREAMS = Path("/var/lib/map-deploy/upstreams")
OVERRIDE = "services:\n" + "".join(f"  {s}:\n    restart: 'no'\n" for s in PUBLIC)
RECEIVER_UNIT = "map-deploy-receive.service"
ENV = {"PATH": "/usr/bin:/bin", "DOCKER_HOST": "unix:///var/run/docker.sock",
       "DOCKER_CONFIG": "/var/empty"}


def receiver():
    spec = importlib.util.spec_from_file_location("guard_receiver", Path(__file__).with_name("deploy-gcp.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(d, args, timeout=30):
    return d.command(args, env=ENV, cwd=d.STATE, timeout=timeout)


def verify_override(d):
    path = d.STATE / "public-restart.yml"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        d.validate_host_metadata(os.fstat(stream.fileno()))
        d.require(stream.read(len(OVERRIDE) + 1) == OVERRIDE.encode(), "invalid public restart override")


def containers(d, services=PUBLIC):
    result = {}
    for service in services:
        project = "map-admin-test" if service in ("admin", "admin-web") else "map-test"
        ids = run(d, ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}",
                      "--filter", f"label=com.docker.compose.service={service}"]).splitlines()
        d.require(len(ids) == 1 and re.fullmatch(r"[a-f0-9]{12,64}", ids[0]), "expected one reviewed container")
        # Do not request Config.Env, mounts, logs or complete inspect JSON.
        fmt = ('{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},'
               '"restart":{{json .HostConfig.RestartPolicy.Name}}}')
        item = json.loads(run(d, ["docker", "inspect", "--format", fmt, ids[0]]))
        d.require(set(item) == {"id", "image", "running", "restart"}
                  and re.fullmatch(r"[a-f0-9]{64}", item["id"])
                  and d.release.DIGEST.fullmatch(item["image"])
                  and type(item["running"]) is bool and isinstance(item["restart"], str), "invalid container metadata")
        result[service] = item
    return result


def return_to_canonical(d):
    """Send traffic back to the canonical containers and leave everything running.

    Only override files are removed and the proxy is asked to re-read them. No
    container is started or stopped here, so a partly finished replacement ends
    with whichever copy is healthy still answering.
    """
    try:
        removed = []
        if UPSTREAMS.is_dir():
            for path in sorted(UPSTREAMS.glob("*.conf")):
                path.unlink()
                removed.append(path.name)
        actual = containers(d)
        d.require(all(item["running"] for item in actual.values()), "a public container is not running")
        run(d, ["docker", "exec", actual["proxy"]["id"], "nginx", "-t"])
        run(d, ["docker", "exec", actual["proxy"]["id"], "nginx", "-s", "reload"])
        return {"removed": removed}
    except Exception:
        return None


def policy_for_latch(d, latch):
    return d.load_rollback_policy(latch["candidate"])


def approved_tuple(d, latch, phase):
    policy = policy_for_latch(d, latch)
    choices = [latch["candidate"]] if phase == "complete" else policy["rollback_verified"]
    actual = containers(d, d.release.SERVICES)
    for candidate in choices:
        try:
            if all(run(d, ["docker", "image", "inspect", "--format", "{{.Id}}", candidate[s]]) == actual[s]["image"]
                   for s in d.release.SERVICES):
                return candidate
        except d.DeployError:
            # An absent earlier approved tuple must not conceal a later exact
            # locally present one. Never pull an image to make this check pass.
            continue
    raise d.DeployError("ready receipt requires exact approved six-image identity")


def write_ready_receipt(d, latch, phase):
    d.require(phase in TERMINAL, "invalid ready phase")
    verify_override(d)
    approved = approved_tuple(d, latch, phase)
    actual = containers(d)
    d.require(all(item["running"] and item["restart"] == "no" for item in actual.values()),
              "public services must be running without Docker restart")
    value = {"schema_version": 1, "instance_id": d.INSTANCE_ID, "run_id": latch["run_id"],
             "infra_sha": latch["infra_sha"], "phase": phase, "approved": approved,
             "public": {s: {k: item[k] for k in ("id", "image")} for s, item in actual.items()}}
    d.atomic_state(d.STATE / "security-public-ready.json", value)


def read_ready_receipt(d, latch):
    data = d.read_host_json(d.STATE / "security-public-ready.json")
    d.require(isinstance(data, dict) and set(data) ==
              {"schema_version", "instance_id", "run_id", "infra_sha", "phase", "approved", "public"}
              and type(data["schema_version"]) is int and data["schema_version"] == 1
              and all(data[k] == latch[k] for k in ("instance_id", "run_id", "infra_sha", "phase")),
              "stale or invalid ready receipt")
    policy = policy_for_latch(d, latch)
    choices = [latch["candidate"]] if latch["phase"] == "complete" else policy["rollback_verified"]
    d.require(d.exact_image_tuple(data["approved"]) in choices, "ready tuple no longer approved")
    d.require(isinstance(data["public"], dict) and set(data["public"]) == set(PUBLIC), "invalid public receipt")
    for value in data["public"].values():
        d.require(isinstance(value, dict) and set(value) == {"id", "image"}
                  and isinstance(value["id"], str) and re.fullmatch(r"[a-f0-9]{64}", value["id"])
                  and isinstance(value["image"], str) and d.release.DIGEST.fullmatch(value["image"]),
                  "invalid public receipt identity")
    return data


def maintenance(d):
    path = d.STATE / "public-maintenance.json"
    if not path.exists() and not path.is_symlink():
        return False
    value = d.read_host_json(path)
    d.require(value == {"schema_version": 1, "instance_id": d.INSTANCE_ID, "hold": True}
              and type(value["schema_version"]) is int, "invalid maintenance hold")
    return True


def receiver_scope_active(d):
    # systemd keeps the unit deactivating while SIGKILL descendants are reaped.
    state = run(d, ["systemctl", "show", RECEIVER_UNIT, "--property=ActiveState", "--value"])
    d.require(state in ("active", "activating", "deactivating", "inactive", "failed"), "receiver scope state unavailable")
    return state not in ("inactive", "failed")


def recover_once(d):
    with d.deployment_lock():
        # Never act on a state read before locking or while orphan cleanup is active.
        if receiver_scope_active(d):
            return "receiver_active"
        latch = d.load_cutover_latch()
        d.require(latch is not None, "cutover latch required")
        if not maintenance(d) and latch["phase"] == "rollover":
            # Never read the ready receipt here: during a replacement the recorded
            # identities legitimately differ, and calling that a fault is what
            # closes an entry point that is still healthy.
            if return_to_canonical(d) is not None:
                return "rollover_returned_to_canonical"
            d.stop_public_services(ENV)
            return "public_quarantined"
        if maintenance(d) or latch["phase"] not in TERMINAL:
            d.stop_public_services(ENV)
            return "public_quarantined"
        # Invalid/missing metadata never authorizes a start. Nor does a supervisor
        # read/health failure stop an already healthy completed release.
        verify_override(d)
        ready = read_ready_receipt(d, latch)
        actual = containers(d)
        d.require(all({k: item[k] for k in ("id", "image")} == ready["public"][s]
                      and item["restart"] == "no" for s, item in actual.items()), "public identity or restart contract changed")
        for service in ("yolo", "user", "proxy", "edge"):
            item = actual[service]
            if not item["running"]:
                if service == "edge":
                    with d.topology_scope():
                        d.smoke(include_public=False)
                run(d, ["docker", "start", item["id"]], timeout=60)
        return "completed_public_supervised"


def require_enrolled(d):
    verify_override(d)
    d.require(not maintenance(d), "public maintenance hold blocks deployment")
    d.require(run(d, ["systemctl", "is-active", "map-cutover-watchdog.service"]) == "active",
              "cutover watchdog must be active")


def require_public_restart(d):
    d.require(all(item["restart"] == "no" for item in containers(d).values()),
              "public Docker restart policy changed")


def require_receiver_scope(d):
    # Root SSH wrapper invokes Python directly as the transient unit MainPID.
    group = Path("/proc/self/cgroup").read_text()
    d.require(any(line.endswith("/" + RECEIVER_UNIT) for line in group.splitlines()),
              "receiver requires supervised systemd scope")


def enroll(d):
    d.verify_instance()
    with d.deployment_lock(), d.topology_scope():
        latch = d.load_cutover_latch()
        d.require(latch is not None and latch["phase"] in TERMINAL, "enrollment requires completed cutover")
        d.require(not maintenance(d), "cannot enroll during maintenance")
        approved_tuple(d, latch, latch["phase"])
        # A label alone cannot confer readiness; actual private/public probes run.
        d.smoke()
        actual = containers(d)
        d.require(all(x["running"] for x in actual.values()), "enrollment requires running public services")
        # Use the same fsync/0600 primitive, but preserve YAML bytes exactly.
        path = d.STATE / "public-restart.yml"
        import tempfile
        fd, temporary = tempfile.mkstemp(prefix=".public-restart-", dir=d.STATE)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(OVERRIDE)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(d.STATE, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        for service in PUBLIC:
            run(d, ["docker", "update", "--restart=no", actual[service]["id"]])
        write_ready_receipt(d, latch, latch["phase"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("watch", "once", "enroll", "verify-override"))
    args = parser.parse_args(argv)
    d = receiver()
    last = None
    instance_verified = False
    while True:
        try:
            if args.operation in ("watch", "once") and not instance_verified:
                d.verify_instance()
                instance_verified = True
            if args.operation == "enroll":
                enroll(d)
                phase = "enrolled"
            elif args.operation == "verify-override":
                verify_override(d)
                phase = "override_verified"
            else:
                phase = recover_once(d)
            failed = False
        except Exception:
            # No exception messages, env, subprocess output or stored values.
            phase, failed = "guard_retry_no_unverified_start", True
        if phase != last:
            print(json.dumps({"phase": phase}), flush=True)
            last = phase
        if args.operation != "watch":
            return int(failed)
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
