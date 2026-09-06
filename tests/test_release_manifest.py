import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release_manifest", ROOT / "scripts/release_manifest.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def fixture():
    data = {
        "schema_version": 1, "release_tag": "2026-09-06-123-1",
        "created_at": "2026-09-06T10:00:00Z", "infra_sha": "a" * 40,
        "source_ref": "fix/release-readiness-20260906", "github_run_id": "123",
        "workflow_repository": release.REPOSITORY,
        "provenance": {"workflow_path": release.WORKFLOW_PATH, "workflow_sha": "b" * 40,
                       "event_name": "workflow_dispatch"}, "services": {},
    }
    for service in release.SERVICES:
        repo = "admin" if service == "admin-web" else service
        data["services"][service] = {
            "source_repo": f"we-meet-trip/map-service-{repo}", "source_sha": "c" * 40,
            "image": f"{release.REGISTRY}/map-service-{service}", "digest": "sha256:" + "d" * 64,
        }
    return data


def dispatch_fixture():
    data = fixture()
    data["source_ref"] = "develop"
    data["provenance"].update(event_name="repository_dispatch", dispatch={
        "repository": "we-meet-trip/map-service-user", "sha": "c" * 40, "run_id": "987"})
    return data


def source_ci(payload):
    return {"id": int(payload["run_id"]), "path": release.SOURCE_CI_PATH,
            "event": "push", "head_branch": "develop", "head_sha": payload["sha"],
            "head_repository": {"full_name": payload["repository"]},
            "repository": {"full_name": payload["repository"]},
            "status": "completed", "conclusion": "success"}


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.payload = dispatch_fixture()["provenance"]["dispatch"]
        self.branch = {"ref": "refs/heads/develop", "object": {"sha": self.payload["sha"]}}

    def test_four_allowed_repositories_require_successful_exact_ci(self):
        for repository in release.DISPATCH_REPOSITORIES:
            payload = {**self.payload, "repository": repository}
            with self.subTest(repository=repository), patch.object(release, "public_github_json",
                    side_effect=[source_ci(payload), self.branch]) as api:
                self.assertEqual(release.verify_dispatch(payload), payload)
                self.assertEqual(api.call_args_list[0].args[0], f"/repos/{repository}/actions/runs/987")

    def test_unsafe_payload_is_rejected_before_any_api_call(self):
        changes = ({"repository": "attacker/fork"}, {"repository": "we-meet-trip/map-service-admin"},
                   {"repository": []}, {"sha": "develop"}, {"sha": "c" * 40 + "\n"},
                   {"run_id": "1/../../x"}, {"run_id": 987}, {"ref": "arbitrary"})
        with patch.object(release, "public_github_json") as api:
            for change in changes:
                with self.subTest(change=change), self.assertRaises(ValueError):
                    release.verify_dispatch({**self.payload, **change})
            api.assert_not_called()

    def test_pending_sender_can_finish_within_120_seconds(self):
        run = source_ci(self.payload)
        pending = {**run, "status": "in_progress", "conclusion": None}
        clock = [0.0]
        with patch.object(release.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(release.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)) as sleep, \
             patch.object(release, "public_github_json", side_effect=[pending, pending, run, self.branch]):
            release.verify_dispatch(self.payload, wait_seconds=120)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(clock[0], 20)

    def test_pending_sender_has_finite_deadline_and_never_builds_unfinished_ci(self):
        pending = {**source_ci(self.payload), "status": "in_progress", "conclusion": None}
        clock = [0.0]
        with patch.object(release.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(release.time, "sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)), \
             patch.object(release, "public_github_json", return_value=pending) as api:
            with self.assertRaisesRegex(ValueError, "deadline"):
                release.verify_dispatch(self.payload, wait_seconds=120)
        self.assertEqual(clock[0], 120)
        self.assertEqual(api.call_count, 12)
        with patch.object(release, "public_github_json", return_value=pending), patch.object(release.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "deadline"):
                release.verify_dispatch(self.payload)
            sleep.assert_not_called()

    def test_wrong_ci_identity_and_failure_are_rejected_without_polling(self):
        changes = ({"id": 999}, {"path": "other.yml"}, {"event": "pull_request"},
                   {"head_branch": "feature"}, {"head_sha": "f" * 40},
                   {"head_repository": {"full_name": "attacker/fork"}},
                   {"repository": {"full_name": "attacker/fork"}},
                   {"conclusion": "failure"}, {"conclusion": "cancelled"})
        for change in changes:
            with self.subTest(change=change), patch.object(release, "public_github_json",
                    return_value={**source_ci(self.payload), **change}), patch.object(release.time, "sleep") as sleep:
                with self.assertRaises(ValueError):
                    release.verify_dispatch(self.payload, wait_seconds=120)
                sleep.assert_not_called()

    def test_superseded_develop_commit_is_rejected(self):
        branch = {"ref": "refs/heads/develop", "object": {"sha": "f" * 40}}
        with patch.object(release, "public_github_json", side_effect=[source_ci(self.payload), branch]):
            with self.assertRaisesRegex(ValueError, "advanced"):
                release.verify_dispatch(self.payload)

    def test_dispatch_provenance_requires_matching_develop_image(self):
        release.validate(dispatch_fixture())
        for mutate in (lambda d: d.update(source_ref="feature"),
                       lambda d: d["provenance"].pop("dispatch"),
                       lambda d: d["services"]["user"].update(source_sha="f" * 40)):
            data = dispatch_fixture()
            mutate(data)
            with self.assertRaises(ValueError):
                release.validate(data)

    def test_event_validation_outputs_only_verified_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            event = {"action": "release-develop", "repository": {"full_name": release.REPOSITORY},
                     "client_payload": self.payload}
            (root / "event").write_text(json.dumps(event))
            args = SimpleNamespace(event_file=root / "event", output=root / "evidence", github_output=root / "outputs")
            with patch.object(release, "verify_dispatch", return_value=self.payload) as verify:
                release.verify_dispatch_event(args)
                verify.assert_called_once_with(self.payload, wait_seconds=120)
            self.assertEqual(json.loads(args.output.read_text()), self.payload)
            self.assertEqual(len(args.github_output.read_text().splitlines()), 3)
            event["action"] = "unrelated"
            args.event_file.write_text(json.dumps(event))
            with patch.object(release, "verify_dispatch") as verify:
                with self.assertRaises(ValueError):
                    release.verify_dispatch_event(args)
                verify.assert_not_called()


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        self.data = fixture()
        release.write_bundle(self.bundle, self.data)

    def rewrite_sums(self):
        lines = [f"{hashlib.sha256((self.bundle / name).read_bytes()).hexdigest()}  {name}\n"
                 for name in release.FILES]
        (self.bundle / "SHA256SUMS").write_text("".join(lines))

    def test_bundle_roundtrip_and_separate_compose_scopes(self):
        actual = release.verify_bundle(self.bundle, expected_run_id="123", expected_infra_sha="a" * 40,
                                       expected_workflow_sha="b" * 40)
        self.assertEqual(actual, self.data)
        app = (self.bundle / "compose.images.yml").read_text()
        admin = (self.bundle / "compose.admin-images.yml").read_text()
        self.assertNotIn("  admin:", app)
        self.assertNotIn("  user:", admin)
        self.assertEqual(app.count("@sha256:"), 4)
        self.assertEqual(admin.count("@sha256:"), 2)
        self.assertEqual(release.main(["verify", "--bundle", str(self.bundle)]), 0)

    def test_changed_file_is_rejected(self):
        (self.bundle / "compose.images.yml").write_text("services: {}\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            release.verify_bundle(self.bundle)

    def test_rehashed_injected_compose_is_rejected(self):
        path = self.bundle / "compose.images.yml"
        path.write_text(path.read_text() + "    privileged: true\n")
        self.rewrite_sums()
        with self.assertRaisesRegex(ValueError, "Compose mismatch"):
            release.verify_bundle(self.bundle)

    def test_duplicate_json_keys_are_rejected(self):
        path = self.bundle / "release.json"
        path.write_text(path.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'))
        self.rewrite_sums()
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            release.verify_bundle(self.bundle)

    def test_symlink_is_rejected(self):
        path = self.bundle / "release.json"
        path.rename(self.bundle / "original.json")
        path.symlink_to(self.bundle / "original.json")
        with self.assertRaisesRegex(ValueError, "bundle file"):
            release.verify_bundle(self.bundle)

    def test_receiver_provenance_expectations_are_enforced(self):
        for options in ({"expected_run_id": "999"}, {"expected_infra_sha": "f" * 40},
                        {"expected_workflow_sha": "f" * 40}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                release.verify_bundle(self.bundle, **options)

    def test_invalid_references_and_mutable_tags_are_rejected(self):
        for value in ("--upload-pack=bad", "develop\nnext", "x..y", "x//y", "x/.hidden", "x.lock", "x;command", "x/", "refs/heads/x@{1}"):
            with self.subTest(ref=value), self.assertRaises(ValueError):
                release.validate_ref(value)
        for value in ("latest", "develop", "master", "", "a" * 129, "a/b", "a\nx", ".bad"):
            with self.subTest(tag=value), self.assertRaises(ValueError):
                release.validate_tag(value)
        for value in ("develop", "fix/release-readiness-20260906", "refs/tags/v1.2.3", "a" * 40):
            release.validate_ref(value)

    def test_missing_or_mismatched_service_identity_is_rejected(self):
        mutations = (
            lambda d: d["services"].pop("yolo"),
            lambda d: d["services"]["hub"].update(image="attacker.invalid/hub"),
            lambda d: d["services"]["agent"].update(digest="latest"),
            lambda d: d["services"]["user"].update(source_sha="short"),
            lambda d: d["services"]["admin-web"].update(source_sha="f" * 40),
            lambda d: d.update(workflow_repository="someone/fork"),
            lambda d: d.update(schema_version=True),
            lambda d: d["provenance"].update(event_name="pull_request"),
            lambda d: d["provenance"].update(workflow_path="other.yml"),
            lambda d: d.update(created_at="2026-09-06T10:00:00"),
        )
        for mutate in mutations:
            data = copy.deepcopy(self.data)
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                release.validate(data)

    def test_automatic_release_requires_develop(self):
        self.data["provenance"]["event_name"] = "workflow_run"
        with self.assertRaisesRegex(ValueError, "develop"):
            release.validate(self.data)
        self.data["source_ref"] = "develop"
        release.validate(self.data)


class RegistryTests(unittest.TestCase):
    def config(self, entry, tag):
        return {"os": "linux", "architecture": "amd64", "config": {"Labels": {
            "org.opencontainers.image.revision": entry["source_sha"],
            "org.opencontainers.image.source": f"https://github.com/{entry['source_repo']}.git",
            "org.opencontainers.image.version": tag,
        }}}

    def test_verification_reads_only_digest_pinned_reference(self):
        data = fixture()
        entry = data["services"]["agent"]
        config = self.config(entry, data["release_tag"])
        for image in (config, {"linux/amd64": config}):
            with patch.object(release, "inspect", side_effect=[{"digest": entry["digest"]}, image]) as inspect:
                release.verify_registry_image(entry, data["release_tag"])
            self.assertTrue(all(call.args[0] == entry["image"] + "@" + entry["digest"]
                                for call in inspect.call_args_list))

    def test_wrong_platform_source_revision_version_and_digest_fail_closed(self):
        data = fixture()
        entry = data["services"]["agent"]
        changes = (
            lambda c: c.update(architecture="arm64"),
            lambda c: c["config"]["Labels"].update({"org.opencontainers.image.revision": "f" * 40}),
            lambda c: c["config"]["Labels"].update({"org.opencontainers.image.source": "https://attacker.invalid"}),
            lambda c: c["config"]["Labels"].update({"org.opencontainers.image.version": "old"}),
        )
        for change in changes:
            config = self.config(entry, data["release_tag"])
            change(config)
            with patch.object(release, "inspect", side_effect=[{"digest": entry["digest"]}, config]):
                with self.assertRaises(ValueError):
                    release.verify_registry_image(entry, data["release_tag"])
        with patch.object(release, "inspect", return_value={"digest": "sha256:" + "f" * 64}):
            with self.assertRaises(ValueError):
                release.verify_registry_image(entry, data["release_tag"])


if __name__ == "__main__":
    unittest.main()
