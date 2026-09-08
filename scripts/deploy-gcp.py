#!/usr/bin/env python3
"""Trusted GitHub artifact receiver / fixed GCP test-server deployment command.

Runner: prepare --run-id ID --output DIR [--automatic]
Server: receive (JSON on stdin; root-only forced SSH command, no caller arguments).
Install this file and release_manifest.py together in /usr/local/lib/map-deploy.
No subprocess output, environment contents, credentials or request bodies are logged.
Trust boundary: the dedicated forced-command SSH key grants deployment authority.
The GitHub runner verifies publisher/artifact provenance; the server independently
checks bundle consistency, fixed registry/repository and target isolation. Possession
of this SSH key is equivalent to deployment permission, not a read-only capability.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
import zipfile

spec = importlib.util.spec_from_file_location("release_manifest", Path(__file__).resolve().with_name("release_manifest.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
guard_spec = importlib.util.spec_from_file_location("cutover_watchdog", Path(__file__).resolve().with_name("cutover_watchdog.py"))
cutover_guard = importlib.util.module_from_spec(guard_spec)
guard_spec.loader.exec_module(cutover_guard)

REPO = Path("/home/mapadmin26/map-service-infra")
STATE = Path("/var/lib/map-deploy")
BACKUP_ENV = Path("/etc/map-deploy/backup.env")
PROJECT = "mapcenter-b59ca"
ZONE = "us-central1-a"
INSTANCE = "map-test"
INSTANCE_ID = "2327348931395410137"
PUBLIC_URL = "https://mapapptest.duckdns.org"
# The published proxy port on this host. The private smoke and the
# rollover probes must name the same origin or one of them is checking
# something that is not there.
PRIVATE_ORIGIN = "http://127.0.0.1:8290"
MAX_PAYLOAD = 256 * 1024
PROCESS_TERM_GRACE_SECONDS = 5
ARTIFACT_FILES = (*release.FILES, "SHA256SUMS")
APP_SERVICES = ("user", "agent", "hub", "yolo", "proxy", "edge", "dns")
ADMIN_SERVICES = ("admin", "admin-web", "prometheus", "grafana", "postgres-exporter", "redis-exporter", "node-exporter", "cadvisor")
TARGET_EXPORTERS = ("postgres-exporter", "redis-exporter", "node-exporter")
ADMIN_DETACHED = False
DETACHED_MARKER = "# MAP_ADMIN_DETACHED_VERSION=1"
INFRASTRUCTURE = {
    "map-test": {"postgres": "postgis/postgis", "redis": "redis", "proxy": "nginx",
                 "edge": "caddy", "dns": "curlimages/curl"},
    "map-admin-test": {"prometheus": "prom/prometheus", "grafana": "grafana/grafana",
                       "postgres-exporter": "prometheuscommunity/postgres-exporter",
                       "redis-exporter": "oliver006/redis_exporter", "node-exporter": "prom/node-exporter"},
}
INFRA_BUNDLE_MARKER = "# MAP_INFRA_IMAGE_BUNDLE_VERSION=1"


class DeployError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise DeployError(message)


def status(phase):
    print(json.dumps({"phase": phase}), flush=True)


def guard_context():
    # Also works for importlib-loaded receiver regression fixtures.
    return SimpleNamespace(**globals())


PUBLIC_SERVICES = ("edge", "proxy", "user", "yolo")
CUTOVER_PHASES = {"starting_private", "rollover", "private_ready", "opening_ingress", "complete",
                  "quarantined", "quarantine_failed", "rolled_back", "rollback_failed_quarantined"}


def validate_host_metadata(info):
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
            and stat.S_IMODE(info.st_mode) in (0o600, 0o644), "unsafe root-owned host policy")



def validate_state_directory(info):
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o022 == 0,
            "unsafe deployment state directory")

def read_host_json(path):
    # O_NOFOLLOW + descriptor metadata avoids swapping a checked path for a symlink.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            validate_host_metadata(os.fstat(stream.fileno()))
            content = stream.read(65537)
        require(len(content) <= 65536, "host policy exceeds limit")
        return json.loads(content, object_pairs_hook=release.unique_object)
    except (OSError, ValueError, TypeError):
        raise DeployError("root-owned host policy missing or invalid") from None


def exact_image_tuple(value):
    require(isinstance(value, dict) and set(value) == set(release.SERVICES), "policy requires exactly six services")
    for service, image in value.items():
        require(isinstance(image, str) and re.fullmatch(
            rf"{re.escape(release.REGISTRY)}/map-service-{service}@sha256:[a-f0-9]{{64}}", image),
            "policy requires fixed repository and exact digest")
    return value


def candidate_images(data):
    return {service: data["services"][service]["image"] + "@" + data["services"][service]["digest"]
            for service in release.SERVICES}


def load_rollback_policy(candidate):
    policy = read_host_json(STATE / "rollback-policy.json")
    require(isinstance(policy, dict) and set(policy) ==
            {"schema_version", "instance_id", "candidate_allowed", "rollback_verified"}, "invalid rollback policy schema")
    require(type(policy["schema_version"]) is int and policy["schema_version"] == 1
            and policy["instance_id"] == INSTANCE_ID, "rollback policy instance mismatch")
    for key in ("candidate_allowed", "rollback_verified"):
        values = policy[key]
        require(isinstance(values, list) and len(values) <= 32, "invalid rollback policy list")
        unique = set()
        for value in values:
            exact_image_tuple(value)
            identity = json.dumps(value, sort_keys=True)
            require(identity not in unique, "duplicate rollback tuple")
            unique.add(identity)
    require(bool(policy["candidate_allowed"]) and exact_image_tuple(candidate) in policy["candidate_allowed"],
            "candidate is not explicitly allowed")
    return policy


def load_rollover_policy():
    """Whether this host replaces one service at a time instead of recreating all.

    A root-owned file is the switch, so turning it on is an operator act and
    removing the file is the complete undo. An absent file means the ordinary
    recreate path, which is what every earlier deployment used.
    """
    path = STATE / "rollover.json"
    if not path.exists() and not path.is_symlink():
        return False
    data = read_host_json(path)
    require(isinstance(data, dict) and set(data) == {"schema_version", "instance_id", "enabled"}
            and type(data["schema_version"]) is int and data["schema_version"] == 1
            and data["instance_id"] == INSTANCE_ID and type(data["enabled"]) is bool,
            "invalid rollover policy")
    return data["enabled"]


def load_cutover_latch():
    path = STATE / "security-cutover.json"
    if not path.exists() and not path.is_symlink():
        return None
    data = read_host_json(path)
    require(isinstance(data, dict) and set(data) == {"schema_version", "instance_id", "phase", "run_id",
            "infra_sha", "bundle", "candidate", "prior_rollback_compatible"}, "invalid cutover latch")
    require(type(data["schema_version"]) is int and data["schema_version"] == 1
            and data["instance_id"] == INSTANCE_ID and data["phase"] in CUTOVER_PHASES
            and type(data["prior_rollback_compatible"]) is bool
            and isinstance(data["run_id"], str) and re.fullmatch(r"[1-9][0-9]{0,19}", data["run_id"])
            and isinstance(data["infra_sha"], str) and release.SHA.fullmatch(data["infra_sha"])
            and isinstance(data["bundle"], str), "invalid cutover state")
    exact_image_tuple(data["candidate"])
    return data


def atomic_state(path, value):
    # State is outside the source checkout and must survive kill/reboot/rollback.
    fd, temporary = tempfile.mkstemp(prefix=".cutover-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prior_rollback_compatible(policy, previous, env):
    # These files were generated from the containers' actual .Image identities.
    # Resolve allowed registry digests LOCALLY to the same .Id field; no pull, tag,
    # label, registry/index-vs-config guess, or partial match can grant permission.
    identities = {}
    for filename in ("compose.images.yml", "compose.admin-images.yml"):
        path = previous / filename
        if path.is_file():
            identities.update(re.findall(r"^  ([a-z-]+):\n    build: !reset null\n    image: (sha256:[a-f0-9]{64})$",
                                         path.read_text(), flags=re.MULTILINE))
    if not set(release.SERVICES) <= identities.keys():
        return False
    for image_tuple in policy["rollback_verified"]:
        try:
            if all(command(["docker", "image", "inspect", "--format", "{{.Id}}", image_tuple[service]], env=env)
                   == identities[service] for service in release.SERVICES):
                return True
        except DeployError:
            continue
    return False


def running_service_ids(service, env):
    ids = command(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=map-test",
                   "--filter", f"label=com.docker.compose.service={service}"], env=env, cwd=STATE).splitlines()
    require(all(re.fullmatch(r"[a-f0-9]{12,64}", item) for item in ids), "invalid ingress container ID")
    return ids


def stop_public_services(env, services=PUBLIC_SERVICES):
    # Try every service even if one stop fails, then independently verify all.
    errors = []
    for service in services:
        try:
            ids = running_service_ids(service, env)
            if ids:
                command(["docker", "stop", "--time", "30", *ids], env=env, timeout=90, cwd=STATE)
        except Exception:
            errors.append(service)
    for service in services:
        try:
            if running_service_ids(service, env):
                errors.append(service)
        except Exception:
            errors.append(service)
    require(not errors, "public entrypoint stop or verification failed")


def verify_edge_closed(env):
    require(not running_service_ids("edge", env), "public entrypoint opened before private readiness")


@contextmanager
def topology_scope():
    """Root-owned host policy outlives every app checkout and application rollback."""
    global ADMIN_DETACHED
    previous = ADMIN_DETACHED
    path = STATE / "topology.json"
    try:
        ADMIN_DETACHED = False
        if path.exists() or path.is_symlink():
            info = path.lstat()
            require(path.is_file() and not path.is_symlink() and info.st_uid == 0
                    and info.st_mode & 0o022 == 0, "unsafe topology policy")
            data = json.loads(path.read_text())
            require(data.get("schema_version") == 1 and data.get("instance_id") == INSTANCE_ID
                    and data.get("mode") == "application", "invalid topology policy")
            evidence = data.get("verified_admin_handoff_sha256", "")
            require(bool(re.fullmatch("[a-f0-9]{64}", evidence)), "verified independent admin handoff required")
            # Kept separately from Git; reverting a release cannot resurrect retired control services.
            handoff = STATE / "admin-handoff.json"
            require(handoff.is_file() and not handoff.is_symlink()
                    and hashlib.sha256(handoff.read_bytes()).hexdigest() == evidence,
                    "admin handoff evidence mismatch")
            verified = json.loads(handoff.read_text())
            require(verified.get("status") == "PASS" and verified.get("application_instance_id") == INSTANCE_ID
                    and verified.get("central_instance_id") not in (None, "", INSTANCE_ID),
                    "independent administrator identity not verified")
            required_checks = {"control_auth", "target_read", "target_isolation", "browser_charts",
                               "audit_restore", "serving_survives_admin_failure"}
            require(all(verified.get("checks", {}).get(check) == "PASS" for check in required_checks),
                    "independent administrator acceptance incomplete")
            ADMIN_DETACHED = True
        yield
    finally:
        ADMIN_DETACHED = previous


def admin_services():
    return TARGET_EXPORTERS if ADMIN_DETACHED else ADMIN_SERVICES


def verify_detached_services(env):
    if not ADMIN_DETACHED:
        return
    require(DETACHED_MARKER in (REPO / "scripts/cloud-up.sh").read_text().splitlines()
            and (REPO / "docker-compose.target-exporters.yml").is_file(),
            "current release cannot safely roll back detached administrator topology")
    for service in ("admin", "admin-web", "prometheus", "grafana", "cadvisor"):
        running = command(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=map-admin-test",
                           "--filter", f"label=com.docker.compose.service={service}"], env=env)
        require(not running, "retired control service is still running on application host")


@contextmanager
def interruption_guard():
    watched = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = {sig: signal.getsignal(sig) for sig in watched}
    def interrupted(_signum, _frame):
        # A second termination signal must not interrupt group/ingress cleanup.
        for sig in watched:
            signal.signal(sig, signal.SIG_IGN)
        raise DeployError("deployment interrupted")
    try:
        for sig in watched:
            signal.signal(sig, interrupted)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=PROCESS_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # The parent can exit before children that ignored TERM or detached pipes.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()


def command_status(args, *, env=None, timeout=300, cwd=REPO, umask=-1, accept=(0,)):
    """Run one child and return its exit code with its output.

    A caller that can act on a particular non-zero code lists it in accept;
    everything else still ends the deployment exactly as before.
    """
    # A timed-out shell must not leave its compose/backup children racing rollback.
    process = subprocess.Popen(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True, umask=umask)
    try:
        output, _errors = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_process_group(process)
        raise DeployError("subprocess timed out and process group was stopped") from None
    except BaseException:
        stop_process_group(process)
        raise
    require(process.returncode in accept, "subprocess failed")
    return process.returncode, output.strip()


def command(args, **kwargs):
    return command_status(args, **kwargs)[1]


def git(*args):
    return command(["git", "-c", f"safe.directory={REPO}", "-c", "core.hooksPath=/dev/null",
                    "-C", str(REPO), *args], umask=0o022 if args and args[0] == "checkout" else -1)


def read_limited(response, limit):
    data = response.read(limit + 1)
    require(len(data) <= limit, "response exceeds limit")
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_get(path, token, *, archive=False):
    # The token is sent only to api.github.com; signed artifact redirects get no token.
    request = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            content = read_limited(response, 1024 * 1024)
    except urllib.error.HTTPError as error:
        if not archive or error.code not in (301, 302, 303, 307, 308):
            raise DeployError("GitHub request failed") from None
        location = error.headers.get("Location", "")
        require(location.startswith("https://"), "artifact redirect must use HTTPS")
        # Do not forward GitHub authorization to artifact storage.
        with urllib.request.urlopen(location, timeout=60) as response:
            content = read_limited(response, 1024 * 1024)
    return content if archive else json.loads(content, object_pairs_hook=release.unique_object)


def prepare(args):
    require(re.fullmatch(r"[1-9][0-9]{0,19}", args.run_id), "invalid release run ID")
    token = os.environ.get("GH_TOKEN", "")
    require(bool(token), "GitHub token is required")
    prefix = f"/repos/{release.REPOSITORY}/actions"
    run = github_get(f"{prefix}/runs/{args.run_id}", token)
    require(str(run.get("id")) == args.run_id and run.get("status") == "completed"
            and run.get("conclusion") == "success", "release run did not succeed")
    require(run.get("path") == release.WORKFLOW_PATH
            and run.get("head_repository", {}).get("full_name") == release.REPOSITORY,
            "unexpected release workflow/source repository")
    require(run.get("event") in ("workflow_dispatch", "workflow_run", "repository_dispatch"), "unexpected release event")
    workflow_sha = run.get("head_sha", "")
    require(bool(release.SHA.fullmatch(workflow_sha)), "invalid workflow SHA")
    listing = github_get(f"{prefix}/runs/{args.run_id}/artifacts?per_page=100", token)
    artifacts = [item for item in listing.get("artifacts", []) if item.get("name") == "release-manifest"]
    require(len(artifacts) == 1 and not artifacts[0].get("expired", True), "release artifact unavailable or ambiguous")
    artifact = artifacts[0]
    digest = artifact.get("digest", "")
    require(isinstance(digest, str) and release.DIGEST.fullmatch(digest), "artifact digest is required")
    require(type(artifact.get("id")) is int and artifact["id"] > 0, "invalid artifact ID")
    archive = github_get(f"{prefix}/artifacts/{artifact['id']}/zip", token, archive=True)
    require("sha256:" + hashlib.sha256(archive).hexdigest() == digest, "GitHub artifact digest mismatch")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        require(len(zipped.infolist()) == len(ARTIFACT_FILES)
                and set(zipped.namelist()) == set(ARTIFACT_FILES), "unexpected artifact files")
        for name in ARTIFACT_FILES:
            info = zipped.getinfo(name)
            require(info.file_size <= 65536 and not stat.S_ISLNK(info.external_attr >> 16), "invalid artifact member")
            (args.output / name).write_bytes(zipped.read(name))
    data = release.verify_bundle(args.output, expected_run_id=args.run_id,
                                 expected_workflow_sha=workflow_sha)
    require(data["provenance"]["event_name"] == run["event"], "release event provenance mismatch")
    if run["event"] == "repository_dispatch":
        # A self-consistent artifact alone must not turn an arbitrary dispatch into
        # automatic deployment. Recheck the source CI and reject superseded HEADs.
        release.verify_dispatch(data["provenance"]["dispatch"])
    if args.automatic:
        require(data["source_ref"] == "develop" and run["event"] in ("workflow_run", "repository_dispatch"), "automatic deployment requires develop CI release")
    payload = {"schema_version": 1, "expected_run_id": args.run_id,
               "files": {name: base64.b64encode((args.output / name).read_bytes()).decode()
                         for name in ARTIFACT_FILES}}
    encoded = json.dumps(payload).encode()
    require(len(encoded) <= MAX_PAYLOAD, "deployment payload too large")
    (args.output / "transport.json").write_bytes(encoded)
    status("artifact_verified")


def unpack_payload(raw, directory):
    require(len(raw) <= MAX_PAYLOAD, "deployment payload too large")
    payload = json.loads(raw, object_pairs_hook=release.unique_object)
    require(isinstance(payload, dict) and set(payload) == {"schema_version", "expected_run_id", "files"}, "invalid transport fields")
    require(type(payload["schema_version"]) is int and payload["schema_version"] == 1, "invalid transport schema")
    require(isinstance(payload["expected_run_id"], str) and re.fullmatch(r"[1-9][0-9]{0,19}", payload["expected_run_id"]), "invalid expected run ID")
    require(isinstance(payload["files"], dict) and set(payload["files"]) == set(ARTIFACT_FILES), "invalid transport files")
    for name in ARTIFACT_FILES:
        value = payload["files"][name]
        require(isinstance(value, str), "invalid encoded file")
        content = base64.b64decode(value, validate=True)
        require(len(content) <= 65536, "bundle file too large")
        (directory / name).write_bytes(content)
    return release.verify_bundle(directory, expected_run_id=payload["expected_run_id"])


def verify_instance():
    require(os.geteuid() == 0, "receiver requires root forced command")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path, expected in (("project/project-id", PROJECT), ("instance/name", INSTANCE),
                           ("instance/id", INSTANCE_ID), ("instance/zone", ZONE)):
        request = urllib.request.Request("http://169.254.169.254/computeMetadata/v1/" + path,
                                         headers={"Metadata-Flavor": "Google"})
        with opener.open(request, timeout=3) as response:
            require(response.headers.get("Metadata-Flavor") == "Google", "invalid metadata response")
            actual = read_limited(response, 1024).decode().strip()
        require(actual.rsplit("/", 1)[-1] == expected, "wrong GCP instance")


def backup_environment():
    require(BACKUP_ENV.is_file() and not BACKUP_ENV.is_symlink(), "backup configuration is required")
    metadata = BACKUP_ENV.stat()
    require(metadata.st_uid == 0 and not metadata.st_mode & 0o077, "backup configuration must be root-only")
    allowed = {"BACKUP_DIR", "BACKUP_REMOTE", "BACKUP_S3_ENDPOINT", "BACKUP_REQUIRE_REMOTE", "RETAIN_DAYS", "BACKUP_GCP_CREDENTIALS_FILE"}
    # Ubuntu GCP images install the Cloud SDK under the root-managed snap path.
    # Keep a fixed allowlist instead of inheriting an interactive deployment PATH.
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin", "HOME": "/root",
           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    for line in BACKUP_ENV.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        require(sep and key in allowed and key not in env, "invalid backup configuration")
        require("\x00" not in value, "invalid backup configuration value")
        env[key] = value
    env["BACKUP_REQUIRE_REMOTE"] = "1"
    require(bool(env.get("BACKUP_REMOTE")), "remote backup destination is required")
    return env


def updated_environment(original, tag):
    release.validate_tag(tag)
    values = {"IMAGE_REGISTRY": release.REGISTRY, "IMAGE_TAG": tag}
    lines = original.decode("utf-8").splitlines(keepends=True)
    for key, value in values.items():
        found = [index for index, line in enumerate(lines) if re.match(rf"^\s*{key}\s*=", line)]
        require(len(found) <= 1, "duplicate image environment key")
        if found:
            lines[found[0]] = f"{key}={value}\n"
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{key}={value}\n")
    return "".join(lines).encode("utf-8")


def replace_environment(path, content, metadata):
    fd, temporary = tempfile.mkstemp(prefix=".map-deploy-env-", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
        os.fchown(fd, metadata.st_uid, metadata.st_gid)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def compose_command(*, admin=False, bundle=None, env_file=None, infrastructure=None):
    files = (["docker-compose.admin.yml", "docker-compose.admin.test.yml", "docker-compose.admin.registry.yml"]
             if admin else ["docker-compose.yml", "docker-compose.test.yml", "docker-compose.registry.yml", "docker-compose.edge.yml"])
    if admin and ADMIN_DETACHED:
        files = ["docker-compose.target-exporters.yml"]
    args = ["docker", "compose", "--env-file", str(env_file or REPO / ".env.test")]
    for filename in files:
        args.extend(("-f", str(REPO / filename)))
    if bundle and not (admin and ADMIN_DETACHED):
        args.extend(("-f", str(bundle / ("compose.admin-images.yml" if admin else "compose.images.yml"))))
    if bundle and admin and ADMIN_DETACHED and (bundle / "compose.target-images.yml").is_file():
        args.extend(("-f", str(bundle / "compose.target-images.yml")))
    if infrastructure:
        args.extend(("-f", str(infrastructure / ("compose.admin-infrastructure.yml" if admin else "compose.infrastructure.yml"))))
    if not admin:
        args.extend(("-f", str(STATE / "public-restart.yml")))
    for profile in (("monitoring",) if admin else ("full", "vision", "edge", "dns")):
        args.extend(("--profile", profile))
    return args


def preflight(bundle, env_file, env, infrastructure=None):
    for admin in (False, True):
        config = json.loads(command(compose_command(admin=admin, bundle=bundle, env_file=env_file, infrastructure=infrastructure)
                                    + ["config", "--format", "json"], env=env))
        require(config.get("name") == ("map-admin-test" if admin else "map-test"), "wrong Compose project")
        require(all(str(value.get("name", "")).startswith("map-test_") for value in config.get("volumes", {}).values()), "non-test volume")
        require(config.get("networks", {}).get("default", {}).get("name") == "map-test-net", "non-test network")
        expected = (() if ADMIN_DETACHED else release.SERVICES[4:]) if admin else release.SERVICES[:4]
        if admin and ADMIN_DETACHED:
            require(set(config["services"]) == set(TARGET_EXPORTERS), "control service cannot return to application host")
        for service in expected:
            require(service in config["services"], "missing application service")
            image = config["services"][service].get("image", "")
            require(re.fullmatch(rf"{re.escape(release.REGISTRY)}/map-service-{service}@sha256:[0-9a-f]{{64}}", image), "unpinned application image")
        if not admin:
            require(all(config["services"].get(service, {}).get("restart") == "no" for service in PUBLIC_SERVICES),
                    "public services require supervised restart=no")
            for service in ("user", "agent", "hub"):
                values = config["services"][service].get("environment", {})
                require(str(values.get("AUTH_ENFORCED", "")).lower() == "true", "authentication must be enforced")
                if service in ("user", "agent"):
                    require(str(values.get("TRAINING_CAPTURE_ENABLED") or "false").lower() == "false", "training capture must remain on hold")
                    require(str(values.get("TRAINING_EXPORT_ENABLED") or "false").lower() == "false", "training export must remain on hold")
            require(str(config["services"]["hub"].get("environment", {}).get("PLACES_STUB_MODE") or "false").lower() == "false", "stub mode cannot be deployed")


def image_metadata(image, env):
    # Project only non-secret fields; never inspect container Config.Env or print raw output.
    template = '{"image_id":{{json .Id}},"os":{{json .Os}},"architecture":{{json .Architecture}},"repo_digests":{{json .RepoDigests}}}'
    value = json.loads(command(["docker", "image", "inspect", "--format", template, image], env=env))
    require(isinstance(value, dict) and bool(release.DIGEST.fullmatch(value.get("image_id", ""))), "invalid infrastructure image ID")
    require(value.get("os") == "linux" and value.get("architecture") == "amd64", "infrastructure requires linux/amd64 image")
    require(value.get("repo_digests") is None or isinstance(value.get("repo_digests"), list), "invalid image digest evidence")
    value["repo_digests"] = value.get("repo_digests") or []
    return value


def capture_infrastructure(env):
    """Capture old containers before checkout, including stopped existing services."""
    captured = {}
    for project, services in INFRASTRUCTURE.items():
        captured[project] = {}
        for service in services:
            if ADMIN_DETACHED and project == "map-admin-test" and service not in TARGET_EXPORTERS:
                continue
            ids = command(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                           "--filter", f"label=com.docker.compose.service={service}", "--format", "{{.ID}}"], env=env).splitlines()
            require(len(ids) <= 1, "multiple infrastructure containers for a service")
            if not ids:
                continue
            require(bool(re.fullmatch(r"[0-9a-f]{12,64}", ids[0])), "invalid infrastructure container ID")
            image = command(["docker", "inspect", "--format", "{{.Image}}", ids[0]], env=env)
            require(bool(release.DIGEST.fullmatch(image)), "invalid existing infrastructure image ID")
            metadata = image_metadata(image, env)
            require(metadata["image_id"] == image, "existing image identity mismatch")
            captured[project][service] = {**metadata, "container_id": ids[0]}
    require({"postgres", "redis"} <= captured["map-test"].keys(), "existing PostgreSQL and Redis are required; app deployment cannot provision databases")
    return captured


def canonical_repository(value):
    value = value.removeprefix("docker.io/")
    return value.removeprefix("library/") if "/" not in value.removeprefix("library/") else value


def prepare_infrastructure(directory, bundle, env_file, captured, env):
    """Pin infrastructure separately from the six intentionally updated app images."""
    require(INFRA_BUNDLE_MARKER in (REPO / "scripts/cloud-up.sh").read_text().splitlines(), "cloud-up lacks immutable infrastructure support")
    directory.mkdir(mode=0o700)
    evidence = {"schema_version": 1, "platform": "linux/amd64", "projects": {}}
    for admin in (False, True):
        project = "map-admin-test" if admin else "map-test"
        config = json.loads(command(compose_command(admin=admin, bundle=bundle, env_file=env_file)
                                    + ["config", "--format", "json"], env=env))
        require(config.get("name") == project, "wrong infrastructure Compose project")
        profiles = {"monitoring"} if admin else {"full", "vision", "edge", "dns"}
        selected = {name: item for name, item in config.get("services", {}).items()
                    if not item.get("profiles") or profiles.intersection(item["profiles"])}
        app_names = set(release.SERVICES[4:] if admin else release.SERVICES[:4])
        candidates = set(selected) - app_names
        require(candidates <= INFRASTRUCTURE[project].keys(), "unapproved infrastructure service")
        require(set(captured[project]) <= candidates, "existing infrastructure cannot be removed by app release")
        if not admin:
            require({"postgres", "redis"} <= candidates, "stateful infrastructure is required")
        evidence["projects"][project] = {}
        pins = ["services:"]
        for service in sorted(candidates):
            item = selected[service]
            requested = item.get("image", "")
            match = re.fullmatch(r"(.+?)(?::([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})|@(sha256:[0-9a-f]{64}))", requested)
            repository = INFRASTRUCTURE[project][service]
            require(match is not None and canonical_repository(match[1]) == repository, "unapproved infrastructure image repository")
            require(item.get("platform", "linux/amd64") == "linux/amd64", "wrong infrastructure platform")
            metadata = captured[project].get(service)
            if metadata is None:
                # Only newly installed, stateless/monitoring services may consult a tag.
                require(service not in ("postgres", "redis"), "database image pull is prohibited")
                require(service != "edge", "new edge requires reviewed artifact installation before automatic deployment")
                command(["docker", "pull", "--platform", "linux/amd64", requested], env=env, timeout=600)
                metadata = image_metadata(requested, env)
                digests = [value for value in metadata["repo_digests"] if isinstance(value, str)
                           and "@" in value and canonical_repository(value.split("@", 1)[0]) == repository
                           and release.DIGEST.fullmatch(value.split("@", 1)[1])]
                require(bool(digests), "new infrastructure requires matching registry digest evidence")
                if match[3]:
                    require(any(value.endswith("@" + match[3]) for value in digests), "pulled infrastructure digest mismatch")
                metadata = {**metadata, "repo_digests": sorted(digests)}
                mode = "new"
            else:
                mode = "preserved"
            evidence["projects"][project][service] = {**metadata, "requested_image": requested, "mode": mode}
            pins.extend((f"  {service}:", "    build: !reset null", f"    image: {metadata['image_id']}",
                         "    platform: linux/amd64", "    pull_policy: never"))
        filename = "compose.admin-infrastructure.yml" if admin else "compose.infrastructure.yml"
        (directory / filename).write_text("\n".join(pins) + "\n" if len(pins) > 1 else "services: {}\n")
    (directory / "images.json").write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    # Check the final merge, including that no application image was overwritten.
    for admin in (False, True):
        project = "map-admin-test" if admin else "map-test"
        config = json.loads(command(compose_command(admin=admin, bundle=bundle, env_file=env_file, infrastructure=directory)
                                    + ["config", "--format", "json"], env=env))
        for service, metadata in evidence["projects"][project].items():
            actual = config.get("services", {}).get(service, {})
            require(actual.get("image") == metadata["image_id"] and actual.get("pull_policy") == "never"
                    and actual.get("platform") == "linux/amd64" and not actual.get("build"), "infrastructure image override was not applied")
    return evidence


def verify_infrastructure_images(evidence, env):
    for project, services in evidence["projects"].items():
        for service, metadata in services.items():
            ids = command(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
                           "--filter", f"label=com.docker.compose.service={service}", "--format", "{{.ID}}"], env=env).splitlines()
            require(len(ids) == 1 and bool(re.fullmatch(r"[0-9a-f]{12,64}", ids[0])), "infrastructure did not start exactly once")
            if project == "map-test" and service in ("postgres", "redis"):
                require(ids[0] == metadata.get("container_id"), "stateful container was recreated by app release")
            image = command(["docker", "inspect", "--format", "{{.Image}}", ids[0]], env=env)
            require(image == metadata["image_id"], "running infrastructure image changed unexpectedly")


def snapshot_images(directory, env, *, include_stopped=False):
    active = {}
    for admin, candidates in ((False, APP_SERVICES), (True, admin_services())):
        # Query by labels so capturing an older deployment does not need new compose syntax.
        project = "map-admin-test" if admin else "map-test"
        pins = ["services:"]
        active[project] = []
        for service in candidates:
            ids = command(["docker", "ps", *(["-a"] if include_stopped else []), "--filter", f"label=com.docker.compose.project={project}",
                           "--filter", f"label=com.docker.compose.service={service}", "--format", "{{.ID}}"], env=env).splitlines()
            require(len(ids) <= 1, "multiple containers for a service")
            if not ids:
                continue
            image = command(["docker", "inspect", "--format", "{{.Image}}", ids[0]], env=env)
            require(bool(release.DIGEST.fullmatch(image)), "invalid prior image ID")
            # A local immutable image ID preserves legacy/local builds as well as registry pulls.
            pins.extend((f"  {service}:", "    build: !reset null", f"    image: {image}", "    pull_policy: never"))
            active[project].append(service)
        filename = ("compose.target-images.yml" if ADMIN_DETACHED else "compose.admin-images.yml") if admin else "compose.images.yml"
        (directory / filename).write_text("\n".join(pins) + "\n" if len(pins) > 1 else "services: {}\n")
    require("user" in active["map-test"], "existing BFF is required for safe automated rollback")
    return active


def verify_admin_rollback_compatibility(bundle, active, env):
    if "admin" not in active["map-admin-test"]:
        return
    # Do not bypass old entrypoints: an old Alembic cannot read an unknown new head.
    # Changing a head requires a separately verified rollback strategy before enabling it.
    ids = command(["docker", "ps", "-a", "-q", "--filter", "label=com.docker.compose.project=map-admin-test",
                   "--filter", "label=com.docker.compose.service=admin"], env=env).splitlines()
    require(len(ids) == 1, "previous admin unavailable")
    old_image = command(["docker", "inspect", "--format", "{{.Image}}", ids[0]], env=env)
    data = release.verify_bundle(bundle)
    entry = data["services"]["admin"]
    new_image = entry["image"] + "@" + entry["digest"]
    command(["docker", "pull", new_image], env=env, timeout=600)
    heads = []
    for image in (old_image, new_image):
        output = command(["docker", "run", "--rm", "--network", "none", "--pull", "never",
                          "--entrypoint", "alembic", image, "heads"], env=env)
        heads.append(set(re.findall(r"^([A-Za-z0-9_]+) \(head\)", output, flags=re.MULTILINE)))
    require(len(heads[0]) == 1 and heads[0] == heads[1], "admin migration change requires verified rollback compatibility")


PUBLIC_SMOKE_DEADLINE_SECONDS = 90
PUBLIC_PROBES = (("edge_health", "/healthz", 200), ("bff_ready", "/healthz/app", 200),
                 ("account_unauthorized", "/api/v1/users/me", 401))


class SmokeDeadline(DeployError):
    pass


class PublicProbeError(DeployError):
    def __init__(self, alias, code, kind, retryable):
        super().__init__("public readiness failed")
        self.alias, self.code, self.kind, self.retryable = alias, code, kind, retryable


def probe_status(phase, alias, code=None, kind="none"):
    # Fixed aliases and classifications only: never URL, body or exception text.
    print(json.dumps({"phase": phase, "alias": alias, "status": code, "error_kind": kind}), flush=True)


def remaining_smoke_time(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SmokeDeadline("public readiness deadline exceeded")
    return remaining


@contextmanager
def smoke_deadline(deadline):
    # urllib's socket timeout alone does not bound DNS resolution or a slow body.
    # This fixed Linux receiver runs in the main thread, without another alarm.
    require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), "another process deadline is active")
    previous = signal.getsignal(signal.SIGALRM)
    def expired(_signum, _frame):
        raise SmokeDeadline("public readiness deadline exceeded")
    try:
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, remaining_smoke_time(deadline))
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def http_response(url, timeout):
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status, read_limited(response, 65536)
    except urllib.error.HTTPError as error:
        try:
            return error.code, b""
        finally:
            error.close()


def http_status(url, expected, *, deadline=None):
    # Private probes retain their immediate-failure semantics; full smoke shares
    # the public deadline across its private recheck and every external attempt.
    timeout = min(10, remaining_smoke_time(deadline)) if deadline is not None else 10
    code, body = http_response(url, timeout)
    if deadline is not None:
        remaining_smoke_time(deadline)
    require(code == expected, "smoke response mismatch")
    return body


def network_failure(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        # A certificate that does not identify this host is never a timing
        # problem, so it fails immediately and is not waited out.
        return "tls_verification", False
    if isinstance(reason, ssl.SSLError):
        # A handshake that fails without a certificate verdict is what a freshly
        # started entry point answers for its first fraction of a second. Waiting
        # costs nothing: a genuinely broken listener keeps failing and still ends
        # the deployment when the readiness deadline expires.
        return "tls_error", True
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout", True
    if isinstance(reason, socket.gaierror):
        return ("dns_temporary", True) if reason.errno == socket.EAI_AGAIN else ("dns_error", False)
    if isinstance(reason, ConnectionError):
        return "connection", True
    if isinstance(reason, OSError) and reason.errno in {
            errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
            errno.ETIMEDOUT, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPIPE}:
        return "connection", True
    return "transport_error", False


def public_probe(alias, path, expected, deadline):
    try:
        code, body = http_response(PUBLIC_URL + path, min(10, remaining_smoke_time(deadline)))
    except (urllib.error.URLError, OSError) as error:
        kind, retryable = network_failure(error)
        raise PublicProbeError(alias, None, kind, retryable) from None
    remaining_smoke_time(deadline)
    if code != expected:
        transient = code in (502, 503, 504)
        kind = "upstream_unavailable" if transient else (
            "authentication_mismatch" if alias == "account_unauthorized" else "http_mismatch")
        raise PublicProbeError(alias, code, kind, transient)
    if alias == "bff_ready":
        try:
            ready = json.loads(body)
        except (ValueError, UnicodeError):
            raise PublicProbeError(alias, code, "invalid_readiness_body", False) from None
        if not isinstance(ready, dict) or ready.get("status") != "UP":
            raise PublicProbeError(alias, code, "readiness_mismatch", False)
    probe_status("public_probe_pass", alias, code)


def wait_public_readiness(deadline):
    delay = 0.25
    try:
        while True:
            remaining_smoke_time(deadline)
            try:
                # One complete round must pass; earlier successes do not mask a
                # later readiness regression or authorize a partially healthy edge.
                for alias, path, expected in PUBLIC_PROBES:
                    public_probe(alias, path, expected, deadline)
                remaining_smoke_time(deadline)
                return
            except PublicProbeError as error:
                probe_status("public_probe_retry" if error.retryable else "public_probe_failed",
                             error.alias, error.code, error.kind)
                if not error.retryable:
                    raise
                # Backoff follows a real failed probe; elapsed time never grants PASS.
                time.sleep(min(delay, remaining_smoke_time(deadline)))
                delay = min(delay * 2, 2)
    except SmokeDeadline:
        probe_status("public_probe_failed", "public_smoke", kind="deadline")
        raise


def smoke(*, include_public=True):
    deadline = time.monotonic() + PUBLIC_SMOKE_DEADLINE_SECONDS if include_public else None
    def check():
        base = PRIVATE_ORIGIN
        http_status(base + "/healthz", 200, deadline=deadline)
        body = http_status(base + "/healthz/app", 200, deadline=deadline)
        require(json.loads(body).get("status") == "UP", "BFF readiness failed")
        http_status(base + "/api/v1/users/me", 401, deadline=deadline)
        endpoints = [(8200, "/health/ready"), (8201, "/health/ready"), (8204, "/health")]
        if not ADMIN_DETACHED:
            endpoints.extend(((8202, "/health/ready"), (8203, "/")))
        for port, path in endpoints:
            http_status(f"http://127.0.0.1:{port}{path}", 200, deadline=deadline)
        if not ADMIN_DETACHED:
            http_status("http://127.0.0.1:8202/api/v1/auth/me", 401, deadline=deadline)
        if include_public:
            wait_public_readiness(deadline)
    if include_public:
        with smoke_deadline(deadline):
            check()
    else:
        check()


def rollback(old_sha, original_env, env_metadata, previous, active, current_bundle, env):
    status("rollback_started")
    # Stop only newly introduced services. Never down/rm/prune or touch DB volumes.
    for admin, candidates in ((False, APP_SERVICES), (True, admin_services())):
        project = "map-admin-test" if admin else "map-test"
        new = set(candidates) - set(active[project])
        for service in sorted(new):
            ids = command(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
                           "--filter", f"label=com.docker.compose.service={service}"], env=env).splitlines()
            if ids:
                require(all(re.fullmatch(r"[0-9a-f]{12,64}", item) for item in ids), "invalid container ID")
                command(["docker", "stop", *ids], env=env)
    git("checkout", "--detach", old_sha)
    replace_environment(REPO / ".env.test", original_env, env_metadata)
    for admin in (False, True):
        services = active["map-admin-test" if admin else "map-test"]
        if admin and ADMIN_DETACHED:
            require(set(services) <= set(TARGET_EXPORTERS), "rollback cannot restore retired control services")
        if services:
            command(compose_command(admin=admin, bundle=previous) + ["up", "-d", "--force-recreate", "--no-deps", "--no-build",
                    "--pull", "never", "--wait", "--wait-timeout", "180", *services], env=env, timeout=300)
    status("rollback_complete")


@contextmanager
def deployment_lock():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    validate_state_directory(STATE.lstat())
    fd = os.open(STATE / "deploy.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "a") as lock:
        validate_host_metadata(os.fstat(lock.fileno()))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("another deployment is in progress") from None
        yield


def receive(raw):
    os.umask(0o077)
    verify_instance()
    require(REPO.is_dir() and not REPO.is_symlink(), "fixed deployment repository unavailable")
    with deployment_lock(), topology_scope(), interruption_guard():
        cutover_guard.require_receiver_scope(guard_context())
        # A ready systemd supervisor and fixed root override are mandatory before
        # any checkout or serving mutation. Legacy/direct SSH commands fail here.
        cutover_guard.require_enrolled(guard_context())
        with tempfile.TemporaryDirectory(prefix="incoming-", dir=STATE) as incoming:
            bundle = Path(incoming)
            data = unpack_payload(raw, bundle)
            candidate = candidate_images(data)
            policy = load_rollback_policy(candidate)
            prior_latch = load_cutover_latch()
            rollover = load_rollover_policy()
            interrupted = prior_latch is not None and prior_latch["phase"] not in ("complete", "rolled_back")
            env = backup_environment()
            verify_detached_services(env)
            if interrupted:
                # Recover even the kill windows before the first edge stop or after
                # edge reopening. Never treat a pending attempt as a verified prior.
                if prior_latch["phase"] == "rollover" and cutover_guard.return_to_canonical(guard_context()):
                    status("interrupted_rollover_returned")
                else:
                    stop_public_services(env)
                    status("interrupted_cutover_closed")
            git("diff", "--quiet", "--")
            git("diff", "--cached", "--quiet", "--")
            old_sha = git("rev-parse", "HEAD")
            require(bool(release.SHA.fullmatch(old_sha)), "invalid previous infra SHA")
            env_path = REPO / ".env.test"
            require(env_path.is_file() and not env_path.is_symlink(), "test environment file is required")
            env_metadata, original_env = env_path.stat(), env_path.read_bytes()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            history = STATE / f"release-{data['github_run_id']}-{stamp}"
            history.mkdir(mode=0o700)
            previous = history / "previous"
            previous.mkdir(mode=0o700)
            (previous / "environment").write_bytes(original_env)
            (previous / "infra_sha").write_text(old_sha + "\n")
            active = snapshot_images(previous, env, include_stopped=interrupted)
            rollback_allowed = not interrupted and prior_rollback_compatible(policy, previous, env)
            status("verified_rollback_available" if rollback_allowed else "security_cutover_no_rollback")
            (previous / "active.json").write_text(json.dumps(active))
            captured_infrastructure = capture_infrastructure(env)
            (previous / "infrastructure.json").write_text(json.dumps(captured_infrastructure))
            candidate_env = history / "candidate.env"
            candidate_env.write_bytes(updated_environment(original_env, data["release_tag"]))
            new_bundle = history / "bundle"
            shutil.copytree(bundle, new_bundle)
            # Fetch a full validated SHA from a fixed public origin; do not trust repo remote config.
            status("source_fetch")
            git("fetch", "--no-tags", "--depth", "1", "https://github.com/we-meet-trip/map-service-infra.git", data["infra_sha"])
            started = False
            try:
                git("checkout", "--detach", data["infra_sha"])
                require(git("rev-parse", "HEAD") == data["infra_sha"], "infra checkout mismatch")
                status("preflight")
                if ADMIN_DETACHED:
                    require(DETACHED_MARKER in (REPO / "scripts/cloud-up.sh").read_text().splitlines(),
                            "release lacks detached administrator support")
                require("# MAP_CUTOVER_SUPERVISOR_VERSION=1" in (REPO / "scripts/cloud-up.sh").read_text().splitlines(),
                        "release lacks public restart supervisor contract")
                require("# MAP_USER_STANDALONE_MIGRATION_VERSION=1" in (REPO / "scripts/cloud-up.sh").read_text().splitlines(),
                        "release lacks standalone User migration contract")
                require("# MAP_SERVICE_MIGRATION_VERSION=1" in (REPO / "scripts/cloud-up.sh").read_text().splitlines(),
                        "release lacks per-service isolated migration contract")
                if rollover:
                    require("# MAP_ROLLOVER_VERSION=1" in (REPO / "scripts/cloud-up.sh").read_text().splitlines(),
                            "release lacks the one-service-at-a-time replacement contract")
                preflight(new_bundle, candidate_env, env)
                infrastructure = history / "infrastructure"
                evidence = prepare_infrastructure(infrastructure, new_bundle, candidate_env, captured_infrastructure, env)
                preflight(new_bundle, candidate_env, env, infrastructure=infrastructure)
                verify_admin_rollback_compatibility(new_bundle, active, env)
                if rollover:
                    # The entry point keeps serving throughout, so a release that would
                    # replace it needs the separate procedure. Refuse before anything
                    # on the machine has changed.
                    planned_edge = (evidence["projects"].get("map-test") or {}).get("edge")
                    running_edge = (captured_infrastructure.get("map-test") or {}).get("edge") or {}
                    require(planned_edge is None
                            or running_edge.get("image_id") == planned_edge.get("image_id"),
                            "edge replacement requires the separate approved procedure")
                status("prebackup")
                # Verified new backup implementation reads the unchanged prior test environment.
                command(["bash", "scripts/pg-backup.sh", "--test"], env=env, timeout=1800)
                replace_environment(env_path, candidate_env.read_bytes(), env_metadata)
                latch = {"schema_version": 1, "instance_id": INSTANCE_ID,
                         "phase": "rollover" if rollover else "starting_private",
                         "run_id": data["github_run_id"], "infra_sha": data["infra_sha"], "bundle": str(new_bundle),
                         "candidate": candidate, "prior_rollback_compatible": rollback_allowed}
                atomic_state(STATE / "security-cutover.json", latch)
                started = True
                # The latch precedes the first serving mutation. A pending latch on
                # retry never authorizes rollback, including a partially started candidate.
                if not rollover:
                    stop_public_services(env, ("edge",))
                status("rollover_started" if rollover else "deploy_private_started")
                role_args = ["--target-exporters"] if ADMIN_DETACHED else ["--admin", "--monitoring"]
                # cloud-up's explicit application list omits edge/dns without --edge.
                # Existing DNS remains running; all database/infrastructure pins remain.
                child = {**env, "RELEASE_BUNDLE": str(new_bundle), "INFRA_IMAGE_BUNDLE": str(infrastructure),
                         "CUTOVER_SUPERVISED": "1"}
                if rollover:
                    child["CUTOVER_ROLLOVER"] = "1"
                    child["ROLLOVER_PROBE_ORIGIN"] = PRIVATE_ORIGIN
                code, _ = command_status(["bash", "scripts/cloud-up.sh", "--test", "--registry", "--vision", *role_args],
                                         env=child, timeout=2400, accept=(0, 3))
                if code == 3:
                    # The service stack is up; only the console stack failed. Losing a
                    # dashboard must not close the public entry points.
                    status("console_degraded")
                if rollover:
                    require(running_service_ids("edge", env), "entry point must keep serving during replacement")
                else:
                    verify_edge_closed(env)
                cutover_guard.require_public_restart(guard_context())
                private_evidence = {**evidence, "projects": {
                    project: {service: entry for service, entry in entries.items() if service != "edge"}
                    for project, entries in evidence["projects"].items()}}
                verify_infrastructure_images(private_evidence, env)
                status("private_smoke")
                smoke(include_public=False)
                latch["phase"] = "private_ready"
                atomic_state(STATE / "security-cutover.json", latch)
                latch["phase"] = "opening_ingress"
                atomic_state(STATE / "security-cutover.json", latch)
                if not rollover:
                    command(compose_command(bundle=new_bundle, infrastructure=infrastructure)
                            + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "180", "edge"],
                            env=env, timeout=300)
                verify_infrastructure_images(evidence, env)
                status("smoke")
                smoke()
                cutover_guard.write_ready_receipt(guard_context(), latch, "complete")
                atomic_state(history / "result.json", {"status": "complete", "run_id": data["github_run_id"],
                             "infra_sha": data["infra_sha"], "prior_rollback_compatible": rollback_allowed,
                             "rollover": rollover, "console": "degraded" if code == 3 else "ok"})
                atomic_state(STATE / "current.json", {"bundle": str(new_bundle), "infrastructure": str(infrastructure), "run_id": data["github_run_id"]})
                latch["phase"] = "complete"
                atomic_state(STATE / "security-cutover.json", latch)
                # Success clears only the quarantine phase, NEVER the root policy.
                status("deploy_complete")
            except BaseException:
                if started:
                    if rollback_allowed:
                        try:
                            rollback(old_sha, original_env, env_metadata, previous, active, new_bundle, env)
                            smoke()
                            cutover_guard.write_ready_receipt(guard_context(), latch, "rolled_back")
                            latch["phase"] = "rolled_back"
                            atomic_state(STATE / "security-cutover.json", latch)
                            atomic_state(history / "result.json", {"status": "failed_rolled_back", "run_id": data["github_run_id"]})
                        except Exception:
                            latch["phase"] = "rollback_failed_quarantined"
                            try:
                                stop_public_services(env)
                            except Exception:
                                latch["phase"] = "quarantine_failed"
                            atomic_state(STATE / "security-cutover.json", latch)
                            atomic_state(history / "result.json", {"status": latch["phase"], "run_id": data["github_run_id"]})
                            status(latch["phase"])
                            raise DeployError("deployment and verified rollback failed; ingress quarantine attempted") from None
                    else:
                        # Keep candidate source/env/bundle; never restart unsafe prior code.
                        latch["phase"] = "quarantined"
                        try:
                            stop_public_services(env)
                        except Exception:
                            latch["phase"] = "quarantine_failed"
                        atomic_state(STATE / "security-cutover.json", latch)
                        atomic_state(history / "result.json", {"status": latch["phase"], "run_id": data["github_run_id"],
                                     "infra_sha": data["infra_sha"], "candidate_preserved": True})
                        status(latch["phase"])
                        raise DeployError("deployment failed; unverified rollback prohibited; ingress quarantine attempted") from None
                else:
                    git("checkout", "--detach", old_sha)
                    replace_environment(env_path, original_env, env_metadata)
                    status("predeploy_state_restored")
                raise DeployError("deployment failed; prior application state restored") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--run-id", required=True)
    prepare_parser.add_argument("--output", required=True, type=Path)
    prepare_parser.add_argument("--automatic", action="store_true")
    commands.add_parser("receive")
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            prepare(args)
        else:
            raw = sys.stdin.buffer.read(MAX_PAYLOAD + 1)
            require(len(raw) <= MAX_PAYLOAD, "deployment payload too large")
            receive(raw)
    except Exception as error:
        # No exception text/tracebacks: external tools may include credentials in failures.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
