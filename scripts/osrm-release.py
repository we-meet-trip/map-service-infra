#!/usr/bin/env python3
"""Build a new, immutable OSRM MLD release without touching serving volumes.

The 20 runtime files per profile follow Project-OSRM v26.5.0 StorageConfig.
Preprocessing intermediates and the source remain in the build volume; the
runtime archive includes every dataset needed for geometry AND turn guidance.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request

IMAGE = "ghcr.io/project-osrm/osrm-backend@sha256:aa6a1de3a71dafffd0ba39340542524f66e6841fc19bf7874a0e6a7967837f56"
VERSION = "v26.5.0"
PROFILES = ("foot", "bicycle")
PROFILE_HASHES = {"foot": "b58e92c64240b1fc25fa72ea25b09411bc8d3cb05f04c11828977f094ce773c8",
                  "bicycle": "dd473cced008f6a0f63344595b8168c161851ef88544f0150422a10b0ba0377a"}
SUFFIXES = tuple(sorted("datasource_names ebg_nodes edges fileIndex geometry icd maneuver_overrides names nbg_nodes properties ramIndex timestamp tld tls turn_duration_penalties turn_weight_penalties cells cell_metrics mldgr partition".split()))
RUNTIME_FILES = tuple(f"{p}/korea.osrm.{s}" for p in PROFILES for s in SUFFIXES)
SOURCE_CONTRACT = "https://raw.githubusercontent.com/Project-OSRM/osrm-backend/v26.5.0/include/storage/storage_config.hpp"


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path, algorithm="sha256"):
    h = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, **kwargs)


def container(volume, command, *, writable=False, **kwargs):
    return docker("run", "--rm", "--platform", "linux/amd64", "--network", "none",
                  "--memory", "256m", "--memory-swap", "256m", "--cpus", "2", "--cap-drop", "ALL",
                  "-v", f"{volume}:/data:{'rw' if writable else 'ro'}", IMAGE, *command, **kwargs)


def build_stage(volume, profile, command, log, output):
    """Preserve stage exit/OOM and sampled cgroup peak, including on failure."""
    name = f"{volume}-{profile}-{command[0]}"
    docker("create", "--name", name, "--platform", "linux/amd64", "--network", "none",
           "--memory", "4608m", "--memory-swap", "10752m", "--cpus", "1", "--cap-drop", "ALL",
           "-v", f"{volume}:/data:rw", IMAGE, *command, stdout=subprocess.DEVNULL)
    stage = {"container": name, "profile": profile, "command": command[0], "started_at": now(),
             "memory_limit_bytes": 4608 * 1024**2, "memory_plus_swap_limit_bytes": 10752 * 1024**2,
             "sampled_peak_memory_bytes": 0, "sampled_peak_swap_bytes": 0}
    docker("start", name, stdout=subprocess.DEVNULL)
    while True:
        state = json.loads(docker("inspect", name, "--format", "{{json .State}}", capture_output=True, text=True).stdout)
        if not state["Running"]:
            break
        peak = subprocess.run(["docker", "exec", name, "cat", "/sys/fs/cgroup/memory.peak"], capture_output=True, text=True)
        if peak.returncode == 0 and peak.stdout.strip().isdigit():
            stage["sampled_peak_memory_bytes"] = max(stage["sampled_peak_memory_bytes"], int(peak.stdout))
        swap = subprocess.run(["docker", "exec", name, "cat", "/sys/fs/cgroup/memory.swap.current"], capture_output=True, text=True)
        if swap.returncode == 0 and swap.stdout.strip().isdigit():
            stage["sampled_peak_swap_bytes"] = max(stage["sampled_peak_swap_bytes"], int(swap.stdout))
        time.sleep(3)
    docker("logs", name, stdout=log, stderr=subprocess.STDOUT)
    stage.update({"completed_at": now(), "exit_code": state["ExitCode"], "oom_killed": state["OOMKilled"]})
    with (output / "build-stages.jsonl").open("a") as file:
        file.write(json.dumps(stage) + "\n")
    if state["ExitCode"] != 0:
        raise RuntimeError(f"{profile}/{command[0]} failed; container and stage evidence preserved")


def verify_manifest(manifest):
    if manifest.get("schema") != 1 or manifest.get("engine_image") != IMAGE or manifest.get("engine_version") != VERSION:
        raise ValueError("unsupported engine/schema")
    if manifest.get("algorithm") != "mld" or set(manifest.get("runtime_files", {})) != set(RUNTIME_FILES):
        raise ValueError("incomplete MLD runtime dataset")
    for record in manifest["runtime_files"].values():
        if type(record.get("bytes")) is not int or record["bytes"] <= 0 or not re.fullmatch(r"[a-f0-9]{64}", record.get("sha256", "")):
            raise ValueError("invalid file identity")
    if set(manifest.get("profiles", {})) != set(PROFILES):
        raise ValueError("missing profiles")
    for profile, expected in PROFILE_HASHES.items():
        if manifest["profiles"][profile].get("sha256") != expected:
            raise ValueError("profile checksum mismatch")
    return sum(record["bytes"] for record in manifest["runtime_files"].values())


def verify_archive(archive, manifest):
    verify_manifest(manifest)
    if digest(archive) != manifest["archive"]["sha256"]:
        raise ValueError("archive checksum mismatch")
    seen = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            name = member.name
            if not member.isfile() or name not in manifest["runtime_files"] or name in seen:
                raise ValueError("unexpected/duplicate/nonregular archive member")
            if member.mode != 0o644:
                raise ValueError("runtime archive files must be nonroot-readable mode 0644")
            record = manifest["runtime_files"][name]
            if member.size != record["bytes"]:
                raise ValueError("member size mismatch")
            h = hashlib.sha256()
            stream = tar.extractfile(member)
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
            if h.hexdigest() != record["sha256"]:
                raise ValueError("member checksum mismatch")
            seen.add(name)
    if seen != set(RUNTIME_FILES):
        raise ValueError("missing archive member")


def verify_tree(directory, manifest):
    verify_manifest(manifest)
    files = set()
    for path in Path(directory).rglob("*"):
        if path.is_symlink():
            raise ValueError("symlink in runtime tree")
        if path.is_file():
            files.add(path.relative_to(directory).as_posix())
        elif not path.is_dir():
            raise ValueError("nonregular runtime entry")
    if files != set(RUNTIME_FILES):
        raise ValueError("incomplete/unexpected runtime tree")
    for name, record in manifest["runtime_files"].items():
        path = Path(directory) / name
        if path.stat().st_mode & 0o7777 != 0o644:
            raise ValueError("runtime file must have nonroot-readable mode 0644")
        if path.stat().st_size != record["bytes"] or digest(path) != record["sha256"]:
            raise ValueError("runtime file checksum/size mismatch")


def build(args):
    if not re.fullmatch(r"https://download\.geofabrik\.de/asia/south-korea-\d{6}\.osm\.pbf", args.source):
        raise ValueError("use a fixed-date official South Korea source URL")
    if not re.fullmatch(r"[a-f0-9]{32}", args.md5):
        raise ValueError("expected provider MD5 required")
    if not re.fullmatch(r"map-(?:test-)?osrm-build-[a-z0-9-]+", args.volume):
        raise ValueError("use a new map-osrm-build-* volume")
    if subprocess.run(["docker", "volume", "inspect", args.volume], capture_output=True).returncode == 0:
        raise ValueError("existing volume preserved; choose a new build volume")
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    if shutil.disk_usage(out).free < 10 * 1024**3:
        raise ValueError("at least 10 GiB build disk headroom required")
    docker("image", "inspect", IMAGE, stdout=subprocess.DEVNULL)
    memory_raw = docker("run", "--rm", "--platform", "linux/amd64", "--network", "none", "--memory", "32m",
                        IMAGE, "cat", "/proc/meminfo", capture_output=True, text=True).stdout
    memory = {key.rstrip(":"): int(value) * 1024 for key, value, *_ in (line.split() for line in memory_raw.splitlines())}
    if memory["MemAvailable"] < 5 * 1024**3 or memory["SwapFree"] < 6400 * 1024**2:
        raise ValueError("build requires 5 GiB available RAM and 6.25 GiB free swap; never resize or restart a serving host here")
    actual = docker("run", "--rm", "--platform", "linux/amd64", "--network", "none", IMAGE,
                    "osrm-routed", "--version", capture_output=True, text=True).stdout.strip()
    if actual != VERSION:
        raise ValueError("engine reports an unexpected version")
    manifest = {"schema": 1, "engine_image": IMAGE, "engine_version": VERSION,
                "algorithm": "mld", "started_at": now(), "build_volume": args.volume,
                "runtime_contract": SOURCE_CONTRACT, "runtime_files": {}, "profiles": {},
                "build_preflight": {"available_memory_bytes": memory["MemAvailable"], "free_swap_bytes": memory["SwapFree"]},
                "source": {"url": args.source, "provider_md5": args.md5}}
    if args.source_volume:
        if not re.fullmatch(r"map-(?:test-)?osrm-build-[a-z0-9-]+", args.source_volume):
            raise ValueError("unexpected source build volume")
        source_md5 = container(args.source_volume, ["md5sum", "/data/korea.osm.pbf"], capture_output=True, text=True).stdout.split()[0]
        if source_md5 != args.md5:
            raise ValueError("source volume checksum mismatch")
        source_sha = container(args.source_volume, ["sha256sum", "/data/korea.osm.pbf"], capture_output=True, text=True).stdout.split()[0]
        source_size = container(args.source_volume, ["stat", "-c", "%s", "/data/korea.osm.pbf"], capture_output=True, text=True).stdout.strip()
        manifest["source"].update({"sha256": source_sha, "bytes": int(source_size), "reused_from_volume": args.source_volume, "verified_at": now()})
        docker("volume", "create", args.volume, stdout=subprocess.DEVNULL)
        docker("run", "--rm", "--platform", "linux/amd64", "--network", "none", "--cap-drop", "ALL",
               "-v", f"{args.volume}:/data", "-v", f"{args.source_volume}:/source:ro", IMAGE,
               "sh", "-ec", "cp /source/korea.osm.pbf /data/korea.osm.pbf")
    else:
        with tempfile.TemporaryDirectory(prefix="map-osrm-source-") as tmp:
            source = Path(tmp) / "korea.osm.pbf"
            with urllib.request.urlopen(args.source, timeout=60) as response, source.open("wb") as destination:
                shutil.copyfileobj(response, destination, length=1024 * 1024)
            if digest(source, "md5") != args.md5:
                raise ValueError("source differs from authenticated provider checksum")
            manifest["source"].update({"sha256": digest(source), "bytes": source.stat().st_size, "retrieved_at": now()})
            docker("volume", "create", args.volume, stdout=subprocess.DEVNULL)
            docker("run", "--rm", "--platform", "linux/amd64", "--network", "none", "--cap-drop", "ALL",
                   "-v", f"{args.volume}:/data", "-v", f"{tmp}:/source:ro", IMAGE,
                   "sh", "-ec", "cp /source/korea.osm.pbf /data/korea.osm.pbf")
    with (out / "build.log").open("w") as log:
        for profile in PROFILES:
            container(args.volume, ["sh", "-ec", f"mkdir /data/{profile}; ln /data/korea.osm.pbf /data/{profile}/korea.osm.pbf"], writable=True)
            for command in (["osrm-extract", "--threads", "1", "--data_version", "osmosis", "-p", f"/opt/{profile}.lua", f"/data/{profile}/korea.osm.pbf"],
                            ["osrm-partition", "--threads", "1", f"/data/{profile}/korea.osrm"],
                            ["osrm-customize", "--threads", "1", f"/data/{profile}/korea.osrm"]):
                print(json.dumps({"stage": command[0], "profile": profile, "at": now()}), flush=True)
                build_stage(args.volume, profile, command, log, out)
            raw = container(args.volume, ["sha256sum", f"/opt/{profile}.lua"], capture_output=True, text=True).stdout
            manifest["profiles"][profile] = {"path": f"/opt/{profile}.lua", "sha256": raw.split()[0]}
    # osrm-extract creates fileIndex as root-only 0700. Runtime is nonroot and
    # the graph is public data; normalize ONLY this new build's runtime files.
    container(args.volume, ["chmod", "0644", *["/data/" + name for name in RUNTIME_FILES]], writable=True)
    manifest["runtime_file_mode"] = "0644"
    # Include profile libraries, exact image identity, and all generated file hashes.
    manifest["profile_libraries_sha256"] = container(args.volume, ["sh", "-ec", "sha256sum /opt/lib/*.lua"], capture_output=True, text=True).stdout.strip().splitlines()
    raw = container(args.volume, ["sh", "-ec", "cd /data; sha256sum foot/korea.osrm.* bicycle/korea.osrm.*"], capture_output=True, text=True).stdout
    all_hashes = {line.split()[1]: line.split()[0] for line in raw.splitlines()}
    raw = container(args.volume, ["sh", "-ec", "cd /data; stat -c '%n %s' foot/korea.osrm.* bicycle/korea.osrm.*"], capture_output=True, text=True).stdout
    all_sizes = {line.split()[0]: int(line.split()[1]) for line in raw.splitlines()}
    manifest["all_build_files"] = {n: {"bytes": all_sizes[n], "sha256": h} for n, h in all_hashes.items()}
    manifest["runtime_files"] = {n: manifest["all_build_files"][n] for n in RUNTIME_FILES}
    verify_manifest(manifest)
    archive = out / "osrm-runtime.tar.gz"
    with archive.with_suffix(".gz.partial").open("wb") as stream:
        container(args.volume, ["tar", "czf", "-", "-C", "/data", *RUNTIME_FILES], stdout=stream)
    archive.with_suffix(".gz.partial").rename(archive)
    manifest["archive"] = {"filename": archive.name, "bytes": archive.stat().st_size, "sha256": digest(archive)}
    manifest["completed_at"] = now()
    timestamps = re.findall(r"\[info\] timestamp: (\S+)", (out / "build.log").read_text())
    if len(timestamps) != 2 or len(set(timestamps)) != 1:
        raise ValueError("profile source timestamps are missing or inconsistent")
    manifest["source"]["data_timestamp"] = timestamps[0]
    verify_archive(archive, manifest)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"status": "built_and_verified", "output": str(out), "runtime_bytes": verify_manifest(manifest), "archive": manifest["archive"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    b = subs.add_parser("build")
    for name in ("source", "md5", "volume", "output"):
        b.add_argument("--" + name, required=True)
    b.add_argument("--source-volume", help="reuse only a checksum-verified source from an earlier build, mounted read-only")
    v = subs.add_parser("verify")
    v.add_argument("manifest", type=Path)
    v.add_argument("archive", type=Path)
    tree = subs.add_parser("verify-tree")
    tree.add_argument("manifest", type=Path)
    tree.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        try:
            build(args)
        except Exception as error:
            if Path(args.output).is_dir():
                (Path(args.output) / "failure.json").write_text(json.dumps({"status": "FAILED", "at": now(), "error_class": type(error).__name__}) + "\n")
            raise
    elif args.command == "verify":
        verify_archive(args.archive, json.loads(args.manifest.read_text()))
        print(json.dumps({"status": "verified"}))
    else:
        verify_tree(args.directory, json.loads(args.manifest.read_text()))
        print(json.dumps({"status": "verified", "files": len(RUNTIME_FILES)}))
