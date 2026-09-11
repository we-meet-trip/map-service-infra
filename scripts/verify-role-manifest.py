#!/usr/bin/env python3
"""Validate rendered role Compose JSON and host identity without contacting a server."""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlparse

DIGEST = re.compile(r"[^\s]+@sha256:[0-9a-f]{64}")


def require(condition, message):
    if not condition: raise ValueError(message)


def remote_https(url):
    p = urlparse(url)
    require(p.scheme == "https" and p.hostname and not p.username and not p.password, "remote management requires HTTPS")
    require("." in p.hostname and p.hostname not in {"localhost", "localhost.localdomain"}, "remote targets cannot use Docker/loopback DNS")
    try: require(not ipaddress.ip_address(p.hostname).is_loopback, "loopback target forbidden")
    except ValueError as error:
        if str(error) == "loopback target forbidden": raise


def validate_scrapes(config):
    jobs = config.get("scrape_configs", [])
    require(jobs, "remote monitoring targets required")
    for job in jobs:
        require(job.get("scheme") == "https", "remote scrape requires HTTPS")
        require(job.get("tls_config", {}).get("insecure_skip_verify", False) is False, "scrape certificate verification required")
        credentials = job.get("authorization", {}).get("credentials_file", "")
        require(credentials.startswith("/etc/prometheus/credentials/") and ".." not in credentials, "scrape file-based authorization required")
        require(job.get("static_configs"), "explicit host targets required")
        for group in job["static_configs"]:
            require(group.get("labels", {}).get("map_environment") in {"test", "prod"}, "scrape environment label required")
            require(group.get("targets"), "scrape target required")
            for target in group["targets"]: remote_https("https://" + target)
    return True


