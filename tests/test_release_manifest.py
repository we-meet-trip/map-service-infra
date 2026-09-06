import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
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
