#!/usr/bin/env python3
"""Create a complete, immutable compressed MLD runtime from a verified build.

Only the new build volume is read; source/intermediate files stay there. No
serving volume, mount, or existing artifact is modified. Kernel SquashFS allows
osrm-routed mmap to read the compressed artifact without an extracted copy.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess

spec = importlib.util.spec_from_file_location("osrm_release", Path(__file__).with_name("osrm-release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
TOOL_IMAGE = "alpine@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    release.verify_manifest(manifest)
    release.verify_archive(directory / manifest["archive"]["filename"], manifest)
    volume = manifest["build_volume"]
    if not release.re.fullmatch(r"map-(?:test-)?osrm-build-[a-z0-9-]+", volume):
        raise ValueError("unexpected build volume")
    artifact = directory / "osrm-runtime.squashfs"
    if artifact.exists() or artifact.with_suffix(".squashfs.partial").exists():
        raise ValueError("existing artifact preserved")
    excluded = sorted(set(manifest["all_build_files"]) - set(release.RUNTIME_FILES))
    excluded += ["korea.osm.pbf", "foot/korea.osm.pbf", "bicycle/korea.osm.pbf"]
    (directory / "squashfs-excludes.txt").write_text("\n".join(excluded) + "\n")
    command = """set -eu
apk add --no-cache squashfs-tools
mksquashfs -version | head -n 1 > /out/squashfs-tool-version.txt
mksquashfs /data /out/osrm-runtime.squashfs.partial -comp zstd -b 131072 -noappend -all-root -no-progress -processors 2 -mem 256M -ef /out/squashfs-excludes.txt
unsquashfs -s /out/osrm-runtime.squashfs.partial
"""
    with (directory / "squashfs-build.log").open("w") as log:
        subprocess.run(["docker", "run", "--rm", "--memory", "512m", "--memory-swap", "512m", "--cpus", "2",
                        "--cap-drop", "ALL", "-v", f"{volume}:/data:ro", "-v", f"{directory}:/out",
                        TOOL_IMAGE, "sh", "-c", command], stdout=log, stderr=subprocess.STDOUT, check=True)
    artifact.with_suffix(".squashfs.partial").rename(artifact)
    manifest["squashfs"] = {"filename": artifact.name, "sha256": release.digest(artifact), "bytes": artifact.stat().st_size,
                             "tool_image": TOOL_IMAGE, "tool_version": (directory / "squashfs-tool-version.txt").read_text().strip(),
                             "compression": "zstd", "block_bytes": 131072, "created_at": release.now()}
    temporary = directory / "manifest.json.partial"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(directory / "manifest.json")
    print(json.dumps({"status": "compressed", "squashfs": manifest["squashfs"], "runtime_bytes": release.verify_manifest(manifest)}))


if __name__ == "__main__":
    main()
