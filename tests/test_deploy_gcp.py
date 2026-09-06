import base64
import copy
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deploy_gcp", ROOT / "scripts/deploy-gcp.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
fixture_spec = importlib.util.spec_from_file_location("manifest_tests", ROOT / "tests/test_release_manifest.py")
fixtures = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixtures)


class BundleFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        deploy.release.write_bundle(self.source, fixtures.fixture())
        self.payload = {"schema_version": 1, "expected_run_id": "123", "files": {
            name: base64.b64encode((self.source / name).read_bytes()).decode() for name in deploy.ARTIFACT_FILES}}
        self.target = self.root / "target"
        self.target.mkdir()


class PayloadTests(BundleFixture, unittest.TestCase):
    def test_payload_roundtrip(self):
        data = deploy.unpack_payload(json.dumps(self.payload).encode(), self.target)
        self.assertEqual(data["github_run_id"], "123")

    def test_arbitrary_path_is_rejected(self):
        self.payload["files"]["../../outside"] = "eA=="
        with self.assertRaises(deploy.DeployError):
            deploy.unpack_payload(json.dumps(self.payload).encode(), self.target)
        self.assertFalse((self.root / "outside").exists())

    def test_oversized_payload_and_unexpected_command_are_rejected(self):
        with self.assertRaises(deploy.DeployError):
            deploy.unpack_payload(b" " * (deploy.MAX_PAYLOAD + 1), self.target)
        self.payload["command"] = "arbitrary"
        with self.assertRaises(deploy.DeployError):
            deploy.unpack_payload(json.dumps(self.payload).encode(), self.target)

    def test_tampered_file_cannot_pass(self):
        self.payload["files"]["compose.images.yml"] = base64.b64encode(b"services: {}\n").decode()
        with self.assertRaises(ValueError):
            deploy.unpack_payload(json.dumps(self.payload).encode(), self.target)

    def test_environment_update_preserves_secret_values_and_other_files(self):
        original = b"# note\nPOSTGRES_PASSWORD=synthetic@p%25/word\nIMAGE_TAG=old\nOTHER=value"
        result = deploy.updated_environment(original, "release-123")
        self.assertIn(b"POSTGRES_PASSWORD=synthetic@p%25/word\n", result)
        self.assertIn(b"IMAGE_TAG=release-123\n", result)
        self.assertIn(b"OTHER=value\n", result)
        with self.assertRaises(deploy.DeployError):
            deploy.updated_environment(original + b"\nIMAGE_TAG=duplicate", "release-123")