def validate(manifest, config, *, host_identity, deploy_account):
    require(set(manifest) == {"schema_version", "role", "host_identity", "deploy_account", "compose_sha256", "data_scope"}, "invalid role manifest")
    require(manifest["schema_version"] == 1, "invalid role version")
    require(manifest["host_identity"] == host_identity and manifest["deploy_account"] == deploy_account, "role host/account mismatch")
    require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,127}", host_identity or ""), "invalid role host identity")
    services = config.get("services", {})
    for service in services.values():
        require(DIGEST.fullmatch(service.get("image", "")), "role images must use immutable digests")
        require(not service.get("privileged") and not service.get("pid") == "host" and not service.get("cap_add") and not service.get("volumes_from"), "privileged role forbidden")
        for mount in service.get("volumes", []):
            require(not any(value in mount.get("source", "") for value in ("docker.sock", "/.ssh", "map-test", "map-prod")), "cross-role or host control mount forbidden")
        for port in service.get("ports", []):
            require(port.get("host_ip") == "127.0.0.1", "management ports must bind loopback")
    if manifest["role"] == "admin":
        require(manifest["data_scope"] == "control", "admin data scope mismatch")
        require(set(services) == {"control-postgres", "admin-migrate", "admin-api", "admin-web", "prometheus", "grafana"}, "invalid admin role services")
        runtime = services["admin-api"].get("environment", {})
        allowed_runtime = {"ADMIN_CONTROL_DATABASE_URL", "ADMIN_ENVIRONMENT", "ADMIN_TARGETS", "ADMIN_BOOTSTRAP_USER", "ADMIN_BOOTSTRAP_PASSWORD", "ADMIN_SESSION_COOKIE_SECURE", "ADMIN_SESSION_COOKIE_NAME", "ADMIN_SESSION_TTL_MIN", "ADMIN_CORS_ORIGINS", "ADMIN_LOGIN_MAX_ATTEMPTS", "ADMIN_LOGIN_WINDOW_SECONDS", "ADMIN_RUN_MIGRATIONS", "ADMIN_DATABASE_URL", "ADMIN_REDIS_URL", "DB_TIMEOUT_SEC", "MONITORING_PANELS"}
        require(set(runtime) <= allowed_runtime, "central runtime has unrelated serving credentials")
        require(runtime.get("ADMIN_CONTROL_DATABASE_URL"), "explicit control runtime DSN required")
        require(not runtime.get("ADMIN_CONTROL_MIGRATION_DATABASE_URL") and not runtime.get("ADMIN_DATABASE_URL"), "runtime migration/target fallback credential forbidden")
        require(str(runtime.get("ADMIN_RUN_MIGRATIONS")).lower() == "false", "runtime migration must be disabled")
        targets = json.loads(runtime.get("ADMIN_TARGETS", "{}"))
        require(runtime.get("ADMIN_ENVIRONMENT", "test") in targets, "default target must be explicit")
        for target in targets.values():
            require(not target.get("ADMIN_DATABASE_URL") and not target.get("ADMIN_REDIS_URL"), "central role uses target management APIs only")
            for name in ("USER_BASE_URL", "HUB_BASE_URL", "AGENT_BASE_URL"): remote_https(target.get(name, ""))
            ordinary = target.get("INTERNAL_SERVICE_TOKEN", "")
            dedicated = target.get("USER_ADMIN_INTERNAL_TOKEN", "")
            require(ordinary, "target API credential required")
            require(isinstance(dedicated, str) and dedicated.strip() and dedicated != ordinary, "distinct target User admin credential required")
            hub_dedicated = target.get("HUB_ADMIN_INTERNAL_TOKEN", "")
            require(isinstance(hub_dedicated, str) and hub_dedicated.strip() and hub_dedicated not in {ordinary, dedicated}, "distinct target Hub admin credential required")
        migration = services["admin-migrate"].get("environment", {})
        require(set(migration) == {"ADMIN_CONTROL_MIGRATION_DATABASE_URL"}, "migration receives only its own credential")
        require(migration.get("ADMIN_CONTROL_MIGRATION_DATABASE_URL"), "separate migration credential required")
        require(str(services["grafana"].get("environment", {}).get("GF_AUTH_ANONYMOUS_ENABLED")).lower() == "false", "anonymous Grafana forbidden")
    elif manifest["role"] == "learning":
        require(manifest["data_scope"] == "synthetic" and set(services) == {"dataset-worker"}, "real-user learning HOLD")
        worker = services["dataset-worker"]
        require(worker.get("entrypoint") == ["python", "/opt/map/dataset-worker.py"], "unapproved worker entrypoint")
        require(worker.get("command") == ["--job", "/input/job.json", "--dataset-root", "/input", "--output-root", "/output", "--program", "/opt/map/segment_stats.py"], "unapproved worker arguments")
        require("no-new-privileges:true" in worker.get("security_opt", []), "worker privilege escalation forbidden")
        require(worker.get("network_mode") == "none" and worker.get("read_only") is True, "worker network/filesystem isolation required")
        require(str(worker.get("user")) == "10001:10001" and worker.get("cap_drop") == ["ALL"], "worker privilege isolation required")
        require(set(worker.get("environment", {})) <= {"PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "MAP_LEARNING_HOLD"}, "worker environment contains forbidden credentials")
        require(str(worker.get("environment", {}).get("MAP_LEARNING_HOLD")).lower() == "true", "real-user learning HOLD")
        require(not worker.get("ports") and not worker.get("devices") and not worker.get("secrets"), "worker serving/host access forbidden")
        mounts = {item["target"]: item for item in worker.get("volumes", [])}
        require(set(mounts) == {"/input", "/output", "/opt/map/dataset-worker.py", "/opt/map/segment_stats.py"}, "invalid worker mounts")
        require(all(mounts[key].get("read_only") is True for key in mounts if key != "/output"), "worker inputs/programs must be read-only")
        require(int(worker.get("mem_limit", 0)) <= 512 * 1024 * 1024 and int(worker.get("mem_limit", 0)) > 0, "worker memory budget required")
        require(float(worker.get("cpus", 0)) <= 1 and float(worker.get("cpus", 0)) > 0 and worker.get("pids_limit", 0) == 64, "worker CPU/PID budget required")
    else: raise ValueError("unknown host role")
    return {"valid": True, "role": manifest["role"], "independent_host_provisioned": False}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--compose", type=Path, required=True)
    p.add_argument("--rendered", type=Path, required=True)
    p.add_argument("--scrape-config", type=Path)
    p.add_argument("--host-identity", required=True)
    p.add_argument("--deploy-account", required=True)
    args = p.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        require(hashlib.sha256(args.compose.read_bytes()).hexdigest() == manifest["compose_sha256"], "role compose checksum mismatch")
        if manifest["role"] == "admin":
            require(args.scrape_config, "admin remote scrape config required")
            validate_scrapes(json.loads(args.scrape_config.read_text()))
        result = validate(manifest, json.loads(args.rendered.read_text()), host_identity=args.host_identity, deploy_account=args.deploy_account)
    except Exception as error:
        print(json.dumps({"valid": False, "error_type": type(error).__name__}))
        return 1
    print(json.dumps(result))
    return 0

if __name__ == "__main__": raise SystemExit(main())
