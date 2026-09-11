#!/usr/bin/env python3
"""Offline, fail-closed artifact verification and atomic staging for empty NCP hosts.

No Docker commands, registry access, receiver execution, or service activation.
An externally reviewed SHA256 of security-approval.json is required for staging;
checksums alone do not authenticate a publisher. Candidate verification never
authorizes staging. The existing Caddy and OSRM verifiers remain authoritative.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
HEX = re.compile(r"[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")
IMAGE = re.compile(r"[a-z0-9][a-z0-9./_-]+@sha256:[a-f0-9]{64}")
NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SCOPES = {"prod": "serving", "admin": "control", "learning": "synthetic"}
CADDY_ANCHOR_SHA256 = "e6c361045e7a57f94aa08b35342972530c4f09a699ab86e6f76999609a82a2cb"
PROD_REQUIRED = {"user", "agent", "hub", "yolo", "postgres", "redis", "proxy", "osrm-foot", "osrm-bicycle", "edge"}
PROD_OPTIONAL = {"dns", "postgres-exporter", "redis-exporter", "node-exporter", "cadvisor"}
MAX_BUNDLE_BYTES = 64 * 1024**3
MAX_RUNTIME_BYTES = 64 * 1024**3


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


caddy = load_module("ncp_caddy", "install-caddy-artifact.py")
osrm = load_module("ncp_osrm", "osrm-release.py")
roles = load_module("ncp_roles", "verify-role-manifest.py")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def exact(value, names, label):
    require(isinstance(value, dict) and set(value) == set(names.split()), "invalid " + label + " fields")


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def safe_path(path, *, directory=False):
    path = Path(os.path.abspath(path))
    # macOS exposes these two system aliases. Other symlinks, including artifact
    # and destination ancestors, are forbidden even when they resolve in-bounds.
    for item in (path, *path.parents):
        require(not item.is_symlink() or str(item) in ("/tmp", "/var"), "symlink path forbidden")
    path = path.resolve()
    if directory:
        require(path.is_dir(), "regular directory required")
    return path


def regular(path):
    path = safe_path(path)
    require(path.is_file() and stat.S_ISREG(path.stat().st_mode), "regular artifact required")
    return path


def sha256(path):
    with regular(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    path = regular(path)
    require(path.stat().st_size <= 1024 * 1024, "JSON input exceeds 1 MiB")
    with path.open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=unique)


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def artifact_path(bundle, name):
    require(isinstance(name, str) and FILENAME.fullmatch(name) and ".." not in name,
            "flat artifact filename required")
    require(name not in {"contract.json", "security-approval.json", "receipt.json", "runtime"}, "reserved artifact filename")
    return regular(bundle / name)


def verify_approval(bundle, contract, expected=None):
    approval = read_json(bundle / "security-approval.json")
    exact(approval, "schema_version status contract_sha256 images_sha256 reviewer reviewed_at evidence_sha256 serving_approval", "security approval")
    require(type(approval["schema_version"]) is int and approval["schema_version"] == 1, "unsupported approval schema")
    require(approval["status"] in ("candidate", "approved"), "invalid security status")
    require(approval["serving_approval"] is False, "bootstrap cannot authorize serving")
    require(approval["contract_sha256"] == sha256(bundle / "contract.json"), "approval contract checksum mismatch")
    require(approval["images_sha256"] == canonical_sha(contract["images"]), "approval image identity mismatch")
    if approval["status"] == "approved":
        require(isinstance(approval["reviewer"], str) and re.fullmatch(r"[A-Za-z0-9@._/-]{3,128}", approval["reviewer"]), "explicit reviewer required")
        reviewed = datetime.fromisoformat(str(approval["reviewed_at"]).replace("Z", "+00:00"))
        require(reviewed.utcoffset() is not None and reviewed.utcoffset().total_seconds() == 0, "review time must be UTC")
        require(isinstance(approval["evidence_sha256"], str) and HEX.fullmatch(approval["evidence_sha256"]), "review evidence checksum required")
    else:
        require(all(approval[key] is None for key in ("reviewer", "reviewed_at", "evidence_sha256")), "candidate must not claim completed review")
    actual = sha256(bundle / "security-approval.json")
    if expected is not None:
        require(HEX.fullmatch(expected) and expected == actual, "external approval pin mismatch")
    return approval, actual, approval["status"] == "approved" and expected is not None


def verify_role_contract(bundle, contract, files):
    descriptor = contract["role_contract"]
    role = contract["role"]
    if role == "prod":
        require(descriptor is None, "production receiver owns the serving Compose contract")
        return
    exact(descriptor, "manifest compose rendered scrape_config", "role contract")
    paths = {}
    for key in ("manifest", "compose", "rendered"):
        paths[key] = artifact_path(bundle, descriptor[key])
        require(paths[key].stat().st_size <= 1024 * 1024, "role review exceeds 1 MiB")
        files.add(paths[key].name)
    manifest, config = read_json(paths["manifest"]), read_json(paths["rendered"])
    require(manifest.get("role") == role and manifest.get("data_scope") == contract["data_scope"], "role contract mismatch")
    require(manifest.get("compose_sha256") == sha256(paths["compose"]), "role Compose checksum mismatch")
    roles.validate(manifest, config, host_identity=manifest.get("host_identity"), deploy_account=manifest.get("deploy_account"))
    require(set(config["services"]) == set(contract["images"]), "rendered image service set mismatch")
    for service, value in config["services"].items():
        require(value["image"] == contract["images"][service]["image"], "rendered image identity mismatch")
        # This is a public configuration handoff. Secrets are injected separately
        # on the host and must remain placeholders in its rendered review copy.
        for name, value in value.get("environment", {}).items():
            if any(part in name for part in ("PASSWORD", "TOKEN", "DATABASE_URL", "ADMIN_TARGETS")) and value:
                if name == "ADMIN_TARGETS":
                    for target in json.loads(value).values():
                        for key, secret in target.items():
                            if any(part in key for part in ("PASSWORD", "TOKEN", "DATABASE_URL")) and secret:
                                require(re.fullmatch(r"SECRET_REF_[A-Z0-9_]+", str(secret)), "role review must contain secret references only")
                else:
                    require(re.fullmatch(r"SECRET_REF_[A-Z0-9_]+", str(value)), "role review must contain secret references only")
    if role == "admin":
        path = artifact_path(bundle, descriptor["scrape_config"])
        files.add(path.name)
        roles.validate_scrapes(read_json(path))
    else:
        require(descriptor["scrape_config"] is None, "learning cannot scrape serving hosts")


def verify_bundle(bundle, role, expected=None, *, staged=False):
    require(role in SCOPES, "test is observe-only; no test bootstrap artifacts")
    bundle = safe_path(bundle, directory=True)
    contract = read_json(bundle / "contract.json")
    exact(contract, "schema_version role data_scope images role_contract caddy map receiver artifact_sha256", "artifact contract")
    require(type(contract["schema_version"]) is int and contract["schema_version"] == 1, "unsupported contract schema")
    require(contract["role"] == role and contract["data_scope"] == SCOPES[role], "host role/data scope mismatch")
    images = contract["images"]
    require(isinstance(images, dict) and 0 < len(images) <= 32, "bounded nonempty image contract required")
    for service, entry in images.items():
        require(NAME.fullmatch(service), "invalid service name")
        exact(entry, "image source_commit platform", "image")
        identity = entry["image"]
        require(isinstance(identity, str) and (IMAGE.fullmatch(identity) or
                role == "prod" and service == "edge" and re.fullmatch(r"sha256:[a-f0-9]{64}", identity)),
                "immutable image digest required")
        upstream = role == "prod" and service in (PROD_REQUIRED | PROD_OPTIONAL) - {"user", "agent", "hub", "yolo", "edge"}
        source_commit = entry["source_commit"]
        require((upstream and source_commit is None) or
                (isinstance(source_commit, str) and COMMIT.fullmatch(source_commit)),
                "exact source commit required; upstream image revision may be null")
        require(entry["platform"] == "linux/amd64", "host image platform mismatch")
    if role == "prod":
        require(PROD_REQUIRED <= set(images) <= PROD_REQUIRED | PROD_OPTIONAL, "production image role boundary mismatch")
    files = {"contract.json", "security-approval.json"}
    # Bound the inventory before reading archives. Metadata checks never hash an
    # unrelated user graph and candidates cannot request unbounded decompression.
    inventory = contract["artifact_sha256"]
    require(isinstance(inventory, dict) and len(inventory) <= 32, "bounded artifact inventory required")
    inventory_paths = [artifact_path(bundle, name) for name in inventory]
    require(sum(path.stat().st_size for path in inventory_paths) <= MAX_BUNDLE_BYTES, "bundle size exceeds 64 GiB")
    verify_role_contract(bundle, contract, files)
    receiver = contract["receiver"]
    if receiver is not None:
        exact(receiver, "file sha256 source_commit owner capabilities", "receiver")
        require(receiver["owner"] == ("root" if role == "prod" else "session-c"), "receiver owner mismatch")
        require(isinstance(receiver["source_commit"], str) and COMMIT.fullmatch(receiver["source_commit"]), "receiver source commit required")
        path = artifact_path(bundle, receiver["file"])
        require(path.stat().st_size <= 4 * 1024 * 1024, "receiver exceeds 4 MiB")
        require(receiver["sha256"] == sha256(path), "receiver checksum mismatch")
        files.add(path.name)
        require(isinstance(receiver["capabilities"], list) and
                all(isinstance(item, str) and NAME.fullmatch(item) for item in receiver["capabilities"]),
                "invalid receiver capability declarations")
    if contract["caddy"] is not None:
        require(role in ("prod", "admin"), "learning cannot contain an edge proxy")
        exact(contract["caddy"], "archive report", "Caddy descriptor")
        archive = artifact_path(bundle, contract["caddy"]["archive"])
        report = artifact_path(bundle, contract["caddy"]["report"])
        require(report.stat().st_size <= 16 * 1024 * 1024, "Caddy report exceeds 16 MiB")
        files.update((archive.name, report.name))
        require(sha256(caddy.MANIFEST) == CADDY_ANCHOR_SHA256, "reviewed Caddy trust anchor changed")
        anchor = caddy.verify_files(archive, report)
        identity = images.get("edge", {}).get("image", "").split("@")[-1]
        require(identity in {anchor["source_image_id"], anchor["platform_image_id"], anchor["config_image_id"]},
                "Caddy image differs from reviewed archive identity chain")
    else:
        require(role != "prod", "production requires reviewed Caddy transfer artifact")
    runtime_bytes = 0
    if contract["map"] is not None:
        require(role == "prod", "map graph belongs only to serving host")
        exact(contract["map"], "archive manifest", "map descriptor")
        archive = artifact_path(bundle, contract["map"]["archive"])
        manifest_path = artifact_path(bundle, contract["map"]["manifest"])
        files.update((archive.name, manifest_path.name))
        manifest = read_json(manifest_path)
        runtime_bytes = osrm.verify_manifest(manifest)
        require(runtime_bytes <= MAX_RUNTIME_BYTES, "map runtime exceeds 64 GiB")
        require(manifest.get("archive", {}).get("bytes") == archive.stat().st_size, "map archive size mismatch")
        osrm.verify_archive(archive, manifest)
        for service in ("osrm-foot", "osrm-bicycle"):
            require(images[service]["image"] == manifest["engine_image"], "map/runtime engine mismatch")
    else:
        require(role != "prod", "production requires complete map runtime artifact")
    approval, approval_sha, trusted = verify_approval(bundle, contract, expected)
    require(approval["status"] != "approved" or receiver is not None, "reviewed receiver required before approval")
    require(approval["status"] != "approved" or "empty-host-ncp-v1" in receiver["capabilities"],
            "receiver is not reviewed for empty NCP hosts")
    checksums = contract["artifact_sha256"]
    require(isinstance(checksums, dict) and set(checksums) == files - {"contract.json", "security-approval.json"},
            "complete artifact checksum inventory required")
    for name, expected_file in checksums.items():
        require(isinstance(expected_file, str) and HEX.fullmatch(expected_file) and
                sha256(bundle / name) == expected_file, "contract artifact checksum mismatch")
    allowed = files | ({"receipt.json", "runtime"} if staged and contract["map"] else {"receipt.json"} if staged else set())
    require({item.name for item in bundle.iterdir()} == allowed, "unexpected or missing bundle entries")
    return {"status": "approved_for_staging" if trusted else "candidate_verified" if approval["status"] == "candidate" else "untrusted_approval_verified",
            "role": role, "contract_sha256": sha256(bundle / "contract.json"), "approval_sha256": approval_sha,
            "activation_authorized": False, "docker_actions": 0, "runtime_bytes": runtime_bytes,
            "files": {name: sha256(bundle / name) for name in sorted(files)}}


def extract_runtime(archive, destination, manifest):
    destination.mkdir(mode=0o700)
    seen = set()
    with tarfile.open(archive, "r:gz") as saved:
        for member in saved:
            name = member.name
            require(member.isfile() and name in manifest["runtime_files"] and name not in seen and member.mode == 0o644,
                    "unsafe or duplicate runtime archive member")
            path = destination / name
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with saved.extractfile(member) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            path.chmod(0o644)
            seen.add(name)
    osrm.verify_tree(destination, manifest)
    destination.chmod(0o755)


def verify_installed(destination, role, expected):
    destination = safe_path(destination, directory=True)
    result = verify_bundle(destination, role, expected, staged=True)
    require(result["status"] == "approved_for_staging", "candidate cannot be installed")
    receipt = read_json(destination / "receipt.json")
    require(receipt == {**result, "status": "staged", "map_files": 40 if result["runtime_bytes"] else 0}, "installed receipt mismatch")
    contract = read_json(destination / "contract.json")
    if contract["map"]:
        runtime = safe_path(destination / "runtime", directory=True)
        osrm.verify_tree(runtime, read_json(destination / contract["map"]["manifest"]))
    return {**receipt, "status": "installed_verified"}


def stage_locked(bundle, destination, role, expected):
    require(expected is not None, "external approval pin required for staging")
    destination = safe_path(destination)
    parent = safe_path(destination.parent, directory=True)
    result = verify_bundle(bundle, role, expected)
    require(result["status"] == "approved_for_staging", "candidate cannot be staged")
    if destination.exists():
        existing = verify_installed(destination, role, expected)
        require(existing["contract_sha256"] == result["contract_sha256"], "destination belongs to a different release")
        return {**existing, "status": "already_staged"}
    bundle = safe_path(bundle, directory=True)
    needed = sum((bundle / name).stat().st_size for name in result["files"]) + result["runtime_bytes"] + 16 * 1024**2
    require(shutil.disk_usage(parent).free >= needed, "insufficient staging disk headroom")
    temporary = Path(tempfile.mkdtemp(prefix=".ncp-artifacts-", dir=parent))
    try:
        for name, expected_file in result["files"].items():
            source = regular(bundle / name)
            target = temporary / name
            with source.open("rb") as input_file, target.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, 1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
            target.chmod(0o600)
            require(sha256(target) == expected_file, "artifact changed during copy")
        # Verify the private copy to close source mutation races before publish.
        require(verify_bundle(temporary, role, expected) == result, "staging source changed")
        contract = read_json(temporary / "contract.json")
        if contract["map"]:
            extract_runtime(temporary / contract["map"]["archive"], temporary / "runtime",
                            read_json(temporary / contract["map"]["manifest"]))
        receipt = {**result, "status": "staged", "map_files": 40 if result["runtime_bytes"] else 0}
        with (temporary / "receipt.json").open("x") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        (temporary / "receipt.json").chmod(0o600)
        # Atomic directory rename; an existing nonempty release is never replaced.
        require(not destination.exists() and not destination.is_symlink(), "destination appeared during staging")
        os.rename(temporary, destination)
        # Persist the published directory entry on supported host filesystems.
        fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return receipt
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def stage(bundle, destination, role, expected):
    destination = safe_path(destination)
    parent = safe_path(destination.parent, directory=True)
    require(parent.stat().st_uid == os.geteuid() and parent.stat().st_mode & 0o022 == 0,
            "staging parent must be caller-owned and not group/world-writable")
    require(FILENAME.fullmatch(destination.name), "bounded destination name required")
    lock = parent / (".ncp-stage-" + destination.name + ".lock")
    # Persistent lock inode prevents two cooperating installers from publishing
    # the same destination. Never unlink a lock another process may have opened.
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(fd)
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid() and
                metadata.st_nlink == 1 and metadata.st_mode & 0o077 == 0, "unsafe staging lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another artifact installer holds the destination lock") from None
        return stage_locked(bundle, destination, role, expected)
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "stage", "verify-installed"):
        command = commands.add_parser(name)
        command.add_argument("--role", choices=tuple(SCOPES), required=True)
        command.add_argument("--expected-approval-sha256", required=name != "verify")
        if name != "verify-installed":
            command.add_argument("--bundle", type=Path, required=True)
        if name != "verify":
            command.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_bundle(args.bundle, args.role, args.expected_approval_sha256)
        elif args.command == "stage":
            result = stage(args.bundle, args.destination, args.role, args.expected_approval_sha256)
        else:
            result = verify_installed(args.destination, args.role, args.expected_approval_sha256)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError, AttributeError, tarfile.TarError):
        # Untrusted documents can contain secrets; do not echo exception content.
        print(json.dumps({"status": "rejected", "activation_authorized": False, "docker_actions": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
