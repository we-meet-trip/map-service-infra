"""Small stdlib fixtures only: no Docker, network, user data or production graph."""
import copy
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ncp_artifacts", ROOT / "scripts/ncp-bootstrap-artifacts.py")
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


def learning_bundle(bundle, approved=True):
    bundle.mkdir()
    image = "registry.example/map-worker@sha256:" + "a" * 64
    images = {"dataset-worker": {"image": image, "source_commit": "b" * 40, "platform": "linux/amd64"}}
    worker = {"image": image, "entrypoint": ["python", "/opt/map/dataset-worker.py"],
              "command": ["--job", "/input/job.json", "--dataset-root", "/input", "--output-root", "/output", "--program", "/opt/map/segment_stats.py"],
              "security_opt": ["no-new-privileges:true"], "network_mode": "none", "read_only": True,
              "user": "10001:10001", "cap_drop": ["ALL"], "environment": {"MAP_LEARNING_HOLD": "true"},
              "volumes": [{"source": "synthetic" + str(i), "target": target, "read_only": target != "/output"}
                          for i, target in enumerate(("/input", "/output", "/opt/map/dataset-worker.py", "/opt/map/segment_stats.py"))],
              "mem_limit": 128 * 1024**2, "cpus": 0.5, "pids_limit": 64}
    (bundle / "role.yml").write_text("# Synthetic review copy; not executable by this installer.\n")
    write(bundle / "role.json", {"schema_version": 1, "role": "learning", "host_identity": "learning-fixture-1",
                                "deploy_account": "map-learning-deploy", "data_scope": "synthetic",
                                "compose_sha256": artifacts.sha256(bundle / "role.yml")})
    write(bundle / "rendered.json", {"services": {"dataset-worker": worker}})
    (bundle / "receiver.py").write_text("# Synthetic opaque receiver fixture. Never executed.\n")
    contract = {"schema_version": 1, "role": "learning", "data_scope": "synthetic", "images": images,
                "role_contract": {"manifest": "role.json", "compose": "role.yml", "rendered": "rendered.json", "scrape_config": None},
                "caddy": None, "map": None,
                "receiver": {"file": "receiver.py", "source_commit": "c" * 40, "owner": "session-c",
                             "sha256": artifacts.sha256(bundle / "receiver.py"), "capabilities": ["empty-host-ncp-v1"]},
                "artifact_sha256": {}}
    return seal(bundle, contract, approved)


def seal(bundle, contract, approved=True):
    contract["artifact_sha256"] = {path.name: artifacts.sha256(path) for path in bundle.iterdir()
                                   if path.name not in {"contract.json", "security-approval.json"}}
    write(bundle / "contract.json", contract)
    approval = {"schema_version": 1, "status": "approved" if approved else "candidate",
                "contract_sha256": artifacts.sha256(bundle / "contract.json"),
                "images_sha256": artifacts.canonical_sha(contract["images"]), "serving_approval": False,
                "reviewer": "synthetic-fixture" if approved else None,
                "reviewed_at": "2026-09-07T00:00:00Z" if approved else None,
                "evidence_sha256": "d" * 64 if approved else None}
    write(bundle / "security-approval.json", approval)
    return contract, artifacts.sha256(bundle / "security-approval.json")


