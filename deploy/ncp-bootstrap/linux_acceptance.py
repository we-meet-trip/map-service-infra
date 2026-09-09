#!/usr/bin/env python3
"""Remote-only acceptance harness; no Docker or formatting on a user's machine.

run requires a disposable GitHub Linux runner. guest requires this harness's
fresh privileged Ubuntu systemd container. The container shares the runner's
kernel and device model: PASS is not independent NCP VM or reboot certification.
Only a newly created sparse fixture file is formatted; no existing block device
or user volume is accepted. GitHub credentials stay in the host download step.

Primary interfaces reviewed 2026-09-07:
https://docs.docker.com/engine/install/ubuntu/
https://docs.docker.com/reference/cli/docker/container/run/
https://systemd.io/CONTAINER_INTERFACE/
https://docs.github.com/en/actions/reference/runners/github-hosted-runners
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "we-meet-trip/map-service-infra"
ANCHOR = ROOT / "docker/caddy-security/install-artifact-20260906.json"
ANCHOR_SHA = "e6c361045e7a57f94aa08b35342972530c4f09a699ab86e6f76999609a82a2cb"
ASSETS = {"map-caddy-security-image-20260906.tar": "archive_sha256",
          "caddy-security-20260906.json": "report_sha256",
          "install-artifact-v2-20260906.json": None}
PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin")
HEX = re.compile(r"[a-f0-9]{64}")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def run(argv, *, input=None, timeout=600, allowed=(0,), env=None):
    result = subprocess.run([str(arg) for arg in argv], input=input, capture_output=True, timeout=timeout, env=env)
    if result.returncode not in allowed:
        tool = Path(str(argv[0])).name
        # This fixture contains only synthetic data. Keep arguments and stdin
        # private, and never include authenticated gh diagnostics.
        detail = result.stderr.decode(errors="replace")[-4096:] if tool != "gh" and input is None else "suppressed"
        raise ValueError("command failed: " + tool + " (exit " + str(result.returncode) + "): " + detail)
    return result.stdout.decode()


def require_ci():
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_OS") == "Linux" and
            platform.system() == "Linux", "remote GitHub Linux runner required; local Docker is forbidden")
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")), "GitHub run identity required")
    require(re.fullmatch(r"[a-f0-9]{40}", os.environ.get("GITHUB_SHA", "")), "exact checkout SHA required")
    require(ROOT == Path(os.environ.get("GITHUB_WORKSPACE", "")).resolve(), "unexpected checkout directory")
    require(run(["git", "-c", "safe.directory=" + str(ROOT), "-C", ROOT, "rev-parse", "HEAD"]).strip() == os.environ["GITHUB_SHA"], "checkout differs from dispatched SHA")


def verify_assets(directory, key_pin):
    require(HEX.fullmatch(key_pin), "reviewed Docker public-key checksum required")
    require(sha(ANCHOR) == ANCHOR_SHA, "tracked Caddy trust anchor changed")
    anchor = json.loads(ANCHOR.read_text())
    for name, field in ASSETS.items():
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "regular Caddy asset required")
        require(sha(path) == (anchor[field] if field else ANCHOR_SHA), "Caddy transfer checksum mismatch")
    key = directory / "docker.asc"
    require(key.is_file() and not key.is_symlink() and key.stat().st_size < 65536 and
            sha(key) == key_pin and b"BEGIN PGP PUBLIC KEY BLOCK" in key.read_bytes(), "Docker public-key pin mismatch")
    run([sys.executable, "-B", ROOT / "scripts/install-caddy-artifact.py", "--archive",
         directory / "map-caddy-security-image-20260906.tar", "--report", directory / "caddy-security-20260906.json"])
    return anchor


def download(args):
    require_ci()
    require(HEX.fullmatch(args.docker_key_sha256), "reviewed Docker public-key checksum required")
    require(sha(ANCHOR) == ANCHOR_SHA, "tracked Caddy trust anchor changed")
    args.assets.mkdir(mode=0o700, parents=False, exist_ok=False)
    anchor = json.loads(ANCHOR.read_text())
    # Draft visibility requires push access; isolated job uses GET APIs only.
    releases = json.loads(run(["gh", "api", f"repos/{REPOSITORY}/releases?per_page=100"]))
    matches = [release for release in releases if release.get("draft") is True and
               release.get("tag_name") == "caddy-security-20260906-v1"]
    require(len(matches) == 1, "exact reviewed Caddy draft release not found")
    release = matches[0]
    asset_list = json.loads(run(["gh", "api", f"repos/{REPOSITORY}/releases/{release['id']}/assets?per_page=100"]))
    records = []
    for name, field in ASSETS.items():
        entries = [asset for asset in asset_list if asset.get("name") == name and asset.get("state") == "uploaded"]
        require(len(entries) == 1, "exact Caddy release asset required")
        entry = entries[0]
        expected = anchor[field] if field else ANCHOR_SHA
        require(entry.get("digest") == "sha256:" + expected, "GitHub asset metadata digest mismatch")
        maximum = anchor["archive_bytes"] if field == "archive_sha256" else 1024 * 1024
        require(type(entry.get("size")) is int and 0 < entry["size"] <= maximum, "Caddy asset size mismatch")
        target = args.assets / name
        with target.open("xb") as stream:
            result = subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                                     f"repos/{REPOSITORY}/releases/assets/{entry['id']}"], stdout=stream,
                                    stderr=subprocess.PIPE, timeout=120)
        require(result.returncode == 0 and target.stat().st_size == entry["size"] and sha(target) == expected,
                "downloaded Caddy bytes differ from reviewed asset")
        records.append({"name": name, "asset_id": entry["id"], "bytes": entry["size"], "sha256": expected})
    request = urllib.request.Request("https://download.docker.com/linux/ubuntu/gpg", headers={"User-Agent": "map-ncp-fixture"})
    with urllib.request.urlopen(request, timeout=30) as response:
        require(response.geturl() == request.full_url, "Docker key redirect requires review")
        key = response.read(65537)
    require(len(key) <= 65536 and hashlib.sha256(key).hexdigest() == args.docker_key_sha256, "Docker public-key pin mismatch")
    (args.assets / "docker.asc").write_bytes(key)
    verify_assets(args.assets, args.docker_key_sha256)
    write_json(args.assets / "download.json", {"release_id": release["id"], "draft": True,
                                              "assets": records, "docker_key_sha256": args.docker_key_sha256})
    print(json.dumps({"status": "reviewed_assets_downloaded", "asset_count": 3, "release_published": False}))


DOCKERFILE = """ARG UBUNTU_BASE
FROM ${UBUNTU_BASE}
ENV DEBIAN_FRONTEND=noninteractive container=docker
RUN apt-get update -qq && apt-get install -y --no-install-recommends systemd systemd-sysv python3 cryptsetup-bin util-linux ca-certificates iptables procps && rm -rf /var/lib/apt/lists/* && rm -f /usr/sbin/policy-rc.d && truncate -s 0 /etc/machine-id
COPY fixture-init /usr/local/sbin/fixture-init
RUN chmod 0755 /usr/local/sbin/fixture-init
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/sbin/fixture-init"]
"""
INIT = """#!/bin/sh
set -eu
mount --make-rshared /
mount -o remount,ro /sys
exec /sbin/init
"""


def run_fixture(args):
    require_ci()
    require(os.geteuid() == 0, "sudo required on disposable runner")
    require(re.fullmatch(r"ubuntu@sha256:[a-f0-9]{64}", args.ubuntu_base), "official Ubuntu digest pin required")
    require(not any(name in os.environ for name in ("GH_TOKEN", "GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "GOOGLE_APPLICATION_CREDENTIALS")),
            "cloud/GitHub credentials must not enter privileged fixture process")
    require(not os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"), "remote Docker environment forbidden")
    args.output.mkdir(mode=0o755, parents=False, exist_ok=False)
    report = {"status": "running", "source_sha": os.environ["GITHUB_SHA"], "github_run_id": os.environ["GITHUB_RUN_ID"],
              "started_at": datetime.now(timezone.utc).isoformat(), "scope": "disposable GitHub runner, private systemd container, real package install",
              "ncp_vms_created": 0, "shared_gcp_actions": 0, "serving_deployments": 0, "independent_vm_verified": False,
              "kernel_shared": True, "real_host_reboot_tested": False, "account_quote": "synthetic fixture only",
              "ubuntu_base": args.ubuntu_base, "events": []}
    work = Path(tempfile.mkdtemp(prefix="map-ncp-fixture-", dir=os.environ["RUNNER_TEMP"]))
    loop = None
    mapped = mounted = created = False
    mapping = "map-ncp-ci-" + uuid.uuid4().hex[:12]
    guest_name = mapping + "-guest"
    guest_image = mapping + ":fixture"
    mount = work / "data"
    disk = work / "blank-data.luks"
    cleanup_errors = []
    try:
        verify_assets(args.assets, args.docker_key_sha256)
        facts = {"cpu": os.cpu_count(), "memory_bytes": int(next(row.split()[1] for row in Path("/proc/meminfo").read_text().splitlines() if row.startswith("MemTotal:"))) * 1024,
                 "root_total_bytes": shutil.disk_usage("/").total, "physical_free_bytes": shutil.disk_usage(work).free}
        report["runner_measured"] = facts
        require(facts["cpu"] >= 4 and facts["memory_bytes"] >= 14.4 * 10**9, "runner does not provide measured 4 CPU / 16 GB public-runner capacity")
        require(facts["root_total_bytes"] >= 36 * 10**9 and facts["physical_free_bytes"] >= 8 * 1024**3,
                "runner lacks measured prod-small root capacity or 8 GiB physical headroom; sparse capacity is not physical capacity")
        context = json.loads(run(["docker", "context", "inspect"]))[0]
        require(context.get("Name") == "default" and context.get("Endpoints", {}).get("docker", {}).get("Host") == "unix:///var/run/docker.sock",
                "only the disposable runner's local Docker daemon may be used")
        host_daemon_id = json.loads(run(["docker", "info", "--format", "{{json .ID}}"] ))
        report["runner_daemon_id"] = host_daemon_id
        run(["docker", "pull", "--platform", "linux/amd64", args.ubuntu_base], timeout=180)
        identity = json.loads(run(["docker", "image", "inspect", args.ubuntu_base]))[0]
        require(identity.get("Os") == "linux" and identity.get("Architecture") == "amd64", "Ubuntu base platform mismatch")
        report["ubuntu_base_local_id"] = identity["Id"]
        (work / "Dockerfile").write_text(DOCKERFILE)
        (work / "fixture-init").write_text(INIT)
        run(["docker", "build", "--platform", "linux/amd64", "--pull=false", "--build-arg", "UBUNTU_BASE=" + args.ubuntu_base,
             "--tag", guest_image, work], timeout=600)
        report["fixture_guest_image_id"] = json.loads(run(["docker", "image", "inspect", "--format", "{{json .Id}}", guest_image]))
        report["events"].append("small_systemd_guest_built_on_remote_runner")
        # EXCL + exact losetup backing identity are mandatory before format.
        fd = os.open(disk, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(fd, 100 * 10**9 + 64 * 1024**2)
        finally:
            os.close(fd)
        key = work / "fixture-only-luks.key"
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        loop = run(["losetup", "--find", "--show", disk]).strip()
        require(re.fullmatch(r"/dev/loop[0-9]+", loop), "unexpected loop identity")
        require(Path(run(["losetup", "--noheadings", "--output", "BACK-FILE", loop]).strip()).resolve() == disk,
                "loop does not belong to the newly created fixture file")
        run(["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--key-file", key, loop])
        run(["cryptsetup", "open", "--type", "luks2", "--key-file", key, loop, mapping])
        mapped = True
        device = "/dev/mapper/" + mapping
        run(["mkfs.ext4", "-q", "-m", "0", "-E", "lazy_itable_init=1,lazy_journal_init=1", device])
        mount.mkdir()
        run(["mount", "-o", "rw,nodev,nosuid", device, mount])
        mounted = True
        filesystem_uuid = run(["blkid", "-s", "UUID", "-o", "value", device]).strip()
        report["fixture_volume"] = {"logical_bytes": disk.stat().st_size, "physical_bytes_at_start": disk.stat().st_blocks * 512,
                                    "filesystem": "ext4", "encryption": "LUKS2", "fresh_fixture_only": True}
        marker = work / "marker.json"
        write_json(marker, {"run_id": os.environ["GITHUB_RUN_ID"], "source_sha": os.environ["GITHUB_SHA"],
                            "data_uuid": filesystem_uuid, "docker_key_sha256": args.docker_key_sha256,
                            "fixture_only": True, "kind": "privileged-systemd-container-not-vm"})
        run(["docker", "run", "--detach", "--name", guest_name, "--hostname", guest_name, "--privileged", "--cgroupns=private",
             "--device", device + ":" + device,
             "--network", "bridge", "--tmpfs", "/run", "--tmpfs", "/run/lock", "--tmpfs", "/tmp",
             "--env", "container=docker", "--env", "container_uuid=" + str(uuid.uuid4()),
             "--mount", f"type=bind,src={ROOT},dst=/opt/map,readonly",
             "--mount", f"type=bind,src={args.assets.resolve()},dst=/fixture/assets,readonly",
             "--mount", f"type=bind,src={marker},dst=/fixture/marker.json,readonly",
             "--mount", "type=bind,src=/dev/mapper,dst=/dev/mapper,readonly",
             "--mount", f"type=bind,src={args.output.resolve()},dst=/fixture/evidence",
             "--mount", f"type=bind,src={mount},dst=/srv/map-prod", guest_image], timeout=60)
        created = True
        inspection = json.loads(run(["docker", "inspect", guest_name]))[0]
        require(not any(item.get("Source") == "/var/run/docker.sock" for item in inspection["Mounts"]), "runner Docker socket must not be shared")
        require(inspection["HostConfig"].get("CgroupnsMode") == "private" and inspection["HostConfig"].get("PidMode") != "host", "private guest namespaces required")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = subprocess.run(["docker", "exec", guest_name, "test", "-d", "/run/systemd/system"], capture_output=True, timeout=10)
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise ValueError("guest systemd did not become available")
        # No runner environment or token is forwarded into this command.
        run(["docker", "exec", guest_name, "python3", "-B", "/opt/map/deploy/ncp-bootstrap/linux_acceptance.py", "guest"], timeout=1200)
        report["guest"] = json.loads((args.output / "guest.json").read_text())
        require(report["guest"]["status"] == "passed", "guest acceptance did not pass")
        require(report["guest"]["daemon"]["id"] != host_daemon_id, "guest used the runner's Docker daemon")
        report["separate_docker_daemon_verified"] = True
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "blocked_or_failed"
        report["failure"] = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise
    finally:
        guest_evidence = args.output / "guest.json"
        if guest_evidence.is_file():
            report["guest"] = json.loads(guest_evidence.read_text())
        # Exact resources created above only. Never prune the runner daemon or
        # scan, stop, format, or remove another job's devices/containers.
        for enabled, command in ((created, ["docker", "rm", "--force", guest_name]),
                                 (mounted, ["umount", mount]),
                                 (mapped, ["cryptsetup", "close", mapping]),
                                 (loop is not None, ["losetup", "--detach", loop or ""])):
            if enabled:
                try:
                    run(command, timeout=60)
                except Exception:
                    cleanup_errors.append(Path(str(command[0])).name)
        if not cleanup_errors:
            shutil.rmtree(work)
        report["cleanup_errors"] = cleanup_errors
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        if cleanup_errors:
            report["status"] = "blocked_or_failed"
        write_json(args.output / "acceptance.json", report)
        for path in args.output.glob("*.json"):
            path.chmod(0o644)
    require(not cleanup_errors, "fixture cleanup failed; inspect exact runner fixture resources")
    print(json.dumps({"status": report["status"], "independent_vm_verified": False, "ncp_vms_created": 0}))


def guest():
    require(platform.system() == "Linux" and os.geteuid() == 0 and Path("/run/systemd/system").is_dir(), "systemd fixture guest required")
    marker_path = Path("/fixture/marker.json")
    require(marker_path.is_file() and not marker_path.is_symlink(), "fixture marker required")
    marker = json.loads(marker_path.read_text())
    require(marker.get("fixture_only") is True and marker.get("kind") == "privileged-systemd-container-not-vm", "not an approved synthetic guest")
    require(Path("/run/systemd/container").read_text().strip() == "docker", "expected Docker OS container")
    require(not any(name in os.environ for name in ("GH_TOKEN", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS")), "guest must not receive cloud credentials")
    evidence = Path("/fixture/evidence/guest.json")
    result = {"status": "running", "events": [], "role_tested": "prod", "profile": "prod-small",
              "real_host_facts": True, "kernel_shared": True, "ncp_account_approval": False}
    private = Path("/fixture-private")
    private.mkdir(mode=0o700)
    try:
        key = Path("/fixture/assets/docker.asc")
        verify_assets(Path("/fixture/assets"), marker["docker_key_sha256"])
        public_keys = Path("/etc/apt/keyrings")
        public_keys.mkdir(mode=0o755, parents=True, exist_ok=True)
        fixture_key = public_keys / "fixture-reviewed-docker.asc"
        fixture_key.write_bytes(key.read_bytes())
        fixture_key.chmod(0o644)
        require(sha(fixture_key) == marker["docker_key_sha256"], "fixture public-key copy drift")
        # Resolve only the fixture's package versions through Docker's signed
        # official repository. This is not a production version recommendation.
        source = private / "docker.sources"
        source.write_text("Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\nArchitectures: amd64\nSigned-By: " + str(fixture_key) + "\n")
        apt_options = ["-o", "Dir::Etc::sourcelist=" + str(source), "-o", "Dir::Etc::sourceparts=-", "-o", "APT::Get::List-Cleanup=0"]
        run(["apt-get", *apt_options, "update", "-qq"])
        packages = {}
        for name in PACKAGES:
            candidates = run(["apt-cache", *apt_options, "madison", name]).splitlines()
            official = [row.split("|")[1].strip() for row in candidates if "https://download.docker.com/linux/ubuntu" in row]
            require(official, "no signed official Docker package candidate")
            packages[name] = official[0]
        require(packages["docker-ce"] == packages["docker-ce-cli"], "fixture Docker Engine/CLI candidates differ")
        machine = Path("/etc/machine-id").read_text().strip()
        inventory = {role: {"machine_id": uuid.uuid4().hex, "instance_id": "ci-only-" + role,
                            "deploy_account": "map-deploy-" + role, "data_volume_id": "ci-loop-" + role,
                            "secret_scope": "map-" + role, "provider": "gcp" if role == "test" else "ncp"}
                     for role in ("test", "prod", "admin", "learning")}
        inventory["prod"]["machine_id"] = machine
        inventory["admin"].update({key: inventory["test"][key]
                                   for key in ("machine_id", "instance_id", "data_volume_id", "provider")})
        for field, suffix in (("machine_id", "machine"), ("instance_id", "instance"), ("data_volume_id", "volume")):
            inventory["learning"][field] = "reserved-learning-" + suffix
        manifest = {"schema_version": 2, "topology": "gcp-test-admin-ncp-prod",
                    "gcp_cohost_review_sha256": hashlib.sha256(b"synthetic GCP cohost inventory; no observed cloud host").hexdigest(),
                    "profile": "prod-small", "role": "prod", "machine_id": machine,
                    "hostname": platform.node(), "instance_id": inventory["prod"]["instance_id"], "data_uuid": marker["data_uuid"],
                    "data_encryption": "luks2", "inventory": inventory, "docker_packages": packages,
                    "docker_key_sha256": marker["docker_key_sha256"], "ssh_source_cidrs": ["192.0.2.0/24"],
                    "network_review_sha256": hashlib.sha256(b"synthetic CI network review only").hexdigest(),
                    "account_quote_sha256": hashlib.sha256(b"synthetic CI quote; no NCP account approval").hexdigest(),
                    "approval": "approved-empty-host-only", "learning_hold": True}
        enrollment = private / "enrollment.json"
        write_json(enrollment, manifest)
        enrollment.chmod(0o600)
        cli = ["python3", "-B", "/opt/map/scripts/ncp-bootstrap-host.py"]
        def host(command, *options, input=None):
            return json.loads(run([*cli, command, "--manifest", enrollment, *options], input=input))
        result["package_versions"] = packages
        result["guest_measured"] = {"cpu": os.cpu_count(),
                                    "memory_bytes": int(next(row.split()[1] for row in Path("/proc/meminfo").read_text().splitlines() if row.startswith("MemTotal:"))) * 1024,
                                    "root_total_bytes": shutil.disk_usage("/").total,
                                    "data_total_bytes": shutil.disk_usage("/srv/map-prod").total,
                                    "data_free_bytes": shutil.disk_usage("/srv/map-prod").free,
                                    "cgroup_and_proc_capacity_are_shared_kernel_observations": True}
        result["os_package_versions"] = {name: run(["dpkg-query", "-W", "-f=${Version}", name])
                                         for name in ("systemd", "cryptsetup-bin")}
        result["mount_probe"] = json.loads(run(["findmnt", "--json", "--mountpoint", "/srv/map-prod", "--output", "SOURCE,UUID,FSTYPE,OPTIONS"]))
        fixture_source = result["mount_probe"]["filesystems"][0]["source"]
        result["device_probe"] = {"source_exists": Path(fixture_source).exists(), "control_exists": Path("/dev/mapper/control").exists()}
        result["crypt_probe"] = run(["cryptsetup", "status", fixture_source], allowed=(0, 4))
        result["preflight"] = host("preflight")
        pin = host("enrollment-hash")["sha256"]
        result["install"] = host("install", "--docker-key", key, "--approved-enrollment-sha256", pin)
        require(result["install"]["status"] == "host_prepared", "real bootstrap install did not complete")
        result["events"].append("real_cli_install_pass")
        require(host("install", "--docker-key", key, "--approved-enrollment-sha256", pin)["status"] == "host_prepared", "idempotent reinstall failed")
        result["events"].append("real_cli_idempotent_reinstall_pass")
        daemon = json.loads(run(["docker", "info", "--format", "{{json .}}"] ))
        require(daemon["DockerRootDir"] == "/srv/map-prod/docker", "nested daemon data-root mismatch")
        result["daemon"] = {"id": daemon["ID"], "data_root": daemon["DockerRootDir"], "driver": daemon["Driver"]}
        require(not run(["docker", "ps", "-aq"]).strip() and not run(["docker", "volume", "ls", "-q"]).strip(), "new nested daemon not empty")
        secret = os.urandom(48).hex().encode()
        require(host("secret", "--key", "POSTGRES_PASSWORD", input=secret)["value_logged"] is False, "secret operation logged a value")
        host("secret", "--key", "POSTGRES_PASSWORD", input=secret)
        secret_path = Path("/srv/map-prod/secrets/POSTGRES_PASSWORD")
        require(secret_path.stat().st_uid == 0 and stat.S_IMODE(secret_path.stat().st_mode) == 0o600, "secret ownership/mode mismatch")
        for command in (["runuser", "-u", "map-deploy-prod", "--", "test", "-r", secret_path],
                        ["runuser", "-u", "map-deploy-prod", "--", "docker", "info"]):
            probe = subprocess.run([str(x) for x in command], capture_output=True, timeout=30)
            require(probe.returncode != 0, "deploy account acquired secret or daemon access")
        result["events"].append("secret_0600_root_only_and_no_deploy_docker_access_pass")
        caddy = json.loads(run(["python3", "-B", "/opt/map/scripts/install-caddy-artifact.py", "--archive",
                               "/fixture/assets/map-caddy-security-image-20260906.tar", "--report",
                               "/fixture/assets/caddy-security-20260906.json", "--install-compose", private / "caddy.yml"], timeout=180))
        require(caddy["status"] == "installed_and_smoke_verified" and caddy["runtime_checks"]["untrusted_root_rejected"] is True,
                "Caddy import/HTTP/explicit-CA TLS checks incomplete")
        result["caddy"] = caddy
        run(["systemctl", "restart", "containerd.service", "docker.service"])
        host("verify")
        run(["python3", "-B", "/opt/map/scripts/install-caddy-artifact.py", "--verify-compose", private / "caddy.yml"])
        result["events"].append("systemctl_restart_preserves_mount_guard_daemon_and_caddy_identity")
        require(secret_path.read_bytes() == secret, "fixture secret changed across restart")
        # Receiver is never executed. A stopped fixture workload still blocks
        # rollback, proving it cannot discard a serving container accidentally.
        image = caddy["image_id"]
        container_id = run(["docker", "create", "--network", "none", "--tmpfs", "/data", "--tmpfs", "/config",
                            "--entrypoint", "caddy", image, "version"]).strip()
        blocked = subprocess.run([*cli, "rollback", "--manifest", str(enrollment)], capture_output=True, timeout=30)
        require(blocked.returncode != 0, "rollback accepted an existing fixture workload")
        run(["docker", "rm", container_id])
        result["rollback"] = host("rollback")
        require(result["rollback"]["data_deleted"] == 0 and secret_path.read_bytes() == secret, "rollback lost fixture data")
        for unit in ("docker.service", "docker.socket", "containerd.service"):
            require(run(["systemctl", "is-enabled", unit], allowed=(0, 1)).strip() == "masked", "rollback left daemon activation enabled")
        require(host("rollback")["status"] == "rolled_back", "rollback not idempotent")
        result["events"].append("workload_blocks_rollback_then_quiescent_rollback_preserves_data_and_masks_daemon")
        result["status"] = "passed"
    except Exception as error:
        result["status"] = "blocked_or_failed"
        result["failure"] = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise
    finally:
        write_json(evidence, result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download_parser = commands.add_parser("download")
    download_parser.add_argument("--assets", type=Path, required=True)
    download_parser.add_argument("--docker-key-sha256", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--ubuntu-base", required=True)
    execute.add_argument("--docker-key-sha256", required=True)
    execute.add_argument("--assets", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    commands.add_parser("guest")
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            download(args)
        elif args.command == "run":
            run_fixture(args)
        else:
            guest()
        return 0
    except Exception as error:
        print(json.dumps({"status": "blocked_or_failed", "reason": str(error) if isinstance(error, ValueError) else type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