class ArtifactTests(BundleFixture, unittest.TestCase):
    def github(self, data=None):
        if data:
            deploy.release.write_bundle(self.source, data)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            for name in deploy.ARTIFACT_FILES:
                zipped.writestr(name, (self.source / name).read_bytes())
        content = archive.getvalue()
        metadata = {
            "id": 123, "status": "completed", "conclusion": "success",
            "path": deploy.release.WORKFLOW_PATH, "head_repository": {"full_name": deploy.release.REPOSITORY},
            "head_sha": "b" * 40, "event": "workflow_dispatch",
        }
        artifact = {"id": 456, "name": "release-manifest", "expired": False,
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest()}
        def get(path, _token, archive=False):
            if archive:
                return content
            return {"artifacts": [artifact]} if "/artifacts?" in path else metadata
        return metadata, artifact, get

    def prepare(self, getter, automatic=False):
        args = SimpleNamespace(run_id="123", output=self.root / "verified", automatic=automatic)
        with patch.dict(os.environ, {"GH_TOKEN": "synthetic-token"}), patch.object(deploy, "github_get", getter):
            deploy.prepare(args)
        return args.output

    def test_successful_release_artifact_is_authenticated_and_packaged(self):
        _, _, getter = self.github()
        output = self.prepare(getter)
        self.assertTrue((output / "transport.json").is_file())
        deploy.unpack_payload((output / "transport.json").read_bytes(), self.target)

    def test_artifact_digest_mismatch_blocks_transport(self):
        _, artifact, getter = self.github()
        artifact["digest"] = "sha256:" + "e" * 64
        with self.assertRaisesRegex(deploy.DeployError, "digest mismatch"):
            self.prepare(getter)
        self.assertFalse((self.root / "verified/transport.json").exists())

    def test_failed_or_fork_run_is_rejected(self):
        for update in ({"conclusion": "failure"}, {"head_repository": {"full_name": "attacker/fork"}},
                       {"path": ".github/workflows/other.yml"}):
            metadata, _, getter = self.github()
            metadata.update(update)
            with self.subTest(update=update), self.assertRaises(deploy.DeployError):
                self.prepare(getter)

    def test_automatic_feature_branch_release_is_rejected(self):
        _, _, getter = self.github()
        with self.assertRaisesRegex(deploy.DeployError, "automatic deployment"):
            self.prepare(getter, automatic=True)

    def test_automatic_develop_ci_release_is_accepted(self):
        data = fixtures.fixture()
        data["source_ref"] = "develop"
        data["provenance"]["event_name"] = "workflow_run"
        metadata, _, getter = self.github(data)
        metadata["event"] = "workflow_run"
        self.prepare(getter, automatic=True)

    def test_verified_repository_dispatch_can_automatically_deploy(self):
        data = fixtures.dispatch_fixture()
        metadata, _, getter = self.github(data)
        metadata["event"] = "repository_dispatch"
        evidence = data["provenance"]["dispatch"]
        branch = {"ref": "refs/heads/develop", "object": {"sha": evidence["sha"]}}
        with patch.object(deploy.release, "public_github_json", side_effect=[fixtures.source_ci(evidence), branch]) as api:
            output = self.prepare(getter, automatic=True)
        self.assertEqual(api.call_count, 2)
        self.assertTrue((output / "transport.json").is_file())

    def test_dispatch_artifact_hash_does_not_bypass_source_ci_verification(self):
        data = fixtures.dispatch_fixture()
        metadata, _, getter = self.github(data)
        metadata["event"] = "repository_dispatch"
        failed = {**fixtures.source_ci(data["provenance"]["dispatch"]), "conclusion": "failure"}
        with patch.object(deploy.release, "public_github_json", return_value=failed):
            with self.assertRaisesRegex(ValueError, "did not succeed"):
                self.prepare(getter, automatic=True)
        self.assertFalse((self.root / "verified/transport.json").exists())

    def test_superseded_dispatch_artifact_is_rejected_even_for_manual_deploy(self):
        data = fixtures.dispatch_fixture()
        metadata, _, getter = self.github(data)
        metadata["event"] = "repository_dispatch"
        evidence = data["provenance"]["dispatch"]
        branch = {"ref": "refs/heads/develop", "object": {"sha": "f" * 40}}
        with patch.object(deploy.release, "public_github_json", side_effect=[fixtures.source_ci(evidence), branch]):
            with self.assertRaisesRegex(ValueError, "advanced"):
                self.prepare(getter)
        self.assertFalse((self.root / "verified/transport.json").exists())