def map_fixture(archive, extra=None):
    records = {}
    with tarfile.open(archive, "w:gz") as saved:
        for name in artifacts.osrm.RUNTIME_FILES:
            data = name.encode()
            records[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            saved.addfile(info, io.BytesIO(data))
        if extra is not None:
            saved.addfile(extra, io.BytesIO(b""))
    return {"schema": 1, "engine_image": artifacts.osrm.IMAGE, "engine_version": artifacts.osrm.VERSION,
            "algorithm": "mld", "runtime_files": records,
            "profiles": {name: {"sha256": sha} for name, sha in artifacts.osrm.PROFILE_HASHES.items()},
            "archive": {"bytes": archive.stat().st_size, "sha256": artifacts.sha256(archive)}}


class ArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ncp-artifacts-test-")
        self.root = Path(self.tmp.name).resolve()
        self.bundle = self.root / "bundle"
        self.destination = self.root / "staged"
        self.contract, self.pin = learning_bundle(self.bundle)

    def tearDown(self):
        self.tmp.cleanup()

    def reseal(self, approved=True):
        self.contract, self.pin = seal(self.bundle, self.contract, approved)

    def test_real_cli_stage_verify_and_idempotency(self):
        command = [sys.executable, str(ROOT / "scripts/ncp-bootstrap-artifacts.py")]
        common = ["--role", "learning", "--expected-approval-sha256", self.pin]
        for operation, expected in (("stage", "staged"), ("stage", "already_staged"), ("verify-installed", "installed_verified")):
            args = command + [operation, *common, "--destination", str(self.destination)]
            if operation == "stage":
                args.extend(("--bundle", str(self.bundle)))
            run = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads(run.stdout)
            self.assertEqual(report["status"], expected)
            self.assertFalse(report["activation_authorized"])
            self.assertEqual(report["docker_actions"], 0)
        for path in self.destination.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_candidate_is_verifiable_but_never_stageable(self):
        self.reseal(approved=False)
        self.assertEqual(artifacts.verify_bundle(self.bundle, "learning")["status"], "candidate_verified")
        with self.assertRaisesRegex(ValueError, "candidate cannot be staged"):
            artifacts.stage(self.bundle, self.destination, "learning", self.pin)
        self.assertFalse(self.destination.exists())

    def test_approval_must_be_externally_pinned(self):
        self.assertEqual(artifacts.verify_bundle(self.bundle, "learning")["status"], "untrusted_approval_verified")
        for pin in (None, "0" * 64):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                artifacts.stage(self.bundle, self.destination, "learning", pin)

    def test_mutable_images_and_source_platform_mismatches_rejected(self):
        original = copy.deepcopy(self.contract)
        for key, value in (("image", "registry.example/worker:latest"), ("source_commit", "develop"), ("platform", "linux/arm64")):
            self.contract = copy.deepcopy(original)
            self.contract["images"]["dataset-worker"][key] = value
            self.reseal()
            with self.subTest(key=key), self.assertRaises(ValueError):
                artifacts.verify_bundle(self.bundle, "learning", self.pin)

    def test_empty_images_and_cross_role_and_real_user_scope_rejected(self):
        for role in ("test", "admin", "prod"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                artifacts.verify_bundle(self.bundle, role, self.pin)
        self.contract["images"] = {}
        self.reseal()
        with self.assertRaises(ValueError):
            artifacts.verify_bundle(self.bundle, "learning", self.pin)

    def test_existing_role_verifier_enforces_hold_and_no_socket(self):
        rendered = artifacts.read_json(self.bundle / "rendered.json")
        rendered["services"]["dataset-worker"]["environment"]["MAP_LEARNING_HOLD"] = "false"
        write(self.bundle / "rendered.json", rendered)
        self.reseal()
        with self.assertRaisesRegex(ValueError, "HOLD"):
            artifacts.verify_bundle(self.bundle, "learning", self.pin)

    def test_rendered_service_digest_must_match_reviewed_contract(self):
        self.contract["images"]["dataset-worker"]["image"] = "registry.example/other@sha256:" + "e" * 64
        self.reseal()
        with self.assertRaisesRegex(ValueError, "rendered image identity"):
            artifacts.verify_bundle(self.bundle, "learning", self.pin)

    def test_artifact_bytes_are_bound_to_external_approval(self):
        (self.bundle / "role.yml").write_text("changed after review")
        manifest = artifacts.read_json(self.bundle / "role.json")
        manifest["compose_sha256"] = artifacts.sha256(self.bundle / "role.yml")
        write(self.bundle / "role.json", manifest)
        with self.assertRaisesRegex(ValueError, "artifact checksum"):
            artifacts.verify_bundle(self.bundle, "learning", self.pin)

    def test_receiver_is_opaque_and_requires_ncp_capability_and_owner(self):
        for key, value in (("capabilities", ["gcp-only"]), ("owner", "root")):
            original = copy.deepcopy(self.contract)
            self.contract["receiver"][key] = value
            self.reseal()
            with self.subTest(key=key), self.assertRaises(ValueError):
                artifacts.verify_bundle(self.bundle, "learning", self.pin)
            self.contract = original

    def test_json_duplicates_and_excess_size_rejected(self):
        for value in ('{"role":"learning","role":"prod"}', " " * (1024 * 1024 + 1)):
            (self.bundle / "contract.json").write_text(value)
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                artifacts.verify_bundle(self.bundle, "learning", self.pin)

    def test_symlink_source_and_destination_ancestor_rejected(self):
        (self.bundle / "receiver.py").unlink()
        (self.bundle / "receiver.py").symlink_to(self.bundle / "role.yml")
        with self.assertRaisesRegex(ValueError, "symlink"):
            artifacts.verify_bundle(self.bundle, "learning", self.pin)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            artifacts.stage(self.bundle, alias / "staged", "learning", self.pin)

    def test_existing_unrelated_destination_preserved(self):
        self.destination.mkdir()
        marker = self.destination / "existing-data"
        marker.write_text("preserve")
        with self.assertRaises(ValueError):
            artifacts.stage(self.bundle, self.destination, "learning", self.pin)
        self.assertEqual(marker.read_text(), "preserve")

    def test_insufficient_space_and_interrupted_copy_do_not_publish(self):
        disk = type("Disk", (), {"free": 0})()
        with patch.object(artifacts.shutil, "disk_usage", return_value=disk), self.assertRaisesRegex(ValueError, "disk headroom"):
            artifacts.stage(self.bundle, self.destination, "learning", self.pin)
        with patch.object(artifacts.shutil, "copyfileobj", side_effect=OSError("synthetic interruption")), self.assertRaises(OSError):
            artifacts.stage(self.bundle, self.destination, "learning", self.pin)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".ncp-artifacts-*")), [])

    def test_installed_corruption_is_rejected(self):
        artifacts.stage(self.bundle, self.destination, "learning", self.pin)
        (self.destination / "receiver.py").write_text("corrupt")
        with self.assertRaises(ValueError):
            artifacts.verify_installed(self.destination, "learning", self.pin)

    def test_concurrent_stage_lock_is_nonblocking_and_preserved(self):
        lock = self.root / ".ncp-stage-staged.lock"
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "another artifact installer"):
                artifacts.stage(self.bundle, self.destination, "learning", self.pin)
            self.assertFalse(self.destination.exists())
        finally:
            os.close(fd)
        self.assertTrue(lock.exists())

    def test_world_writable_parent_and_hardlinked_lock_rejected(self):
        parent = self.root / "unsafe"
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "caller-owned"):
            artifacts.stage(self.bundle, parent / "staged", "learning", self.pin)
        lock = self.root / ".ncp-stage-staged.lock"
        secret = self.root / "other-file"
        secret.write_text("preserve")
        secret.chmod(0o600)
        os.link(secret, lock)
        with self.assertRaisesRegex(ValueError, "unsafe staging lock"):
            artifacts.stage(self.bundle, self.destination, "learning", self.pin)
        self.assertEqual(secret.read_text(), "preserve")


