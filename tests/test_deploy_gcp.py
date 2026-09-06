import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
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
        def fake_preflight(*args):
            if failure == "preflight":
                raise deploy.DeployError("synthetic-private")
        output = io.StringIO()
        old_umask = os.umask(0o077)
        try:
            with patch.object(deploy, "REPO", repo), patch.object(deploy, "STATE", self.root / "state"), \
                 patch.object(deploy, "verify_instance"), patch.object(deploy, "backup_environment", return_value={}), \
                 patch.object(deploy, "git", side_effect=fake_git), patch.object(deploy, "command", side_effect=fake_command), \
                 patch.object(deploy, "preflight", side_effect=fake_preflight), patch.object(deploy, "smoke", side_effect=fake_smoke), \
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
