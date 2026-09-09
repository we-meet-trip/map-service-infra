#!/usr/bin/env python3
"""Local NCP production backup writer, independent of the GCP administrator.

Root installs a private identity-pinned configuration after production enrollment.
Closed PG/Redis outputs are encrypted before NCP upload. No existing data, older
backups, cloud resources, or services are deleted or recreated by this runner.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg_backup as pg
import redis_backup as redis
import backup_job


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


host = module("ncp_backup_host", "ncp-bootstrap-host.py")
transfer = module("ncp_backup_transfer", "ncp-bootstrap-backup.py")
DATA = Path("/srv/map-prod")
CONFIG = DATA / "secrets/backup.json"
ENROLLMENT = Path("/var/lib/map-bootstrap/enrollment.json")
ID = re.compile(r"[a-f0-9]{64}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def require(ok, code):
    if not ok:
        raise transfer.BackupError(code)


def configuration(path=CONFIG):
    value = transfer.read_json(path, private=True)
    require(set(value) == {"schema_version", "environment", "project", "enrollment_sha256",
                           "postgres", "redis", "remote", "age_recipient"}, "invalid_backup_configuration")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
            value["environment"] == "prod" and value["project"] == "map-prod", "production_configuration_required")
    require(isinstance(value["enrollment_sha256"], str) and ID.fullmatch(value["enrollment_sha256"]), "enrollment_pin_required")
    for service in ("postgres", "redis"):
        expected = {"container_id", "image_id"} | ({"user", "database"} if service == "postgres" else set())
        entry = value[service]
        require(isinstance(entry, dict) and set(entry) == expected, "invalid_source_contract")
        require(isinstance(entry["container_id"], str) and ID.fullmatch(entry["container_id"]) and
                isinstance(entry["image_id"], str) and IMAGE.fullmatch(entry["image_id"]), "exact_source_identity_required")
    require(value["postgres"]["container_id"] != value["redis"]["container_id"], "source_identity_reuse")
    require(all(isinstance(value["postgres"][key], str) and IDENTIFIER.fullmatch(value["postgres"][key])
                for key in ("user", "database")), "database_identifiers_required")
    transfer.remote_prefix(value["remote"], "prod")
    require(isinstance(value["age_recipient"], str) and
            re.fullmatch(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}", value["age_recipient"]), "age_recipient_required")
    return value


def private_directory(path):
    path = transfer.clean_path(path, exists=False)
    path.mkdir(mode=0o700, exist_ok=True)
    meta = path.stat()
    require(meta.st_uid == os.geteuid() and stat.S_IMODE(meta.st_mode) == 0o700, "private_state_directory_required")
    return path


def lock_file(path):
    transfer.clean_path(path, exists=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    stream = os.fdopen(fd, "a")
    meta = os.fstat(stream.fileno())
    if not (stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and meta.st_uid == os.geteuid()
            and stat.S_IMODE(meta.st_mode) == 0o600):
        stream.close()
        raise transfer.BackupError("untrusted_backup_lock")
    return stream


class Backend:
    def verify_host(self, config):
        enrollment = transfer.read_json(ENROLLMENT, private=True)
        host.validate(enrollment)
        host.require_current_topology(enrollment)
        require(host.sha(json.dumps(enrollment, sort_keys=True, separators=(",", ":")).encode()) ==
                config["enrollment_sha256"], "backup_enrollment_mismatch")
        host.verify(enrollment)

    def source(self, config, service):
        pin = config[service]
        # Deliberately omit Env, command arguments, mounts and raw logs.
        fmt = ('{"Id":{{json .Id}},"Image":{{json .Image}},'
               '"State":{"Running":{{json .State.Running}}},'
               '"HostConfig":{"Memory":{{json .HostConfig.Memory}}},'
               '"Config":{"Labels":{"com.docker.compose.project":{{json (index .Config.Labels "com.docker.compose.project")}},'
               '"com.docker.compose.service":{{json (index .Config.Labels "com.docker.compose.service")}}}}}')
        item = json.loads(redis.run(["docker", "inspect", "--format", fmt, pin["container_id"]]))
        labels = item["Config"]["Labels"]
        require(item["Id"] == pin["container_id"] and item["Image"] == pin["image_id"] and
                item["State"]["Running"] is True and labels.get("com.docker.compose.project") == "map-prod" and
                labels.get("com.docker.compose.service") == service, "production_source_drift")
        return item

    def collect(self, kind, config, item, directory):
        if kind == "redis":
            with redirect_stdout(io.StringIO()):
                return redis.backup("prod", item=item, closed_directory=directory)
        entry = config["postgres"]
        command = ["docker", "exec", item["Id"]]
        size = redis.run(command + ["psql", "-XAt", "-U", entry["user"], "-d", entry["database"],
                                    "-c", "SELECT pg_database_size(current_database())"])
        require(size.isdecimal() and int(size) * 8 + 2 * 1024**3 <= shutil.disk_usage(directory).free,
                "postgres_backup_capacity_low")
        roles, data = directory / "postgres.roles.sql.gz", directory / "postgres.sql.gz"
        created = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        pg.dump_gzip(command + ["pg_dumpall", "--roles-only", "--no-role-passwords", "-U", entry["user"]], roles)
        pg.dump_gzip(command + ["pg_dump", "--clean", "--if-exists", "-U", entry["user"], "-d", entry["database"]], data)
        value = {"version": 1, "environment": "prod", "database": entry["database"],
                 "created_at": created, "roles_have_passwords": False, "table_row_counts": pg.dump_row_counts(data),
                 "files": [{"name": path.name, "sha256": pg.checksum(path)} for path in (roles, data)]}
        path = directory / "postgres.helper.json"
        transfer.write_json(path, value)
        return path

    def upload(self, snapshot, config, kind):
        return transfer.ncp_upload(snapshot, "prod", config["remote"] + "/" + kind,
                                   str(DATA / "secrets/NCP_BACKUP_CREDENTIALS"), config["age_recipient"])


def closed_contract(kind, helper_path, source_id):
    helper = transfer.read_json(helper_path)
    if kind == "pg":
        created = dt.datetime.strptime(helper["created_at"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).isoformat()
    else:
        created = helper["snapshot_at"]
    value = {"schema_version": 1, "role": "prod", "environment": "prod", "source_id": source_id,
             "source": "pg_backup.py" if kind == "pg" else "redis_backup.py", "created_at": created,
             "format": "pg-logical-gzip-v1" if kind == "pg" else "redis-rdb-v1",
             "consistency": "closed-logical-backup", "helper_manifest": helper_path.name,
             "helper_manifest_sha256": transfer.digest(helper_path),
             "files": [{"name": item["name"], "sha256": item["sha256"],
                        "bytes": (helper_path.parent / item["name"]).stat().st_size} for item in helper["files"]]}
    if kind == "pg":
        value["database"] = helper["database"]
    path = helper_path.parent / "closed-contract.json"
    transfer.write_json(path, value)
    return path


def write_status(state, kind, code, *, receipt=None):
    state = transfer.clean_path(state)
    metadata = state.stat()
    require(state.is_dir() and metadata.st_uid == os.geteuid() and
            stat.S_IMODE(metadata.st_mode) == 0o700, "private_state_directory_required")
    path = state / (kind + "-backup-status.json")
    old = transfer.read_json(path, private=True) if path.exists() else {}
    now = dt.datetime.now(dt.timezone.utc)
    value = {"code": code, "success": receipt is not None, "last_attempt_at": now.isoformat(),
             "last_success_at": now.isoformat() if receipt else old.get("last_success_at"),
             "snapshot_at": receipt["source_created_at"] if receipt else old.get("snapshot_at"),
             "transport_sha256": receipt["transport_sha256"] if receipt else old.get("transport_sha256"),
             "remote": receipt["remote"] if receipt else old.get("remote")}
    try:
        value["rpo_1h_overdue"] = (now - transfer.utc(value["snapshot_at"])).total_seconds() > 3600
    except Exception:
        value["rpo_1h_overdue"] = True
    host.atomic_json(path, value)
    return value


def execute(kind, config, *, data=DATA, backend=None, lock_wait=150):
    require(kind in ("pg", "redis"), "invalid_backup_kind")
    backend = backend or Backend()
    backend.verify_host(config)  # No state is created on an unenrolled host.
    state = private_directory(data / "deploy")
    # Same acquisition order as the production receiver. No GCP lock or service
    # is queried, so losing the central administrator cannot stop local backups.
    with ExitStack() as stack:
        for filename in ("deploy.lock", "backup.lock"):
            lock = stack.enter_context(lock_file(state / filename))
            if not backup_job.acquire(lock, lock_wait):
                return write_status(state, kind, "LOCK_BUSY")
        backend.verify_host(config)
        service = "postgres" if kind == "pg" else "redis"
        item = backend.source(config, service)
        backups = private_directory(data / "backups")
        directory = Path(tempfile.mkdtemp(prefix=kind + "-", dir=backups))
        helper = backend.collect(kind, config, item, directory)
        contract = closed_contract(kind, helper, item["Id"])
        snapshot = directory / "closed"
        transfer.snapshot(contract, "prod", snapshot)
        receipt = backend.upload(snapshot, config, kind)
        require(receipt.get("ncp_remote_verified") is True and receipt.get("transport") == "ncp-kr-age-encrypted" and
                receipt.get("role") == "prod" and ID.fullmatch(receipt.get("transport_sha256", "")),
                "remote_backup_evidence_incomplete")
        require(receipt.get("source_created_at") == transfer.read_json(contract)["created_at"] and
                re.fullmatch(re.escape(config["remote"] + "/" + kind + "/") + r"[a-f0-9]{32}", receipt.get("remote", "")),
                "remote_backup_identity_mismatch")
        transfer.write_json(directory / "remote-receipt.json", receipt)
        return write_status(state, kind, "COMPLETE", receipt=receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("pg", "redis"))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        require(os.geteuid() == 0, "root_runner_required")
        result = execute(args.kind, configuration())
        print(json.dumps({key: result[key] for key in ("code", "success", "rpo_1h_overdue")}))
        return 0 if result["success"] or (result["code"] == "LOCK_BUSY" and not result["rpo_1h_overdue"]) else 1
    except Exception:
        # No arbitrary exception text, credential paths, or helper output.
        try:
            write_status(DATA / "deploy", args.kind, "BACKUP_FAILED")
        except Exception:
            pass
        print(json.dumps({"code": "BACKUP_FAILED", "success": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