class InfrastructureTests(BundleFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "scripts/cloud-up.sh").write_text(deploy.INFRA_BUNDLE_MARKER + "\n")
        self.directory = self.root / "infrastructure"
        self.present = {service for services in deploy.INFRASTRUCTURE.values() for service in services}
        self.calls = []
        self.configs = {}
        self.metadata = {}
        self.ids = {}
        fixture = fixtures.fixture()
        for project, infrastructure in deploy.INFRASTRUCTURE.items():
            admin = project == "map-admin-test"
            apps = deploy.release.SERVICES[4:] if admin else deploy.release.SERVICES[:4]
            services = {service: {"image": fixture["services"][service]["image"] + "@" + fixture["services"][service]["digest"],
                                  "environment": {"AUTH_ENFORCED": "true"}} for service in apps}
            for service, repository in infrastructure.items():
                digest = hashlib.sha256(service.encode()).hexdigest()
                self.ids[service] = digest[:12]
                self.metadata[service] = {"image_id": "sha256:" + digest, "os": "linux", "architecture": "amd64",
                                          "repo_digests": ["docker.io/" + repository + "@sha256:" + "d" * 64]}
                services[service] = {"image": repository + ":floating", "profiles": ["monitoring" if admin else "full"]}
            self.configs[project] = {"name": project, "services": services, "volumes": {}, "networks": {"default": {"name": "map-test-net"}}}
        # Disabled routing must neither be pulled nor injected into the override.
        self.configs["map-test"]["services"]["osrm-foot"] = {"image": "unused:tag", "profiles": ["routing"]}

    def docker(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ["docker", "ps"]:
            service = next(value.split("=", 2)[-1] for value in args if value.startswith("label=com.docker.compose.service="))
            return self.ids[service] if service in self.present else ""
        if args[:2] == ["docker", "inspect"]:
            service = next(service for service, container in self.ids.items() if container == args[-1])
            return self.metadata[service]["image_id"]
        if args[:3] == ["docker", "image", "inspect"]:
            service = next(service for project, services in deploy.INFRASTRUCTURE.items() for service, repository in services.items()
                           if args[-1] in (self.metadata[service]["image_id"], repository + ":floating"))
            return json.dumps(self.metadata[service])
        if args[:2] == ["docker", "pull"]:
            return ""
        if args[:2] == ["docker", "compose"]:
            admin = any(value.endswith("docker-compose.admin.yml") for value in args)
            project = "map-admin-test" if admin else "map-test"
            config = copy.deepcopy(self.configs[project])
            filename = "compose.admin-infrastructure.yml" if admin else "compose.infrastructure.yml"
            override = self.directory / filename
            if str(override) in args:
                for service, image in re.findall(r"^  ([a-z-]+):\n    build: !reset null\n    image: (sha256:[a-f0-9]{64})\n    platform: linux/amd64\n    pull_policy: never$", override.read_text(), re.MULTILINE):
                    config["services"][service].update(image=image, build=None, platform="linux/amd64", pull_policy="never")
            return json.dumps(config)
        self.fail("unexpected command")

    def prepare(self):
        with patch.object(deploy, "REPO", self.repo), patch.object(deploy, "command", side_effect=self.docker):
            captured = deploy.capture_infrastructure({})
            evidence = deploy.prepare_infrastructure(self.directory, self.source, self.root / "candidate.env", captured, {})
            deploy.preflight(self.source, self.root / "candidate.env", {}, infrastructure=self.directory)
        return evidence

    def test_all_existing_infrastructure_is_preserved_without_any_pull(self):
        evidence = self.prepare()
        self.assertFalse(any(args[:2] == ["docker", "pull"] for args in self.calls))
        self.assertEqual(sum(len(services) for services in evidence["projects"].values()), 10)
        for project, services in evidence["projects"].items():
            for service, item in services.items():
                self.assertEqual(item["image_id"], self.metadata[service]["image_id"])
                self.assertEqual(item["mode"], "preserved")
        for filename in ("compose.infrastructure.yml", "compose.admin-infrastructure.yml"):
            text = (self.directory / filename).read_text()
            for app in deploy.release.SERVICES:
                self.assertNotIn(f"  {app}:\n", text)
            self.assertNotIn("osrm", text)
            self.assertNotIn("cadvisor", text)
        self.assertEqual(json.loads((self.directory / "images.json").read_text()), evidence)
        inspections = [args for args in self.calls if args[:2] == ["docker", "ps"]]
        self.assertTrue(all("-a" in args for args in inspections))

    def test_first_monitoring_install_pulls_only_new_services_and_records_registry_evidence(self):
        self.present = set(deploy.INFRASTRUCTURE["map-test"])
        evidence = self.prepare()
        pulls = [args for args in self.calls if args[:2] == ["docker", "pull"]]
        self.assertEqual({args[-1] for args in pulls}, {repo + ":floating" for repo in deploy.INFRASTRUCTURE["map-admin-test"].values()})
        self.assertTrue(all(args[2:4] == ["--platform", "linux/amd64"] for args in pulls))
        for item in evidence["projects"]["map-admin-test"].values():
            self.assertEqual(item["mode"], "new")
            self.assertEqual(len(item["repo_digests"]), 1)
        self.assertEqual(evidence["projects"]["map-test"]["postgres"]["mode"], "preserved")

    def test_missing_database_blocks_before_any_pull(self):
        self.present.remove("redis")
        with self.assertRaisesRegex(deploy.DeployError, "PostgreSQL and Redis"):
            self.prepare()
        self.assertFalse(any(args[:2] == ["docker", "pull"] for args in self.calls))

    def test_multiple_existing_containers_are_rejected(self):
        self.ids["postgres"] += "\n" + "a" * 12
        with self.assertRaisesRegex(deploy.DeployError, "multiple infrastructure"):
            self.prepare()

    def test_existing_wrong_architecture_is_rejected(self):
        self.metadata["redis"]["architecture"] = "arm64"
        with self.assertRaisesRegex(deploy.DeployError, "linux/amd64"):
            self.prepare()
        self.assertFalse(any(args[:2] == ["docker", "pull"] for args in self.calls))

    def test_new_image_requires_digest_from_expected_repository(self):
        self.present.remove("grafana")
        self.metadata["grafana"]["repo_digests"] = ["attacker/grafana@sha256:" + "d" * 64]
        with self.assertRaisesRegex(deploy.DeployError, "registry digest evidence"):
            self.prepare()

    def test_new_image_wrong_architecture_is_rejected_after_pull(self):
        self.present.remove("grafana")
        self.metadata["grafana"]["architecture"] = "arm64"
        with self.assertRaisesRegex(deploy.DeployError, "linux/amd64"):
            self.prepare()

    def test_app_release_cannot_remove_existing_infrastructure(self):
        del self.configs["map-test"]["services"]["proxy"]
        with self.assertRaisesRegex(deploy.DeployError, "cannot be removed"):
            self.prepare()

    def test_new_or_renamed_infrastructure_repository_is_rejected(self):
        self.configs["map-admin-test"]["services"]["grafana"]["image"] = "attacker/grafana:latest"
        with self.assertRaisesRegex(deploy.DeployError, "unapproved infrastructure image"):
            self.prepare()
        self.assertFalse(any(args[:2] == ["docker", "pull"] for args in self.calls))

    def test_old_cloud_up_cannot_silently_ignore_infrastructure_pins(self):
        (self.repo / "scripts/cloud-up.sh").write_text("echo old\n")
        with self.assertRaisesRegex(deploy.DeployError, "immutable infrastructure support"):
            self.prepare()
        self.assertFalse(any(args[:2] == ["docker", "pull"] for args in self.calls))

    def test_infrastructure_override_follows_app_release_override(self):
        for admin in (False, True):
            args = deploy.compose_command(admin=admin, bundle=self.source, infrastructure=self.directory)
            files = [args[index + 1] for index, value in enumerate(args) if value == "-f"]
            self.assertEqual(Path(files[-1]).parent, self.directory)
            self.assertEqual(Path(files[-2]).parent, self.source)

    def test_running_image_identity_is_verified_without_tag_lookup(self):
        evidence = self.prepare()
        with patch.object(deploy, "command", side_effect=self.docker):
            deploy.verify_infrastructure_images(evidence, {})
            self.metadata["postgres"]["image_id"] = "sha256:" + "f" * 64
            with self.assertRaisesRegex(deploy.DeployError, "changed unexpectedly"):
                deploy.verify_infrastructure_images(evidence, {})

    def test_same_image_does_not_allow_database_container_recreation(self):
        evidence = self.prepare()
        self.ids["redis"] = "f" * 12
        with patch.object(deploy, "command", side_effect=self.docker):
            with self.assertRaisesRegex(deploy.DeployError, "stateful container was recreated"):
                deploy.verify_infrastructure_images(evidence, {})


class ReceiverTests(BundleFixture, unittest.TestCase):
    def scenario(self, failure=""):
        repo = self.root / "repo"
        repo.mkdir()
        env_path = repo / ".env.test"
        original = b"MAP_STACK_ENV=test\nIMAGE_TAG=old\nPOSTGRES_PASSWORD=synthetic-private\n"
        env_path.write_bytes(original)
        (repo / ".env.testyuy").write_text("preserve-existing-local-file")
        self.calls = []
        current_sha = ["f" * 40]
        def fake_git(*args):
            self.calls.append(("git", *args))
            if args == ("rev-parse", "HEAD"):
                return current_sha[0]
            if args[:2] == ("checkout", "--detach"):
                current_sha[0] = args[2]
            return ""
        def fake_command(args, **_kwargs):
            self.calls.append(tuple(args))
            if "scripts/cloud-up.sh" in args:
                self.deployment_env = _kwargs["env"]
            if args[:2] == ["docker", "ps"]:
                return "a" * 12 if "label=com.docker.compose.service=user" in args else ""
            if args[:2] == ["docker", "inspect"]:
                return "sha256:" + "e" * 64
            if failure == "backup" and "scripts/pg-backup.sh" in args:
                raise deploy.DeployError("synthetic-private")
            if failure == "admin" and "scripts/cloud-up.sh" in args:
                raise deploy.DeployError("synthetic-private")
            return ""
        def fake_smoke():
            if failure == "smoke":
                raise deploy.DeployError("synthetic-private")
        def fake_preflight(*args, **kwargs):
            if failure == "preflight":
                raise deploy.DeployError("synthetic-private")
        def fake_capture(*args):
            self.calls.append(("capture_infrastructure",))
            return {}
        def fake_prepare(directory, *args):
            self.calls.append(("prepare_infrastructure",))
            directory.mkdir()
            (directory / "images.json").write_text("{}")
            return {}
        def fake_verify(*args):
            if failure == "infrastructure":
                raise deploy.DeployError("synthetic-private")
        output = io.StringIO()
        old_umask = os.umask(0o077)
        try:
            with patch.object(deploy, "REPO", repo), patch.object(deploy, "STATE", self.root / "state"), \
                 patch.object(deploy, "verify_instance"), patch.object(deploy, "backup_environment", return_value={}), \
                 patch.object(deploy, "git", side_effect=fake_git), patch.object(deploy, "command", side_effect=fake_command), \
                 patch.object(deploy, "preflight", side_effect=fake_preflight), patch.object(deploy, "smoke", side_effect=fake_smoke), \
                 patch.object(deploy, "capture_infrastructure", side_effect=fake_capture), \
                 patch.object(deploy, "prepare_infrastructure", side_effect=fake_prepare), \
                 patch.object(deploy, "verify_infrastructure_images", side_effect=fake_verify), \
                 contextlib.redirect_stdout(output):
                if failure:
                    with self.assertRaises(deploy.DeployError):
                        deploy.receive(json.dumps(self.payload).encode())
                else:
                    deploy.receive(json.dumps(self.payload).encode())
        finally:
            os.umask(old_umask)
        self.assertNotIn("synthetic-private", output.getvalue())
        self.assertEqual((repo / ".env.testyuy").read_text(), "preserve-existing-local-file")
        if failure:
            self.assertEqual(env_path.read_bytes(), original)
            self.assertEqual(current_sha[0], "f" * 40)
        return output.getvalue()

    def test_success_requires_prebackup_deploy_and_smoke(self):
        output = self.scenario()
        self.assertIn("deploy_complete", output)
        backup = next(i for i, c in enumerate(self.calls) if "scripts/pg-backup.sh" in c)
        start = next(i for i, c in enumerate(self.calls) if "scripts/cloud-up.sh" in c)
        self.assertLess(backup, start)
        self.assertNotIn("rollback_started", output)
        captured = self.calls.index(("capture_infrastructure",))
        checkout = next(i for i, c in enumerate(self.calls) if c[:2] == ("git", "checkout"))
        prepared = self.calls.index(("prepare_infrastructure",))
        self.assertLess(captured, checkout)
        self.assertLess(prepared, backup)
        infrastructure = Path(self.deployment_env["INFRA_IMAGE_BUNDLE"])
        self.assertTrue((infrastructure / "images.json").is_file())
        current = json.loads((self.root / "state/current.json").read_text())
        self.assertEqual(current["infrastructure"], str(infrastructure))
        self.assertNotEqual(self.deployment_env["RELEASE_BUNDLE"], str(infrastructure))

    def test_preflight_failure_restores_source_without_starting_apps(self):
        output = self.scenario("preflight")
        self.assertIn("predeploy_state_restored", output)
        self.assertFalse(any("scripts/cloud-up.sh" in c for c in self.calls))

    def test_failed_prebackup_never_starts_application(self):
        self.scenario("backup")
        self.assertFalse(any("scripts/cloud-up.sh" in c for c in self.calls))

    def test_admin_start_failure_rolls_back_once_without_database_commands(self):
        output = self.scenario("admin")
        self.assertEqual(output.count('"rollback_started"'), 1)
        self.assertEqual(output.count('"rollback_complete"'), 1)
        for call in self.calls:
            self.assertNotIn("downgrade", call)
            self.assertNotIn("down", call)
            self.assertNotIn("prune", call)
        restore = [c for c in self.calls if "--pull" in c]
        self.assertEqual(len(restore), 1)
        self.assertIn("never", restore[0])
        self.assertIn("--force-recreate", restore[0])
        self.assertNotIn("postgres", restore[0])
        self.assertNotIn("redis", restore[0])

    def test_smoke_failure_rolls_back_once_and_remains_a_failed_deployment(self):
        output = self.scenario("smoke")
        self.assertEqual(output.count('"rollback_started"'), 1)
        self.assertNotIn("deploy_complete", output)

    def test_infrastructure_identity_failure_cannot_report_success(self):
        output = self.scenario("infrastructure")
        self.assertEqual(output.count('"rollback_started"'), 1)
        self.assertNotIn("deploy_complete", output)

    def test_only_git_checkout_relaxes_child_umask(self):
        with patch.object(deploy, "command") as run:
            deploy.git("checkout", "--detach", "a" * 40)
            self.assertEqual(run.call_args.kwargs["umask"], 0o022)
            deploy.git("fetch", "--no-tags", "origin", "a" * 40)
            self.assertEqual(run.call_args.kwargs["umask"], -1)
            deploy.git("diff", "--quiet", "--")
            self.assertEqual(run.call_args.kwargs["umask"], -1)

    def test_public_checkout_permissions_do_not_relax_private_parent_files(self):
        public = self.root / "tracked.yml"
        private = self.root / "private.env"
        old_umask = os.umask(0o077)
        try:
            deploy.command([sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('public')", str(public)],
                           cwd=self.root, umask=0o022)
            private.write_text("synthetic-private")
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(public.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)

    def test_changed_admin_migration_head_is_rejected_before_deployment(self):
        calls = []
        def fake(args, **_):
            calls.append(args)
            if args[:2] == ["docker", "ps"]:
                return "a" * 12
            if args[:2] == ["docker", "inspect"]:
                return "sha256:" + "e" * 64
            if args[-1] == "heads":
                return "0002_accounts (head)" if args[-2].startswith("sha256:") else "0003_permissions (head)"
            return ""
        with patch.object(deploy, "command", side_effect=fake):
            with self.assertRaisesRegex(deploy.DeployError, "rollback compatibility"):
                deploy.verify_admin_rollback_compatibility(self.source, {"map-admin-test": ["admin"]}, {})
        heads = [args for args in calls if args[-1] == "heads"]
        self.assertEqual(len(heads), 2)
        self.assertTrue(all("none" in args and "--network" in args for args in heads))

    def test_timeout_kills_child_process_group_before_returning(self):
        pidfile = self.root / "child.pid"
        heartbeat = self.root / "heartbeat"
        child = ("import signal,time,sys; from pathlib import Path\n"
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                 "counter=0\n"
                 "while True:\n"
                 " Path(sys.argv[1]).write_text(str(counter)); counter+=1; time.sleep(0.01)\n")
        parent = (
            "import subprocess,sys,time; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[3]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
        )
        with patch.object(deploy, "PROCESS_TERM_GRACE_SECONDS", 0.05):
            with self.assertRaisesRegex(deploy.DeployError, "process group"):
                deploy.command([sys.executable, "-c", parent, str(pidfile), child, str(heartbeat)], timeout=0.5, cwd=self.root)
        # The child ignored TERM and detached its pipes, so unchanged output after
        # return demonstrates the whole group was stopped without relying on ps.
        self.assertGreater(int(pidfile.read_text()), 0)
        stopped_at = heartbeat.read_bytes()
        time.sleep(0.1)
        self.assertEqual(heartbeat.read_bytes(), stopped_at)


if __name__ == "__main__":
    unittest.main()
