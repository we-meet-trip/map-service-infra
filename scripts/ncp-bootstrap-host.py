#!/usr/bin/env python3
"""Prepare a new Ubuntu host. Never provision a VM, start apps, format or erase data.

plan is offline; preflight is read-only; install requires a pinned enrollment.
The filesystem fixture exercises the same transaction with an injected OS backend.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import importlib.util
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

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "deploy/ncp-bootstrap/profiles.json"
ROLES = {"prod", "admin", "learning"}
PACKAGES = {"docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"}
HEX = re.compile(r"[a-f0-9]{64}")
SAFE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,100}")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def regular(path, private=False):
    require(path.is_file() and not path.is_symlink(), "regular file required")
    require(path.stat().st_nlink == 1, "hardlinks forbidden")
    if private:
        require(stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == os.geteuid(), "private file must be owned and 0600")
    require(path.stat().st_size <= 1024 * 1024, "input too large")
    return path.read_bytes()


def safe_path(root, relative):
    require(not Path(relative).is_absolute() and ".." not in Path(relative).parts, "unsafe path")
    p = root / relative
    for ancestor in (p, *p.parents):
        if ancestor == root.parent:
            break
        require(not ancestor.is_symlink(), "symlink path forbidden")
    return p


def write_once(path, content, mode=0o600):
    if path.exists() or path.is_symlink():
        require(regular(path) == content and stat.S_IMODE(path.stat().st_mode) == mode, "existing configuration differs")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".map-write-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # link() publishes without replacing an existing name; temporary bytes are complete.
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        os.unlink(temporary)
    return True


def atomic_json(path, value):
    require(not path.is_symlink(), "state symlink forbidden")
    fd, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate(manifest):
    required = {"schema_version", "profile", "role", "machine_id", "hostname", "instance_id", "data_uuid", "data_encryption", "inventory", "docker_packages", "docker_key_sha256", "ssh_source_cidrs", "network_review_sha256", "account_quote_sha256", "approval", "learning_hold"}
    require(set(manifest) == required and type(manifest["schema_version"]) is int and manifest["schema_version"] == 1, "invalid enrollment schema")
    require(manifest["role"] in ROLES, "existing GCP test host is observe-only")
    profiles = json.loads(PROFILES.read_text())["profiles"]
    require(manifest["profile"] in profiles, "unknown profile")
    p = profiles[manifest["profile"]]
    require(p["role"] == manifest["role"], "profile role mismatch")
    require(manifest["learning_hold"] is True, "learning HOLD must remain enabled")
    require(manifest["approval"] in {"pending", "approved-empty-host-only"}, "invalid approval")
    for name in ("machine_id", "hostname", "instance_id", "data_uuid"):
        require(isinstance(manifest[name], str) and SAFE.fullmatch(manifest[name]), "invalid host identity")
    require(re.fullmatch(r"[a-f0-9]{32}", manifest["machine_id"]), "machine-id must be exact")
    require(re.fullmatch(r"[a-f0-9-]{36}", manifest["data_uuid"]), "filesystem UUID required")
    require(manifest["data_encryption"] == "luks2", "g3 requires separately prepared LUKS2 data volume")
    inv = manifest["inventory"]
    require(isinstance(inv, dict) and set(inv) == {"test", "prod", "admin", "learning"}, "four-role inventory required")
    for field in ("machine_id", "instance_id", "deploy_account", "data_volume_id", "secret_scope"):
        values = []
        for role, host in inv.items():
            require(set(host) == {"machine_id", "instance_id", "deploy_account", "data_volume_id", "secret_scope", "provider"}, "invalid inventory entry")
            require(all(isinstance(v, str) and SAFE.fullmatch(v) for v in host.values()), "invalid inventory value")
            values.append(host[field])
        require(len(set(values)) == 4, "cross-role identity, account, volume or secret scope reuse")
    for role, entry in inv.items():
        if entry["machine_id"].startswith("reserved-"):
            require(role not in {"test", manifest["role"]}, "current/test host cannot be a reservation")
            for field, suffix in (("machine_id", "machine"), ("instance_id", "instance"), ("data_volume_id", "volume")):
                require(entry[field] == "reserved-" + role + "-" + suffix, "incomplete role reservation")
        else:
            require(re.fullmatch(r"[a-f0-9]{32}", entry["machine_id"]), "observed peer machine-id or explicit reservation required")
    require(inv["test"]["provider"] == "gcp", "existing GCP must remain test")
    require(inv[manifest["role"]]["provider"] == "ncp", "new host must be NCP")
    require(inv[manifest["role"]]["machine_id"] == manifest["machine_id"] and inv[manifest["role"]]["instance_id"] == manifest["instance_id"], "inventory identity mismatch")
    require(inv[manifest["role"]]["deploy_account"] == "map-deploy-" + manifest["role"], "role account mismatch")
    require(inv[manifest["role"]]["secret_scope"] == "map-" + manifest["role"], "role secret prefix mismatch")
    packages = manifest["docker_packages"]
    require(isinstance(packages, dict) and set(packages) == PACKAGES, "pin all five Docker packages")
    require(all(isinstance(v, str) and re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]{2,100}", v) for v in packages.values()), "exact package versions required")
    require(packages["docker-ce"] == packages["docker-ce-cli"], "Docker CLI/Engine versions differ")
    for field in ("docker_key_sha256", "network_review_sha256", "account_quote_sha256"):
        require(isinstance(manifest[field], str) and HEX.fullmatch(manifest[field]), "missing reviewed input hash")
    import ipaddress
    require(isinstance(manifest["ssh_source_cidrs"], list) and 0 < len(manifest["ssh_source_cidrs"]) <= 8, "operator source CIDRs required")
    for cidr in manifest["ssh_source_cidrs"]:
        network = ipaddress.ip_network(cidr, strict=True)
        require(network.prefixlen >= (24 if network.version == 4 else 64), "broad SSH ingress forbidden")
    return p


def plan(manifest):
    p = validate(manifest)
    role = manifest["role"]
    return {"role": role, "profile": manifest["profile"], "cpu": p["cpu"], "ram_gb": p["ram_gb"],
            "root_gb": p["root_gb"], "data_gb": p["data_gb"], "data_mount": "/srv/map-" + role,
            "os": "Ubuntu 24.04 amd64", "deploy_account": "map-deploy-" + role,
            "account_shell": "/usr/sbin/nologin", "docker_socket_group": "root",
            "paid_creation": False, "serving_activation": False, "formats_disks": False,
            "approval": manifest["approval"], "learning_hold": True,
            "next": "preflight on a separately approved empty host; no cloud API is called"}


class Linux:
    """OS effects are injectable only from tests, never via a fixture CLI flag."""
    def run(self, argv, *, allowed=(0,), input=None):
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "DEBIAN_FRONTEND": "noninteractive", "DOCKER_HOST": "unix:///var/run/docker.sock"}
        result = subprocess.run(argv, input=input, capture_output=True, env=env, timeout=600)
        require(result.returncode in allowed, "host command failed: " + Path(argv[0]).name)
        return result.stdout.decode()

    def facts(self, mount):
        require(platform.system() == "Linux" and os.geteuid() == 0, "root on approved Linux host required")
        os_release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
        row = json.loads(self.run(["findmnt", "--json", "--mountpoint", mount, "--output", "SOURCE,UUID,FSTYPE,OPTIONS"]))["filesystems"][0]
        source = row["source"]
        require(source.startswith("/dev/mapper/"), "data volume must be LUKS mapping")
        crypt = self.run(["cryptsetup", "status", source])
        mem = int(next(x.split()[1] for x in Path("/proc/meminfo").read_text().splitlines() if x.startswith("MemTotal:"))) * 1024
        return {"os": os_release.get("ID", "").strip('"'), "version": os_release.get("VERSION_ID", "").strip('"'),
                "arch": platform.machine(), "machine_id": Path("/etc/machine-id").read_text().strip(),
                "hostname": platform.node(), "cpu": os.cpu_count(), "memory_bytes": mem,
                "root_bytes": shutil.disk_usage("/").total, "data_bytes": shutil.disk_usage(mount).total,
                "data_free": shutil.disk_usage(mount).free, "mount_uuid": row["uuid"], "mount_fstype": row["fstype"],
                "luks2": bool(re.search(r"type:\s+LUKS2", crypt)), "mount_rw": "rw" in row["options"].split(","),
                "systemd": Path("/run/systemd/system").is_dir()}

    def exists_package(self, name):
        return self.run(["dpkg-query", "-W", "-f=${db:Status-Status}", name], allowed=(0, 1)) == "installed"

    def empty(self):
        if shutil.which("docker"):
            # A stopped unknown daemon is also rejected: its disk data is not disposable.
            raise ValueError("unmanaged Docker installation exists")
        for name in ("docker.io", "podman-docker", "containerd", "runc", *PACKAGES):
            require(not self.exists_package(name), "conflicting or unmanaged container package")

    def account(self, account, create=False):
        import pwd, grp
        try:
            row = pwd.getpwnam(account)
            require(row.pw_dir == "/var/lib/" + account and row.pw_shell == "/usr/sbin/nologin", "unmanaged deploy account")
            groups = os.getgrouplist(account, row.pw_gid)
            require(all(grp.getgrgid(g).gr_name == account for g in groups), "deploy account has extra groups")
            return
        except KeyError:
            require(create, "deploy account missing")
        self.run(["useradd", "--system", "--user-group", "--create-home", "--home-dir", "/var/lib/" + account, "--shell", "/usr/sbin/nologin", account])

    def packages(self, manifest, key):
        self.run(["apt-get", "update", "-qq"])
        self.run(["apt-get", "install", "-y", "--no-install-recommends", *[k + "=" + v for k, v in sorted(manifest["docker_packages"].items())]])

    def verify(self, manifest):
        for name, expected in manifest["docker_packages"].items():
            actual = self.run(["dpkg-query", "-W", "-f=${Version}", name])
            require(actual == expected, "installed package version drift")
        info = json.loads(self.run(["docker", "--host", "unix:///var/run/docker.sock", "info", "--format", "{{json .}}"] ))
        require(info["DockerRootDir"] == "/srv/map-" + manifest["role"] + "/docker", "daemon data root drift")
        require(info.get("OSType") == "linux" and info.get("Architecture") in {"x86_64", "amd64"}, "daemon platform drift")
        socket = Path("/var/run/docker.sock").stat()
        require(socket.st_uid == 0 and socket.st_gid == 0 and stat.S_IMODE(socket.st_mode) == 0o660, "socket permissions drift")
        self.account("map-deploy-" + manifest["role"])

    def no_workloads(self):
        require(not self.run(["docker", "--host", "unix:///var/run/docker.sock", "ps", "-aq"]).strip(), "rollback blocked by containers")
        require(not self.run(["docker", "--host", "unix:///var/run/docker.sock", "volume", "ls", "-q"]).strip(), "rollback blocked by named data volumes")


def managed_files(manifest):
    role = manifest["role"]
    mount = "/srv/map-" + role
    daemon = {"data-root": mount + "/docker", "group": "root", "live-restore": True,
              "log-driver": "local", "log-opts": {"max-size": "10m", "max-file": "3"},
              "features": {"containerd-snapshotter": False}}
    service = "[Unit]\nRequiresMountsFor=" + mount + "\nAfter=local-fs.target\n[Service]\nExecStartPre=/usr/local/lib/map-bootstrap/verify-mount\n"
    # UUID and crypt mapping are verified on every Docker/containerd restart.
    check = ("#!/usr/bin/python3\nimport json,subprocess\n"
             "r=json.loads(subprocess.check_output(['findmnt','--json','--mountpoint'," + repr(mount) + ", '--output','SOURCE,UUID,FSTYPE,OPTIONS']))['filesystems'][0]\n"
             "assert r['uuid']==" + repr(manifest["data_uuid"]) + " and r['fstype']=='ext4' and 'rw' in r['options'].split(',')\n"
             "assert r['source'].startswith('/dev/mapper/')\n"
             "s=subprocess.check_output(['cryptsetup','status',r['source']],text=True)\n"
             "assert 'LUKS2' in s\n")
    return {
        "etc/docker/daemon.json": (json.dumps(daemon, sort_keys=True, indent=2).encode() + b"\n", 0o600),
        "etc/systemd/system/docker.service.d/20-map-data.conf": (service.encode(), 0o644),
        "etc/systemd/system/containerd.service.d/20-map-data.conf": ((service + "ExecStart=\nExecStart=/usr/bin/containerd --config /etc/map-bootstrap/containerd.toml\n").encode(), 0o644),
        "etc/systemd/system/docker.socket.d/20-map-root.conf": (b"[Socket]\nSocketUser=root\nSocketGroup=root\nSocketMode=0660\n", 0o644),
        "usr/local/lib/map-bootstrap/verify-mount": (check.encode(), 0o755),
        "etc/apt/sources.list.d/map-docker.sources": (b"Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/map-docker.asc\n", 0o644),
    }


def preflight(manifest, root=Path("/"), backend=None):
    backend = backend or Linux()
    p = validate(manifest)
    role = manifest["role"]
    mount = "/srv/map-" + role
    facts = backend.facts(mount)
    require(facts["os"] == "ubuntu" and facts["version"] == "24.04" and facts["arch"] in {"x86_64", "amd64"}, "Ubuntu 24.04 amd64 required")
    require(facts["systemd"] and facts["machine_id"] == manifest["machine_id"] and facts["hostname"] == manifest["hostname"], "host identity/systemd mismatch")
    require(facts["cpu"] >= p["cpu"] and facts["memory_bytes"] >= p["ram_gb"] * 10**9 * .9, "CPU/RAM below selected profile")
    require(facts["root_bytes"] >= p["root_gb"] * 10**9 * .9 and facts["data_bytes"] >= p["data_gb"] * 10**9 * .9, "disk capacity below profile")
    require(facts["mount_uuid"] == manifest["data_uuid"] and facts["mount_fstype"] == "ext4" and facts["mount_rw"] and facts["luks2"], "data UUID/filesystem/encryption mismatch")
    data = safe_path(root, mount.lstrip("/"))
    if root == Path("/"):
        for parent in (data, data.parent, Path("/etc"), Path("/var/lib")):
            require(parent.stat().st_uid == 0 and parent.stat().st_mode & 0o022 == 0, "root-owned non-writable host boundary required")
    stamp = safe_path(root, "var/lib/map-bootstrap/enrollment.json")
    if stamp.exists():
        require(json.loads(regular(stamp)) == manifest, "host already enrolled with different contract")
    else:
        backend.empty()
        for path in ("var/lib/docker", "var/lib/containerd", "etc/docker", "etc/containerd"):
            old = safe_path(root, path)
            require(not old.exists() or (old.is_dir() and not any(old.iterdir())), "unmanaged container data/config exists")
        require(data.is_dir() and set(x.name for x in data.iterdir()) <= {"lost+found"}, "new data mount is not empty")
        require(facts["data_free"] >= p["data_gb"] * 10**9 * .85, "new volume lacks free capacity")
        for path in managed_files(manifest):
            require(not safe_path(root, path).exists(), "unmanaged bootstrap file exists")
        for unit in ("docker.service", "docker.socket", "containerd.service"):
            dropins = safe_path(root, "etc/systemd/system/" + unit + ".d")
            require(not dropins.exists() or not any(dropins.iterdir()), "unmanaged systemd dropin exists")
        require(not safe_path(root, "etc/map-bootstrap").exists(), "unmanaged bootstrap configuration")
    return {"status": "preflight_pass", "role": role, "machine_id_sha256": sha(facts["machine_id"].encode()), "profile": manifest["profile"], "serving_changes": 0}


def locked(root):
    directory = safe_path(root, "var/lib/map-bootstrap")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(stat.S_IMODE(directory.stat().st_mode) == 0o700 and directory.stat().st_uid == os.geteuid(), "state directory permissions")
    path = safe_path(root, "var/lib/map-bootstrap/lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600, "untrusted bootstrap lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def install(manifest, key_bytes, expected_sha, root=Path("/"), backend=None):
    backend = backend or Linux()
    validate(manifest)
    require(manifest["approval"] == "approved-empty-host-only", "user empty-host approval is pending")
    require(sha(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()) == expected_sha, "enrollment approval pin mismatch")
    require(sha(key_bytes) == manifest["docker_key_sha256"] and b"BEGIN PGP PUBLIC KEY BLOCK" in key_bytes, "Docker official key checksum mismatch")
    preflight(manifest, root, backend)
    fd = locked(root)
    try:
        preflight(manifest, root, backend)
        state_dir = safe_path(root, "var/lib/map-bootstrap")
        state_file = state_dir / "transaction.json"
        if state_file.exists():
            state = json.loads(regular(state_file, True))
            require(state["enrollment_sha256"] == expected_sha, "transaction enrollment mismatch")
            if state["status"] == "installed":
                return verify(manifest, root, backend)
            require(state["status"] == "installing", "rolled back host requires replacement/review")
        else:
            require(not safe_path(root, "usr/sbin/policy-rc.d").exists(), "existing package service policy must not be overwritten")
            for unit in ("docker.service", "docker.socket", "containerd.service"):
                require(not safe_path(root, "etc/systemd/system/" + unit).exists() and not safe_path(root, "etc/systemd/system/" + unit).is_symlink(), "existing systemd override/mask")
            write_once(state_dir / "enrollment.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
            state = {"status": "installing", "enrollment_sha256": expected_sha, "files": {}, "role": manifest["role"], "packages_retained_on_rollback": True}
            atomic_json(state_file, state)
        files = managed_files(manifest)
        files["etc/apt/keyrings/map-docker.asc"] = (key_bytes, 0o644)
        # Persist ownership before each write, so a crash leaves a resumable transaction.
        for relative, (content, mode) in files.items():
            state["files"][relative] = {"sha256": sha(content), "mode": mode}
            atomic_json(state_file, state)
            write_once(safe_path(root, relative), content, mode)
        data = safe_path(root, "srv/map-" + manifest["role"])
        for name in ("docker", "containerd", "staging", "backups", "restore", "secrets"):
            path = safe_path(data, name)
            path.mkdir(mode=0o700, exist_ok=True)
        backend.account("map-deploy-" + manifest["role"], create=True)
        policy = safe_path(root, "usr/sbin/policy-rc.d")
        policy_bytes = b"#!/bin/sh\nexit 101\n"
        write_once(policy, policy_bytes, 0o755)
        try:
            backend.run(["systemctl", "mask", "docker.service", "docker.socket", "containerd.service"])
            backend.packages(manifest, key_bytes)
            containerd = backend.run(["containerd", "config", "default"])
            require(re.search(r"(?m)^root = [^\n]+$", containerd), "containerd root field missing")
            containerd = re.sub(r"(?m)^root = [^\n]+$", 'root = "/srv/map-' + manifest["role"] + '/containerd"', containerd)
            path = "etc/map-bootstrap/containerd.toml"
            content = containerd.encode()
            existing = safe_path(root, path)
            state["files"][path] = {"sha256": sha(content), "mode": 0o600}
            atomic_json(state_file, state)
            write_once(existing, content)
        finally:
            if policy.exists() and regular(policy) == policy_bytes:
                policy.unlink()
        backend.run(["systemctl", "daemon-reload"])
        backend.run(["systemctl", "unmask", "docker.service", "docker.socket", "containerd.service"])
        backend.run(["systemctl", "enable", "--now", "containerd.service", "docker.service"])
        backend.verify(manifest)
        state["status"] = "installed"
        atomic_json(state_file, state)
        return verify(manifest, root, backend)
    finally:
        os.close(fd)


def verify(manifest, root=Path("/"), backend=None):
    backend = backend or Linux()
    preflight(manifest, root, backend)
    state = json.loads(regular(safe_path(root, "var/lib/map-bootstrap/transaction.json"), True))
    require(state["status"] == "installed", "bootstrap not installed")
    for relative, record in state["files"].items():
        path = safe_path(root, relative)
        require(sha(regular(path)) == record["sha256"] and stat.S_IMODE(path.stat().st_mode) == record["mode"], "managed configuration drift")
    backend.verify(manifest)
    return {"status": "host_prepared", "role": manifest["role"], "managed_files": len(state["files"]), "idempotent": True, "serving_changes": 0, "receiver_enrolled": False}


def inject_secret(manifest, key, content, root=Path("/")):
    validate(manifest)
    require(re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", key), "invalid secret key")
    allowed = json.loads(PROFILES.read_text())["secret_keys"][manifest["role"]]
    require(key in allowed, "secret is not allowed for this role")
    require(0 < len(content) <= 65536 and b"\x00" not in content, "invalid secret content")
    state = json.loads(regular(safe_path(root, "var/lib/map-bootstrap/enrollment.json"), True))
    require(state == manifest, "secret role/host mismatch")
    fd = locked(root)
    try:
        state = json.loads(regular(safe_path(root, "var/lib/map-bootstrap/transaction.json"), True))
        require(state["status"] == "installed", "host is not prepared")
        directory = safe_path(root, "srv/map-" + manifest["role"] + "/secrets")
        require(directory.is_dir() and stat.S_IMODE(directory.stat().st_mode) == 0o700, "private secret directory required")
        write_once(safe_path(directory, key), content)
        return {"status": "secret_injected", "role": manifest["role"], "key": key, "value_logged": False}
    finally:
        os.close(fd)


def rollback(manifest, root=Path("/"), backend=None):
    backend = backend or Linux()
    preflight(manifest, root, backend)
    fd = locked(root)
    try:
        state_file = safe_path(root, "var/lib/map-bootstrap/transaction.json")
        state = json.loads(regular(state_file, True))
        require(state["status"] in {"installed", "rolling_back", "rolled_back"}, "incomplete install must resume before rollback")
        # Check everything before the first stop; never replace administrator changes.
        for relative, record in state["files"].items():
            require(sha(regular(safe_path(root, relative))) == record["sha256"], "rollback refuses configuration drift")
        if state["status"] == "rolled_back":
            return {"status": "rolled_back", "data_deleted": 0, "idempotent": True}
        if state["status"] == "installed":
            backend.no_workloads()
            state["status"] = "rolling_back"
            atomic_json(state_file, state)
        backend.run(["systemctl", "disable", "--now", "docker.service", "docker.socket", "containerd.service"])
        backend.run(["systemctl", "mask", "docker.service", "docker.socket", "containerd.service"])
        backup_dir = safe_path(root, "var/lib/map-bootstrap/rollback-config")
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        for relative in state["files"]:
            path = safe_path(root, relative)
            destination = safe_path(backup_dir, relative)
            write_once(destination, regular(path))
            # Keep data-root, mount and socket guards in place, even if an operator later unmasks.
        backend.run(["systemctl", "daemon-reload"])
        state["status"] = "rolled_back"
        atomic_json(state_file, state)
        return {"status": "rolled_back", "data_deleted": 0, "packages_deleted": 0, "accounts_deleted": 0, "secrets_preserved": True, "serving_changes": 0}
    finally:
        os.close(fd)


def artifact_cache(manifest, bundle, approval, name, root=Path("/"), backend=None):
    """Stage + cache reviewed images on an enrolled host; no receiver or app runs."""
    backend = backend or Linux()
    verify(manifest, root, backend)
    require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9-]{1,63}", name), "release name required")
    spec = importlib.util.spec_from_file_location("host_artifacts", ROOT / "scripts/ncp-bootstrap-artifacts.py")
    artifacts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(artifacts)
    fd = locked(root)
    try:
        destination = safe_path(root, "srv/map-" + manifest["role"] + "/staging/" + name)
        artifacts.stage(bundle, destination, manifest["role"], approval)
        # Consume only the private staged bytes whose hashes stage() verified.
        # Re-reading the mutable source after verification would break the pin.
        contract = artifacts.read_json(destination / "contract.json")
        if contract["role_contract"] is not None:
            role = artifacts.read_json(destination / contract["role_contract"]["manifest"])
            require(role["host_identity"] == manifest["hostname"] and role["deploy_account"] == "map-deploy-" + manifest["role"], "artifact target host/account mismatch")
        install_dir = safe_path(root, "srv/map-" + manifest["role"] + "/installations/" + name)
        install_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for service, entry in contract["images"].items():
            if service == "edge" and contract["caddy"] is not None:
                continue
            backend.run(["docker", "--host", "unix:///var/run/docker.sock", "image", "pull", "--platform", "linux/amd64", entry["image"]])
            actual = json.loads(backend.run(["docker", "--host", "unix:///var/run/docker.sock", "image", "inspect", entry["image"]]))[0]
            require(actual["Os"] == "linux" and actual["Architecture"] == "amd64" and entry["image"] in actual.get("RepoDigests", []), "cached image platform/digest mismatch")
        if contract["caddy"] is not None:
            override = install_dir / "caddy-verified.yml"
            if override.exists():
                backend.run([sys.executable, str(ROOT / "scripts/install-caddy-artifact.py"), "--verify-compose", str(override)])
            else:
                backend.run([sys.executable, str(ROOT / "scripts/install-caddy-artifact.py"), "--archive", str(destination / contract["caddy"]["archive"]), "--report", str(destination / contract["caddy"]["report"]), "--install-compose", str(override)])
        receipt = {"status": "images_cached", "role": manifest["role"], "security_approval_sha256": approval, "contract_sha256": artifacts.sha256(destination / "contract.json"), "images": contract["images"], "receiver_executed": False, "serving_changes": 0}
        atomic_json(install_dir / "cache-receipt.json", receipt)
        return receipt
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "preflight", "install", "verify", "secret", "rollback", "cache", "enrollment-hash"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--profile", help="offline capacity plan without host enrollment")
    parser.add_argument("--approved-enrollment-sha256")
    parser.add_argument("--docker-key", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--security-approval-sha256")
    parser.add_argument("--release-name")
    args = parser.parse_args()
    try:
        if args.command == "plan" and args.profile and not args.manifest:
            profiles = json.loads(PROFILES.read_text())["profiles"]
            require(args.profile in profiles, "unknown profile")
            print(json.dumps({"status": "offline_plan", "profile": args.profile, **profiles[args.profile],
                              "os": "Ubuntu 24.04 amd64", "approval": "pending", "paid_creation": False,
                              "required_inputs": ["exact NCP quote/image/zone", "four-role inventory", "machine-id/hostname/instance-id", "new LUKS2 ext4 data UUID", "five exact Docker package versions and official key SHA256", "network review hash and operator CIDRs", "user empty-host approval", "separate reviewed receiver and image artifact contract"]}, sort_keys=True))
            return 0
        require(args.manifest is not None and not args.profile, "manifest required; profile is offline plan only")
        manifest = json.loads(regular(args.manifest))
        validate(manifest)
        if args.command == "plan": result = plan(manifest)
        elif args.command == "enrollment-hash": result = {"sha256": sha(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())}
        elif args.command == "preflight": result = preflight(manifest)
        elif args.command == "verify": result = verify(manifest)
        elif args.command == "install":
            require(args.docker_key and args.approved_enrollment_sha256, "pinned enrollment and Docker key required")
            result = install(manifest, regular(args.docker_key), args.approved_enrollment_sha256)
        elif args.command == "rollback": result = rollback(manifest)
        elif args.command == "cache":
            require(args.bundle and args.security_approval_sha256 and args.release_name, "bundle, reviewed approval pin and release name required")
            result = artifact_cache(manifest, args.bundle, args.security_approval_sha256, args.release_name)
        else:
            require(os.geteuid() == 0 and not sys.stdin.isatty(), "secret bytes must come from private stdin as root")
            preflight(manifest)
            result = inject_secret(manifest, args.key or "", sys.stdin.buffer.read(65537))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        # Raw command output, credentials and arbitrary manifest values never enter logs.
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__, "reason": str(error) if isinstance(error, ValueError) else "host operation failed; inspect private host state"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
