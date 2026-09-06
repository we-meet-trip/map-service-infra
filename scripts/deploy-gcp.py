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
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

spec = importlib.util.spec_from_file_location("release_manifest", Path(__file__).resolve().with_name("release_manifest.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

REPO = Path("/home/mapadmin26/map-service-infra")
STATE = Path("/var/lib/map-deploy")
BACKUP_ENV = Path("/etc/map-deploy/backup.env")
PROJECT = "mapcenter-b59ca"
ZONE = "us-central1-a"
INSTANCE = "map-test"
INSTANCE_ID = "2327348931395410137"
PUBLIC_URL = "https://mapapptest.duckdns.org"
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


def command(args, *, env=None, timeout=300, cwd=REPO, umask=-1):
    # A timed-out shell must not leave its compose/backup children racing rollback.
    process = subprocess.Popen(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True, umask=umask)
    try:
        output, _errors = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=PROCESS_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        # The shell can exit before a child that ignores TERM, even if that child
        # redirected its pipes. Always kill the remaining group, not only the shell.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise DeployError("subprocess timed out and process group was stopped") from None
    require(process.returncode == 0, "subprocess failed")
    return output.strip()


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


def snapshot_images(directory, env):
    active = {}
    for admin, candidates in ((False, APP_SERVICES), (True, admin_services())):
        # Query by labels so capturing an older deployment does not need new compose syntax.
        project = "map-admin-test" if admin else "map-test"
        pins = ["services:"]
        active[project] = []
        for service in candidates:
            ids = command(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
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
    ids = command(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=map-admin-test",
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


def http_status(url, expected):
    # No redirects: an unexpected login redirect must not pass a health check.
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(url, timeout=10) as response:
            code = response.status
            body = read_limited(response, 65536)
    except urllib.error.HTTPError as error:
        code, body = error.code, b""
    require(code == expected, "smoke response mismatch")
    return body


def smoke():
    for base in ("http://127.0.0.1:8290", PUBLIC_URL):
        http_status(base + "/healthz", 200)
        body = http_status(base + "/healthz/app", 200)
        require(json.loads(body).get("status") == "UP", "BFF readiness failed")
        http_status(base + "/api/v1/users/me", 401)
    endpoints = [(8200, "/health/ready"), (8201, "/health/ready"), (8204, "/health")]
    if not ADMIN_DETACHED:
        endpoints.extend(((8202, "/health/ready"), (8203, "/")))
    for port, path in endpoints:
        http_status(f"http://127.0.0.1:{port}{path}", 200)
    if not ADMIN_DETACHED:
        http_status("http://127.0.0.1:8202/api/v1/auth/me", 401)


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
    with (STATE / "deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("another deployment is in progress") from None
        yield


def receive(raw):
    os.umask(0o077)
    verify_instance()
    require(REPO.is_dir() and not REPO.is_symlink(), "fixed deployment repository unavailable")
    with deployment_lock(), topology_scope():
        with tempfile.TemporaryDirectory(prefix="incoming-", dir=STATE) as incoming:
            bundle = Path(incoming)
            data = unpack_payload(raw, bundle)
            env = backup_environment()
            verify_detached_services(env)
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
            active = snapshot_images(previous, env)
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
                preflight(new_bundle, candidate_env, env)
                infrastructure = history / "infrastructure"
                evidence = prepare_infrastructure(infrastructure, new_bundle, candidate_env, captured_infrastructure, env)
                preflight(new_bundle, candidate_env, env, infrastructure=infrastructure)
                verify_admin_rollback_compatibility(new_bundle, active, env)
                status("prebackup")
                # Verified new backup implementation reads the unchanged prior test environment.
                command(["bash", "scripts/pg-backup.sh", "--test"], env=env, timeout=1800)
                replace_environment(env_path, candidate_env.read_bytes(), env_metadata)
                started = True
                status("deploy_started")
                role_args = ["--target-exporters"] if ADMIN_DETACHED else ["--admin", "--monitoring"]
                command(["bash", "scripts/cloud-up.sh", "--test", "--registry", "--vision", "--edge", *role_args],
                        env={**env, "RELEASE_BUNDLE": str(new_bundle), "INFRA_IMAGE_BUNDLE": str(infrastructure)}, timeout=2400)
                verify_infrastructure_images(evidence, env)
                status("smoke")
                smoke()
                (history / "result.json").write_text(json.dumps({"status": "complete", "run_id": data["github_run_id"], "infra_sha": data["infra_sha"]}))
                (STATE / "current.json").write_text(json.dumps({"bundle": str(new_bundle), "infrastructure": str(infrastructure), "run_id": data["github_run_id"]}))
                status("deploy_complete")
            except Exception:
                # Before application mutation, restoring checkout/env is enough. After it,
                # one application rollback is attempted; the deployment still reports failed.
                if started:
                    try:
                        rollback(old_sha, original_env, env_metadata, previous, active, new_bundle, env)
                    except Exception:
                        status("rollback_failed")
                        raise DeployError("deployment and application rollback failed") from None
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
