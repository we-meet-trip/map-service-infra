#!/usr/bin/env python3
"""Render reviewable host units for one verified routing release; do not install.

Copy the rendered units/config only after SHA256, resource and rollback checks.
Serving app receivers use a separate project and never own this mount lifecycle.
"""
import argparse
from pathlib import Path
import re


def render(environment, release_dir, infra_dir, network, foot_port, bicycle_port):
    if environment not in ("test", "prod"):
        raise ValueError("environment must be test or prod")
    for path in (release_dir, infra_dir):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) or ".." in Path(path).parts:
            raise ValueError("simple absolute host paths required")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", network) or (environment == "prod" and "test" in network.lower()):
        raise ValueError("invalid or mixed serving network")
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in (foot_port, bicycle_port)) or foot_port == bicycle_port:
        raise ValueError("distinct unprivileged private ports required")
    mountpoint = "/srv/map-osrm-" + environment
    mount_unit = "srv-map\\x2dosrm\\x2d" + environment + ".mount"
    command = (f"/usr/bin/docker compose --project-name map-routing-{environment} "
               f"--env-file {release_dir}/runtime.env --file {infra_dir}/docker-compose.osrm-runtime.yml")
    return {
        mount_unit: f"""[Unit]
Description=Verified immutable MAP OSRM graph ({environment})
Before=map-osrm-{environment}.service

[Mount]
What={release_dir}/osrm-runtime.squashfs
Where={mountpoint}
Type=squashfs
Options=loop,ro,nodev,nosuid,noexec
TimeoutSec=60
""",
        f"map-osrm-{environment}.service": f"""[Unit]
Description=MAP foot and bicycle OSRM ({environment})
Requires=docker.service {mount_unit}
After=docker.service {mount_unit}
BindsTo={mount_unit}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/usr/bin/python3 {infra_dir}/scripts/osrm-release.py verify-tree {release_dir}/manifest.json {mountpoint}
ExecStart={command} up --detach --wait --wait-timeout 120 --pull never --no-build
ExecStop={command} stop --timeout 15
TimeoutStartSec=300
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
""",
        "runtime.env": f"""MAP_ENVIRONMENT={environment}
OSRM_MOUNT_ROOT={mountpoint}
OSRM_NETWORK={network}
OSRM_FOOT_PORT={foot_port}
OSRM_BICYCLE_PORT={bicycle_port}
""",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("test", "prod"), required=True)
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--infra-dir", required=True)
    parser.add_argument("--network", required=True)
    parser.add_argument("--foot-port", type=int, required=True)
    parser.add_argument("--bicycle-port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    units = render(args.environment, args.release_dir, args.infra_dir, args.network, args.foot_port, args.bicycle_port)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, content in units.items():
        (args.output / name).write_text(content)
    print("Rendered mount, routing service and environment; host unchanged.")