class MapAndCaddyTest(unittest.TestCase):
    def test_40_runtime_files_verified_safely_extracted_and_corruption_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            archive = root / "map.tar.gz"
            manifest = map_fixture(archive)
            artifacts.osrm.verify_archive(archive, manifest)
            artifacts.extract_runtime(archive, root / "runtime", manifest)
            self.assertEqual(len(list((root / "runtime").rglob("*.osrm.*"))), 40)
            path = root / "runtime" / artifacts.osrm.RUNTIME_FILES[0]
            path.write_text("corrupt")
            with self.assertRaisesRegex(ValueError, "checksum/size"):
                artifacts.osrm.verify_tree(root / "runtime", manifest)

    def test_path_traversal_symlink_hardlink_duplicate_and_device_rejected(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE), ("link", tarfile.LNKTYPE),
                           (artifacts.osrm.RUNTIME_FILES[0], tarfile.REGTYPE), ("device", tarfile.CHRTYPE)):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                extra = tarfile.TarInfo(name)
                extra.type, extra.mode, extra.linkname = kind, 0o644, "/etc/passwd"
                archive = root / "map.tar.gz"
                manifest = map_fixture(archive, extra)
                with self.assertRaises(ValueError):
                    artifacts.osrm.verify_archive(archive, manifest)
                with self.assertRaises(ValueError):
                    artifacts.extract_runtime(archive, root / "runtime", manifest)
                self.assertFalse((root / "escape").exists())

    def test_production_caddy_anchor_is_exact_and_changed_archive_fails(self):
        self.assertEqual(artifacts.sha256(artifacts.caddy.MANIFEST), artifacts.CADDY_ANCHOR_SHA256)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            archive, report = root / "image.tar", root / "scan.json"
            archive.write_bytes(b"not reviewed archive")
            report.write_text("{}")
            with patch.object(artifacts.caddy.security, "run") as run, self.assertRaisesRegex(ValueError, "size mismatch"):
                artifacts.caddy.verify_files(archive, report)
            run.assert_not_called()

    def test_caddy_oci_chain_and_report_are_checked_without_runtime(self):
        documents = {}
        def blob(value):
            raw = json.dumps(value).encode()
            identity = "sha256:" + hashlib.sha256(raw).hexdigest()
            documents[identity] = raw
            return identity
        config = blob({"os": "linux", "architecture": "amd64"})
        platform = blob({"config": {"digest": config}})
        source = blob({"manifests": [{"digest": platform, "platform": {"architecture": "amd64", "os": "linux"}}]})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            archive, report, anchor_path = root / "image.tar", root / "scan.json", root / "anchor.json"
            with tarfile.open(archive, "w") as saved:
                for identity, data in documents.items():
                    info = tarfile.TarInfo("blobs/" + identity.replace(":", "/"))
                    info.size = len(data)
                    saved.addfile(info, io.BytesIO(data))
            scan = {"ArtifactType": "container_image", "Metadata": {"ImageID": source, "ImageConfig": {"architecture": "amd64", "os": "linux"}},
                    "Results": [{"Class": "os-pkgs"}, {"Target": "usr/bin/caddy", "Type": "gobinary"}]}
            write(report, scan)
            anchor = {"schema_version": 1, "platform": "linux/amd64", "archive_bytes": archive.stat().st_size,
                      "archive_sha256": artifacts.sha256(archive), "report_sha256": artifacts.sha256(report),
                      "source_image_id": source, "platform_image_id": platform, "config_image_id": config}
            write(anchor_path, anchor)
            with patch.object(artifacts.caddy, "MANIFEST", anchor_path), patch.object(artifacts.caddy.security, "run") as run:
                self.assertEqual(artifacts.caddy.verify_files(archive, report), anchor)
                scan["Results"][0]["Vulnerabilities"] = [{"Severity": "HIGH"}]
                write(report, scan)
                anchor["report_sha256"] = artifacts.sha256(report)
                write(anchor_path, anchor)
                with self.assertRaisesRegex(ValueError, "HIGH/CRITICAL"):
                    artifacts.caddy.verify_files(archive, report)
                run.assert_not_called()

    def test_combined_synthetic_prod_stage_with_local_caddy_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bundle = root / "bundle"
            bundle.mkdir()
            documents = {}
            def blob(value):
                raw = json.dumps(value).encode()
                identity = "sha256:" + hashlib.sha256(raw).hexdigest()
                documents[identity] = raw
                return identity
            config = blob({"os": "linux", "architecture": "amd64"})
            platform = blob({"config": {"digest": config}})
            source = blob({"manifests": [{"digest": platform, "platform": {"architecture": "amd64", "os": "linux"}}]})
            archive, report = bundle / "caddy.tar", bundle / "caddy-scan.json"
            with tarfile.open(archive, "w") as saved:
                for identity, data in documents.items():
                    info = tarfile.TarInfo("blobs/" + identity.replace(":", "/"))
                    info.size = len(data)
                    saved.addfile(info, io.BytesIO(data))
            write(report, {"ArtifactType": "container_image", "Metadata": {"ImageID": source, "ImageConfig": {"architecture": "amd64", "os": "linux"}},
                           "Results": [{"Class": "os-pkgs"}, {"Target": "usr/bin/caddy", "Type": "gobinary"}]})
            anchor = {"schema_version": 1, "platform": "linux/amd64", "archive_bytes": archive.stat().st_size,
                      "archive_sha256": artifacts.sha256(archive), "report_sha256": artifacts.sha256(report),
                      "source_image_id": source, "platform_image_id": platform, "config_image_id": config}
            anchor_path = root / "synthetic-anchor.json"
            write(anchor_path, anchor)
            map_archive = bundle / "map.tar.gz"
            write(bundle / "map.json", map_fixture(map_archive))
            (bundle / "receiver.py").write_text("# Synthetic NCP staging-only receiver, never executed.\n")
            images = {name: {"image": "registry.example/fixture@sha256:" + "a" * 64,
                             "source_commit": "b" * 40, "platform": "linux/amd64"} for name in artifacts.PROD_REQUIRED}
            images["edge"]["image"] = config
            for name in ("osrm-foot", "osrm-bicycle"):
                images[name]["image"] = artifacts.osrm.IMAGE
            contract = {"schema_version": 1, "role": "prod", "data_scope": "serving", "images": images,
                        "role_contract": None, "caddy": {"archive": archive.name, "report": report.name},
                        "map": {"archive": map_archive.name, "manifest": "map.json"},
                        "receiver": {"file": "receiver.py", "sha256": artifacts.sha256(bundle / "receiver.py"),
                                     "source_commit": "c" * 40, "owner": "root", "capabilities": ["empty-host-ncp-v1"]},
                        "artifact_sha256": {}}
            _, pin = seal(bundle, contract)
            with patch.object(artifacts.caddy, "MANIFEST", anchor_path), patch.object(artifacts, "CADDY_ANCHOR_SHA256", artifacts.sha256(anchor_path)):
                result = artifacts.stage(bundle, root / "staged", "prod", pin)
                self.assertEqual(result["map_files"], 40)
                self.assertEqual(artifacts.verify_installed(root / "staged", "prod", pin)["status"], "installed_verified")
                self.assertFalse(result["activation_authorized"])
                self.assertEqual(result["docker_actions"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
