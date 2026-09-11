#!/usr/bin/env python3
"""Explicit-environment backup bundles and isolated PostgreSQL restore checks."""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent


class BackupError(RuntimeError):
    pass


def run(args, **kwargs):
    result = subprocess.run(args, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        raise BackupError(f"{Path(args[0]).name} operation failed (exit {result.returncode})")
    return result


def compose(environment):
    path = ROOT / (".env.test" if environment == "test" else ".env")
    values = {}
    # Read only nonsecret identifiers. Never execute/source a dotenv file.
    for line in path.read_text().splitlines():
        match = re.match(r"^(POSTGRES_USER|POSTGRES_DB)=(.*)$", line)
        if match:
            value = match[2].strip().strip("\"'")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
                raise BackupError("invalid database identifier")
            values[match[1]] = value
    if set(values) != {"POSTGRES_USER", "POSTGRES_DB"}:
        raise BackupError("database user/name must be explicit in environment file")
    cmd = ["docker", "compose", "--env-file", str(path), "-f", str(ROOT / "docker-compose.yml")]
    if environment == "test":
        cmd += ["-f", str(ROOT / "docker-compose.test.yml")]
    return cmd, values["POSTGRES_USER"], values["POSTGRES_DB"]


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def dump_gzip(args, path):
    partial = path.with_suffix(path.suffix + ".part")
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        with gzip.open(partial, "wb") as out:
            shutil.copyfileobj(process.stdout, out)
        process.stdout.close()
        if process.wait():
            raise BackupError("database dump failed")
        partial.replace(path)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        partial.unlink(missing_ok=True)


def upload(files, remote, endpoint=""):
    # Manifest is copied last. Any copy/verification error fails the job.
    if remote.startswith("gs://"):
        if not re.fullmatch(r"gs://[a-z0-9][a-z0-9.-]+(?:/[A-Za-z0-9_./-]*)?", remote) or '..' in remote.split('/'):
            raise BackupError("invalid GCS destination")
        gcloud = ["gcloud"]
        credential = os.environ.get("BACKUP_GCP_CREDENTIALS_FILE", "")
        if credential:
            gcloud += ["--credential-file-override=" + credential]
        for path in files:
            destination = remote.rstrip('/') + '/' + path.name
            run(gcloud + ["storage", "cp", "--quiet", str(path), destination], stdout=subprocess.DEVNULL)
            # Hash the downloaded object without buffering/printing user data.
            process = subprocess.Popen(gcloud + ["storage", "cat", destination],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            digest = hashlib.sha256()
            try:
                for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
                    digest.update(block)
                process.stdout.close()
                if process.wait() or digest.hexdigest() != checksum(path):
                    raise BackupError("remote GCS backup checksum mismatch")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
        return
    if remote.startswith("s3://"):
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?", endpoint):
            raise BackupError("explicit HTTPS BACKUP_S3_ENDPOINT required")
        if not re.fullmatch(r"s3://[a-z0-9][a-z0-9.-]+(?:/[A-Za-z0-9_./-]*)?", remote) or '..' in remote.split('/'):
            raise BackupError("invalid S3 destination")
        for path in files:
            destination = remote.rstrip("/") + "/" + path.name
            run(["aws", "--endpoint-url", endpoint, "s3", "cp", str(path), destination,
                 "--only-show-errors"], stdout=subprocess.DEVNULL)
            bucket, _, key = destination[5:].partition("/")
            result = run(["aws", "--endpoint-url", endpoint, "s3api", "head-object",
                          "--bucket", bucket, "--key", key, "--query", "ContentLength",
                          "--output", "text"], stdout=subprocess.PIPE, text=True)
            if result.stdout.strip() != str(path.stat().st_size):
                raise BackupError("remote backup size mismatch")
            # Multipart ETag and ContentLength cannot establish byte integrity.
            # Stream the actual remote object, and publish the manifest only after
            # each preceding data object has passed this check.
            process = subprocess.Popen(
                ["aws", "--endpoint-url", endpoint, "s3", "cp", destination, "-",
                 "--only-show-errors"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            digest = hashlib.sha256()
            try:
                for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
                    digest.update(block)
                process.stdout.close()
                if process.wait() or digest.hexdigest() != checksum(path):
                    raise BackupError("remote S3 backup checksum mismatch")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
        return
    match = re.fullmatch(r"([A-Za-z0-9_.-]+@[A-Za-z0-9.-]+):(/[A-Za-z0-9_./-]+)", remote)
    if not match or ".." in match[2].split("/"):
        raise BackupError("SCP destination must be user@host:/absolute/directory")
    host, directory = match.groups()
    for path in files:
        run(["scp", "-q", "-oBatchMode=yes", str(path), f"{host}:{directory}/{path.name}"],
            stdout=subprocess.DEVNULL)
        result = run(["ssh", "-oBatchMode=yes", host, f"sha256sum '{directory}/{path.name}'"],
                     stdout=subprocess.PIPE, text=True)
        if result.stdout.split()[0:1] != [checksum(path)]:
            raise BackupError("remote backup checksum mismatch")


def verified_bundle(manifest, environment):
    meta = json.loads(manifest.read_text())
    if meta.get("version") != 1 or meta.get("environment") != environment:
        raise BackupError("backup environment/version mismatch")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", meta.get("database", "")):
        raise BackupError("invalid backup database")
    entries = meta.get("files", [])
    if len(entries) != 2:
        raise BackupError("roles and database dumps are both required")
    paths = []
    for entry in entries:
        name = entry.get("name", "")
        if not re.fullmatch(r"map-[A-Za-z0-9_.-]+\.sql\.gz", name):
            raise BackupError("invalid artifact name")
        path = manifest.parent / name
        if path.is_symlink() or not path.is_file() or checksum(path) != entry.get("sha256"):
            raise BackupError("backup checksum verification failed")
        paths.append(path)
    if not paths[0].name.endswith(".roles.sql.gz") or paths[1].name.endswith(".roles.sql.gz"):
        raise BackupError("backup roles/data ordering mismatch")
    return meta, paths


def dump_row_counts(path):
    """Count COPY records without retaining or printing any record content."""
    counts, table = {}, None
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if table is not None:
                if line.rstrip("\n") == r"\.":
                    table = None
                else:
                    counts[table] += 1
                continue
            match = re.fullmatch(r'COPY ([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*) \(.*\) FROM stdin;\n?', line)
            if match:
                table = match[1] + "." + match[2]
                counts[table] = 0
    if table is not None:
        raise BackupError("unterminated COPY data in dump")
    # Extension-owned tables such as spatial_ref_sys are seeded by CREATE
    # EXTENSION; pg_dump intentionally exports only their custom records.
    # Compare complete application tables, not the extension's partial COPY.
    return {name: count for name, count in counts.items()
            if name.split('.', 1)[0] in {'hub_data', 'user_service', 'admin_data'}}


def backup(environment):
    cmd, user, database = compose(environment)
    directory = Path(os.environ.get("BACKUP_DIR", str(Path.home() / "backups" / environment)))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    days = int(os.environ.get("RETAIN_DAYS", "7"))
    if days < 1:
        raise BackupError("RETAIN_DAYS must be positive")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"map-{database}-{stamp}-{uuid.uuid4().hex[:8]}"
    data, roles = directory / f"{stem}.sql.gz", directory / f"{stem}.roles.sql.gz"
    manifest = directory / f"{stem}.manifest.json"
    try:
        dump_gzip(cmd + ["exec", "-T", "postgres", "pg_dumpall", "--roles-only",
                         "--no-role-passwords", "-U", user], roles)
        dump_gzip(cmd + ["exec", "-T", "postgres", "pg_dump", "--clean", "--if-exists",
                         "-U", user, "-d", database], data)
        meta = {"version": 1, "environment": environment, "database": database,
                "created_at": stamp, "roles_have_passwords": False,
                "table_row_counts": dump_row_counts(data),
                "files": [{"name": p.name, "sha256": checksum(p)} for p in (roles, data)]}
        partial = manifest.with_suffix(".part")
        partial.write_text(json.dumps(meta, indent=2) + "\n")
        partial.replace(manifest)
        remote = os.environ.get("BACKUP_REMOTE", "")
        if remote:
            upload([roles, data, manifest], remote, os.environ.get("BACKUP_S3_ENDPOINT", ""))
        elif os.environ.get("BACKUP_REQUIRE_REMOTE") == "1":
            raise BackupError("remote backup required but not configured")
        print(json.dumps({"backup": "complete", "environment": environment,
                          "manifest": str(manifest), "remote_verified": bool(remote)}))
        # Retain only our validated bundles; legacy/unrelated files are untouched.
        for old in directory.glob("map-*.manifest.json"):
            if old.stat().st_mtime < time.time() - days * 86400:
                old_meta = json.loads(old.read_text())
                if old_meta.get("environment") != environment:
                    continue
                _, paths = verified_bundle(old, environment)
                for path in paths:
                    path.unlink()
                old.unlink()
        return manifest
    except Exception:
        if not manifest.exists():
            data.unlink(missing_ok=True)
            roles.unlink(missing_ok=True)
        raise


def restore(environment, manifest):
    meta, paths = verified_bundle(manifest, environment)
    token = uuid.uuid4().hex[:16]
    box, user = "map-restore-check-" + token, "restore_" + token
    bootstrap_db = "bootstrap_" + token
    started, created = time.monotonic(), False
    try:
        # Unique superuser avoids duplicate source-role CREATE failures. No live
        # volume, host path, published port or application network is attached.
        run(["docker", "run", "-d", "--name", box, "--network", "none",
             "--platform", "linux/amd64", "--memory", "1g", "--cpus", "1",
             "-e", f"POSTGRES_USER={user}", "-e", f"POSTGRES_DB={bootstrap_db}",
             "-e", "POSTGRES_PASSWORD=isolated-restore-only",
             os.environ.get("POSTGRES_IMAGE", "postgis/postgis:17-3.5")], stdout=subprocess.DEVNULL)
        created = True
        sql = ["docker", "exec", "-i", box, "psql", "-h", "127.0.0.1", "-v",
               "ON_ERROR_STOP=1", "-v", "VERBOSITY=sqlstate", "-q", "-U", user, "-d", bootstrap_db]
        for attempt in range(90):
            result = subprocess.run(sql + ["-c", "SELECT 1"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            if not result.returncode:
                break
            if attempt == 89:
                raise BackupError("isolated restore readiness timed out")
            time.sleep(1)
        # The PostGIS image populates its bootstrap database with extensions that
        # may not exist in the source (e.g. topology). Restore into template0 so a
        # source DROP EXTENSION postgis does not hit bootstrap-only dependencies.
        run(sql + ["-c", f'DROP DATABASE IF EXISTS "{meta["database"]}"'], stdout=subprocess.DEVNULL)
        run(sql + ["-c", f'CREATE DATABASE "{meta["database"]}" TEMPLATE template0'], stdout=subprocess.DEVNULL)
        sql[-1] = meta["database"]
        for index, path in enumerate(paths):
            with tempfile.TemporaryFile() as error_output:
                process = subprocess.Popen(sql + ["-f", "-"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                           stderr=error_output)
                try:
                    try:
                        with gzip.open(path, "rb") as source:
                            shutil.copyfileobj(source, process.stdin)
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                    if process.wait():
                        error_output.seek(0)
                        # psql VERBOSITY=sqlstate suppresses query/user data. Print codes only.
                        error_text = error_output.read().decode(errors="replace")
                        codes = re.findall(r"(?:ERROR|FATAL):\s+([A-Z0-9]{5})", error_text)
                        lines = re.findall(r"(?:<stdin>|stdin):([0-9]+):", error_text)
                        stage = "roles" if index == 0 else "database"
                        raise BackupError(f"isolated restore {stage} SQL failed; SQLSTATE={','.join(codes) or 'unavailable'}; lines={','.join(lines) or 'unavailable'}")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait()
        result = run(sql + ["-Atc", "SELECT table_schema || ':' || count(*) "
                          "FROM information_schema.tables WHERE table_type='BASE TABLE' "
                          "AND table_schema IN ('hub_data','user_service','admin_data') "
                          "GROUP BY table_schema ORDER BY table_schema"], stdout=subprocess.PIPE, text=True)
        counts = dict(line.rsplit(":", 1) for line in result.stdout.splitlines())
        if any(int(counts.get(s, 0)) < 1 for s in ("hub_data", "user_service")):
            raise BackupError("required application schemas missing after restore")
        expected_counts = meta.get("table_row_counts", {})
        for table, expected in expected_counts.items():
            if not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*", table) or not isinstance(expected, int) or expected < 0:
                raise BackupError("invalid table row-count contract")
            schema, name = table.split(".")
            result = run(sql + ["-Atc", f'SELECT count(*) FROM "{schema}"."{name}"'], stdout=subprocess.PIPE, text=True)
            if result.stdout.strip() != str(expected):
                raise BackupError(f"restored table row count mismatch: {table}, expected={expected}, actual={result.stdout.strip()}")
        print(json.dumps({"restore": "complete", "environment": environment,
                          "verified_table_row_counts": len(expected_counts),
                          "schema_table_counts": counts,
                          "elapsed_seconds": round(time.monotonic() - started, 2),
                          "scope": "database only; application/key recovery requires separate rehearsal"}))
    finally:
        if created:
            run(["docker", "rm", "-f", "-v", box], stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["backup", "restore"])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true")
    group.add_argument("--prod", action="store_true")
    parser.add_argument("manifest", nargs="?", type=Path)
    args = parser.parse_intermixed_args()
    os.umask(0o077)
    try:
        environment = "test" if args.test else "prod"
        if args.operation == "backup":
            backup(environment)
        else:
            if args.manifest is None:
                parser.error("restore requires an explicit manifest path")
            restore(environment, args.manifest)
    except (BackupError, OSError, ValueError, KeyError) as exc:
        # Avoid emitting subprocess output, credentials or SQL data on failure.
        message = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
        print(f"[pg-backup] {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
