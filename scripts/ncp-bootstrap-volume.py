#!/usr/bin/env python3
"""Enroll one verified all-zero additional disk as LUKS2/ext4. No cloud APIs.

Only prepare --format-empty can create encryption/filesystem metadata. Interrupted
format stages are quarantined, never automatically formatted again. inspect is
read-only. restore-mount requires the original journal, UUIDs and external key.
No key is copied into the host, crypttab or fstab; reboot unlock remains an external
key-recovery gate. Real block operations require root Linux; fixtures inject the OS.
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
import stat
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ncp_host_volume_contract", ROOT / "scripts/ncp-bootstrap-host.py")
host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host)
require = host.require
safe_path = host.safe_path
READY = {"ready", "filesystem_ready", "closed", "closing"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(enrollment, contract):
    profile = host.validate(enrollment)
    required = {"schema_version", "role", "root_enrollment_sha256", "device", "serial", "size_bytes", "major_minor", "luks_uuid", "approval"}
    require(isinstance(contract, dict) and set(contract) == required and contract["schema_version"] == 1, "invalid volume contract")
    require(contract["role"] == enrollment["role"], "volume role mismatch")
    require(contract["root_enrollment_sha256"] == digest(enrollment), "root enrollment pin mismatch")
    require(isinstance(contract["device"], str) and re.fullmatch(r"/dev/[A-Za-z0-9_-]{2,64}", contract["device"]), "direct block-device path required")
    require(isinstance(contract["serial"], str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", contract["serial"]), "exact device serial required")
    require(type(contract["size_bytes"]) is int and contract["size_bytes"] >= profile["data_gb"] * 10**9, "exact selected disk size required")
    require(isinstance(contract["major_minor"], str) and re.fullmatch(r"[0-9]+:[0-9]+", contract["major_minor"]), "exact device number required")
    require(str(uuid.UUID(contract["luks_uuid"])) == contract["luks_uuid"], "canonical LUKS UUID required")
    require(str(uuid.UUID(enrollment["data_uuid"])) == enrollment["data_uuid"], "canonical filesystem UUID required")
    require(contract["luks_uuid"] != enrollment["data_uuid"], "encryption and filesystem UUIDs must differ")
    require(contract["approval"] in {"pending", "approved-empty-volume-only"}, "invalid volume approval")
    return profile


def names(enrollment):
    return "map-" + enrollment["role"] + "-data", "/srv/map-" + enrollment["role"]


def key_bytes(path):
    # Do not emit bytes, a hash of a passphrase, or its private path into results.
    for p in (path, *path.parents):
        require(not p.is_symlink(), "key path symlink forbidden")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and stat.S_IMODE(st.st_mode) == 0o600 and st.st_uid == os.geteuid(), "private owned 0600 key file required")
        require(32 <= st.st_size <= 4096, "key file must contain 32..4096 private bytes")
        data = os.read(fd, 4097)
        require(len(data) == st.st_size, "key file changed while reading")
        return data
    finally:
        os.close(fd)


def scan_stream(stream, size, chunk_size=4 * 1024 * 1024):
    """Read every byte, including the last sector. Sparse/sample checks are invalid."""
    count = 0
    zero = b"\0" * chunk_size
    while count < size:
        chunk = stream.read(min(chunk_size, size - count))
        require(bool(chunk), "device ended before approved size")
        require(chunk == zero[:len(chunk)], "disk contains data; never format")
        count += len(chunk)
    require(stream.read(1) == b"", "device exceeds approved size")
    return count


def flatten(rows):
    result = []
    for row in rows:
        result.append(row)
        result.extend(flatten(row.get("children", [])))
    return result


class Linux:
    def run(self, argv, *, input=None, allowed=(0,)):
        result = subprocess.run(argv, input=input, capture_output=True, timeout=900,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
        require(result.returncode in allowed, "volume command failed: " + Path(argv[0]).name)
        return result.returncode, result.stdout.decode()

    def identity(self, enrollment):
        require(platform.system() == "Linux" and os.geteuid() == 0, "root Linux host required")
        require(Path("/etc/machine-id").read_text().strip() == enrollment["machine_id"] and platform.node() == enrollment["hostname"], "host enrollment identity mismatch")
        os_release = dict(x.split("=", 1) for x in Path("/etc/os-release").read_text().splitlines() if "=" in x)
        require(os_release.get("ID", "").strip('"') == "ubuntu" and os_release.get("VERSION_ID", "").strip('"') == "24.04" and platform.machine() in {"x86_64", "amd64"}, "Ubuntu24.04 amd64 required")
        require(Path("/run/systemd/system").is_dir(), "systemd host required")

    def probe(self, contract):
        device = Path(contract["device"])
        require(not device.is_symlink(), "device symlink forbidden")
        st = device.stat()
        require(stat.S_ISBLK(st.st_mode), "real block device required")
        number = str(os.major(st.st_rdev)) + ":" + str(os.minor(st.st_rdev))
        _, data = self.run(["lsblk", "--json", "--bytes", "--paths", "--output", "NAME,TYPE,SIZE,SERIAL,RO,PKNAME,MAJ:MIN,MOUNTPOINTS", str(device)])
        rows = json.loads(data)["blockdevices"]
        require(len(rows) == 1, "ambiguous block device")
        row = rows[0]
        _, root_json = self.run(["findmnt", "--json", "--mountpoint", "/", "--output", "MAJ:MIN"])
        root_number = json.loads(root_json)["filesystems"][0]["maj:min"]
        _, ancestors = self.run(["lsblk", "--json", "--inverse", "--paths", "--output", "NAME,MAJ:MIN", "/dev/block/" + root_number])
        root_numbers = {x["maj:min"] for x in flatten(json.loads(ancestors)["blockdevices"])}
        _, mounted = self.run(["findmnt", "--json", "--output", "SOURCE,TARGET,MAJ:MIN,FSTYPE,UUID,OPTIONS"])
        holder_dir = Path("/sys/dev/block") / number / "holders"
        require(holder_dir.is_dir(), "kernel device holders unavailable")
        return {"serial": row.get("serial"), "size_bytes": int(row["size"]), "major_minor": number,
                "reported_major_minor": row["maj:min"], "type": row["type"], "readonly": bool(row["ro"]),
                "parent": row.get("pkname"), "children": row.get("children", []), "holders": sorted(x.name for x in holder_dir.iterdir()),
                "root_numbers": root_numbers, "mounts": flatten(json.loads(mounted)["filesystems"])}

    def signatures(self, device):
        _, data = self.run(["wipefs", "--no-act", "--json", "--output", "TYPE,UUID,OFFSET", device])
        require(not json.loads(data).get("signatures"), "disk signature exists; never format")
        code, text = self.run(["blkid", "--probe", "--output", "export", device], allowed=(0, 2))
        require(code == 2 and not text.strip(), "filesystem/partition signature exists; never format")

    def scan_all(self, contract):
        fd = os.open(contract["device"], os.O_RDONLY | os.O_EXCL | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            require(stat.S_ISBLK(st.st_mode) and str(os.major(st.st_rdev)) + ":" + str(os.minor(st.st_rdev)) == contract["major_minor"], "device changed before full scan")
            with os.fdopen(fd, "rb", buffering=0, closefd=False) as stream:
                return scan_stream(stream, contract["size_bytes"])
        finally:
            os.close(fd)

    def luks(self, contract, key):
        code, _ = self.run(["cryptsetup", "isLuks", "--type", "luks2", contract["device"]], allowed=(0, 1))
        require(code == 0, "owned LUKS2 header missing")
        _, actual = self.run(["cryptsetup", "luksUUID", contract["device"]])
        require(actual.strip() == contract["luks_uuid"], "LUKS UUID mismatch")
        self.run(["cryptsetup", "open", "--type", "luks2", "--test-passphrase", "--key-file", "-", contract["device"]], input=key)

    def mapper(self, enrollment, contract):
        mapper, _ = names(enrollment)
        p = Path("/dev/mapper") / mapper
        if not p.exists():
            return None
        _, status = self.run(["cryptsetup", "status", mapper])
        require(re.search(r"type:\s+LUKS2", status), "mapper is not LUKS2")
        device = re.search(r"(?m)^\s*device:\s+(\S+)\s*$", status)
        require(device is not None, "mapper backing device unavailable")
        require(os.stat(device.group(1)).st_rdev == os.stat(contract["device"]).st_rdev, "mapper belongs to another device")
        st = p.stat()
        require(stat.S_ISBLK(st.st_mode), "mapper must be block device")
        return str(os.major(st.st_rdev)) + ":" + str(os.minor(st.st_rdev))

    def filesystem(self, enrollment):
        mapper, _ = names(enrollment)
        _, data = self.run(["blkid", "--probe", "--output", "export", "/dev/mapper/" + mapper])
        values = dict(line.split("=", 1) for line in data.splitlines() if "=" in line)
        require(values.get("TYPE") == "ext4" and values.get("UUID") == enrollment["data_uuid"], "owned ext4 UUID mismatch")

    def idle(self):
        for unit in ("docker.service", "docker.socket", "containerd.service"):
            code, status = self.run(["systemctl", "is-active", unit], allowed=(0, 3, 4))
            require(code != 0 and status.strip() in {"inactive", "failed", "unknown"}, "stop host runtime before volume close")

    def format(self, contract, key):
        self.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--uuid", contract["luks_uuid"], "--key-file", "-", contract["device"]], input=key)

    def open(self, enrollment, contract, key):
        mapper, _ = names(enrollment)
        self.run(["cryptsetup", "open", "--type", "luks2", "--key-file", "-", contract["device"], mapper], input=key)

    def mkfs(self, enrollment):
        mapper, _ = names(enrollment)
        # No force flag, wipefs erase, discard option, or repeat-format path exists.
        self.run(["mkfs.ext4", "-q", "-U", enrollment["data_uuid"], "/dev/mapper/" + mapper])

    def mount(self, enrollment):
        mapper, mount = names(enrollment)
        self.run(["mount", "--types", "ext4", "--options", "rw,nodev,nosuid", "/dev/mapper/" + mapper, mount])

    def unmount(self, enrollment):
        self.run(["umount", names(enrollment)[1]])

    def close(self, enrollment):
        self.run(["cryptsetup", "close", names(enrollment)[0]])


def check_identity(enrollment, contract, backend):
    validate(enrollment, contract)
    backend.identity(enrollment)
    facts = backend.probe(contract)
    require(facts["serial"] == contract["serial"] and facts["size_bytes"] == contract["size_bytes"] and facts["major_minor"] == contract["major_minor"] and facts["reported_major_minor"] == contract["major_minor"], "device serial/size/number mismatch")
    require(facts["type"] == "disk" and not facts["parent"] and not facts["readonly"], "only writable whole additional disks allowed")
    require(contract["major_minor"] not in facts["root_numbers"], "root disk or root ancestor forbidden")
    return facts


def check_empty(enrollment, contract, backend):
    facts = check_identity(enrollment, contract, backend)
    require(not facts["children"] and not facts["holders"], "disk has children/holders; never format")
    require(all(row["maj:min"] != contract["major_minor"] for row in facts["mounts"]), "mounted disk forbidden")
    target = names(enrollment)[1]
    require(all(row["target"] != target and not row["target"].startswith(target + "/") for row in facts["mounts"]), "target mountpoint or submount already in use")
    require(backend.mapper(enrollment, contract) is None, "role mapper already exists")
    backend.signatures(contract["device"])
    return facts


def paths(root, enrollment):
    return safe_path(root, "var/lib/map-volume/" + enrollment["role"] + ".json"), safe_path(root, names(enrollment)[1].lstrip("/"))


def read_state(path, enrollment, contract):
    state = json.loads(host.regular(path, True))
    require(state["enrollment_sha256"] == digest(enrollment) and state["contract_sha256"] == digest(contract), "volume journal ownership mismatch")
    return state


def check_target(path):
    if path.exists():
        st = path.stat()
        require(path.is_dir() and st.st_uid == os.geteuid() and not (stat.S_IMODE(st.st_mode) & 0o022), "trusted mountpoint required")
        require(not any(path.iterdir()), "mountpoint contains data")


def lock(root):
    directory = safe_path(root, "var/lib/map-volume")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(directory.stat().st_uid == os.geteuid() and stat.S_IMODE(directory.stat().st_mode) == 0o700, "trusted private journal directory required")
    fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid(), "untrusted volume lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def mounted_identity(enrollment, contract, backend, require_mounted=False):
    facts = check_identity(enrollment, contract, backend)
    number = backend.mapper(enrollment, contract)
    mapper, target = names(enrollment)
    children = flatten(facts["children"])
    if number:
        require(len(children) == 1 and children[0].get("maj:min") == number and children[0].get("type") == "crypt" and len(facts["holders"]) == 1, "unexpected mapped disk child or holder")
    else:
        require(not children and not facts["holders"], "unowned disk child or holder")
    hits = [r for r in facts["mounts"] if r["target"] == target or r["target"].startswith(target + "/") or r["maj:min"] in {number, contract["major_minor"]}]
    if hits:
        require(number is not None and len(hits) == 1, "unexpected disk mount or submount")
        row = hits[0]
        require(row["target"] == target and row["maj:min"] == number and row["fstype"] == "ext4" and row["uuid"] == enrollment["data_uuid"] and "rw" in row["options"].split(","), "mounted identity mismatch")
    require(not require_mounted or bool(hits), "owned volume is not mounted; restore-mount needs external key")
    return number, bool(hits)


def inspect(enrollment, contract, root=Path("/"), backend=None):
    backend = backend or Linux()
    validate(enrollment, contract)
    state_path, target = paths(root, enrollment)
    if state_path.exists():
        state = read_state(state_path, enrollment, contract)
        check_identity(enrollment, contract, backend)
        return {"status": state["status"], "role": enrollment["role"], "read_only": True, "reformat_allowed": False}
    check_target(target)
    check_empty(enrollment, contract, backend)
    count = backend.scan_all(contract)
    require(count == contract["size_bytes"], "full disk scan incomplete")
    check_empty(enrollment, contract, backend)
    return {"status": "all_zero_disk_verified", "role": enrollment["role"], "read_only": True, "bytes_read": count, "formats_executed": 0}


def verify(enrollment, contract, key, root=Path("/"), backend=None):
    backend = backend or Linux()
    validate(enrollment, contract)
    state = read_state(paths(root, enrollment)[0], enrollment, contract)
    require(state["status"] == "ready", "volume not ready; interrupted formats require manual recovery")
    check_identity(enrollment, contract, backend)
    backend.luks(contract, key)
    mounted_identity(enrollment, contract, backend, True)
    backend.filesystem(enrollment)
    return {"status": "volume_ready", "role": enrollment["role"], "data_uuid": enrollment["data_uuid"], "luks_uuid": contract["luks_uuid"], "idempotent": True, "reboot_auto_unlock": False, "external_key_recovery_required": True, "formats_executed": 0}


def prepare(enrollment, contract, key, approved_sha, format_empty=False, root=Path("/"), backend=None):
    backend = backend or Linux()
    validate(enrollment, contract)
    require(format_empty is True and enrollment["approval"] == "approved-empty-host-only" and contract["approval"] == "approved-empty-volume-only", "explicit empty-volume format approval required")
    require(approved_sha == digest(contract), "approved volume contract pin mismatch")
    require(32 <= len(key) <= 4096, "private recovery key required")
    check_identity(enrollment, contract, backend)
    fd = lock(root)
    try:
        state_path, target = paths(root, enrollment)
        if state_path.exists():
            return verify(enrollment, contract, key, root, backend)
        check_target(target)
        check_empty(enrollment, contract, backend)
        require(backend.scan_all(contract) == contract["size_bytes"], "full disk scan incomplete")
        check_empty(enrollment, contract, backend)
        # Durable quarantine is written BEFORE the first destructive operation.
        state = {"schema_version": 1, "status": "formatting", "role": enrollment["role"], "enrollment_sha256": digest(enrollment), "contract_sha256": digest(contract), "all_zero_bytes_read": contract["size_bytes"], "automatic_reformat_forbidden": True}
        host.atomic_json(state_path, state)
        backend.format(contract, key)
        backend.luks(contract, key)
        state["status"] = "luks_created"; host.atomic_json(state_path, state)
        backend.open(enrollment, contract, key)
        require(backend.mapper(enrollment, contract) is not None, "owned mapper did not open")
        state["status"] = "filesystem_creating"; host.atomic_json(state_path, state)
        backend.mkfs(enrollment)
        backend.filesystem(enrollment)
        state["status"] = "filesystem_ready"; host.atomic_json(state_path, state)
        check_target(target)
        target.mkdir(mode=0o755, parents=True, exist_ok=True)
        backend.mount(enrollment)
        mounted_identity(enrollment, contract, backend, True)
        state["status"] = "ready"; host.atomic_json(state_path, state)
        result = verify(enrollment, contract, key, root, backend)
        result["formats_executed"] = 1
        return result
    finally:
        os.close(fd)


def restore_mount(enrollment, contract, key, root=Path("/"), backend=None):
    backend = backend or Linux()
    validate(enrollment, contract)
    check_identity(enrollment, contract, backend)
    fd = lock(root)
    try:
        state_path, target = paths(root, enrollment)
        state = read_state(state_path, enrollment, contract)
        require(state["status"] in READY and state["status"] != "closing", "incomplete format or close cannot auto-recover")
        backend.luks(contract, key)
        number, mounted = mounted_identity(enrollment, contract, backend)
        if not number:
            backend.open(enrollment, contract, key)
        backend.filesystem(enrollment)
        if not mounted:
            check_target(target)
            target.mkdir(mode=0o755, parents=True, exist_ok=True)
            backend.mount(enrollment)
        mounted_identity(enrollment, contract, backend, True)
        state["status"] = "ready"; host.atomic_json(state_path, state)
        return verify(enrollment, contract, key, root, backend)
    finally:
        os.close(fd)


def rollback(enrollment, contract, key, root=Path("/"), backend=None):
    backend = backend or Linux()
    validate(enrollment, contract)
    check_identity(enrollment, contract, backend)
    fd = lock(root)
    try:
        state_path, _ = paths(root, enrollment)
        state = read_state(state_path, enrollment, contract)
        # Header and key are required even for recovery of an interrupted format.
        # If header creation failed, this deliberately requires manual inspection.
        backend.luks(contract, key)
        number, mounted = mounted_identity(enrollment, contract, backend)
        backend.idle()
        previous = state.get("close_from", state["status"])
        state["close_from"] = previous
        state["status"] = "closing"; host.atomic_json(state_path, state)
        if mounted:
            backend.filesystem(enrollment)
            backend.unmount(enrollment)
        if number:
            require(not mounted_identity(enrollment, contract, backend)[1], "volume still mounted")
            backend.close(enrollment)
        number, mounted = mounted_identity(enrollment, contract, backend)
        require(not number and not mounted, "volume did not close")
        state["status"] = "closed" if previous in READY else "quarantined_closed"
        host.atomic_json(state_path, state)
        return {"status": state["status"], "role": enrollment["role"], "data_deleted": 0, "volumes_deleted": 0, "keys_stored": 0, "formats_executed": 0}
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("contract-hash", "inspect", "prepare", "verify", "restore-mount", "rollback"))
    parser.add_argument("--enrollment", required=True, type=Path)
    parser.add_argument("--device-contract", required=True, type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--approved-volume-sha256")
    parser.add_argument("--format-empty", action="store_true")
    args = parser.parse_args()
    try:
        enrollment = json.loads(host.regular(args.enrollment))
        contract = json.loads(host.regular(args.device_contract))
        validate(enrollment, contract)
        if args.command == "contract-hash":
            result = {"sha256": digest(contract)}
        elif args.command == "inspect":
            result = inspect(enrollment, contract)
        else:
            require(os.geteuid() == 0 and args.key_file is not None, "root and private key file required")
            key = key_bytes(args.key_file)
            if args.command == "prepare":
                result = prepare(enrollment, contract, key, args.approved_volume_sha256, args.format_empty)
            elif args.command == "verify":
                result = verify(enrollment, contract, key)
            elif args.command == "restore-mount":
                result = restore_mount(enrollment, contract, key)
            else:
                result = rollback(enrollment, contract, key)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__, "reason": str(error) if isinstance(error, ValueError) else "volume operation failed; inspect private journal"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
