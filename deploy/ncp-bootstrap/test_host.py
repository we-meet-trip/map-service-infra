#!/usr/bin/env python3
"""Real filesystem transactions, injected OS effects; never a real host install PASS."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("host", REPO / "scripts/ncp-bootstrap-host.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
KEY = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nsynthetic\n"


def manifest(role="prod"):
    inventory = {r: {"machine_id": str(i) * 32, "instance_id": "instance-" + r, "deploy_account": "map-deploy-" + r,
                     "data_volume_id": "volume-" + r, "secret_scope": "map-" + r, "provider": "gcp" if r == "test" else "ncp"}
                 for i, r in enumerate(("test", "prod", "admin", "learning"), 1)}
    return {"schema_version": 1, "role": role, "profile": {"prod": "prod-small", "admin": "admin-small", "learning": "learning-cpu"}[role],
            "machine_id": inventory[role]["machine_id"], "hostname": "map-" + role, "instance_id": inventory[role]["instance_id"],
            "data_uuid": "12345678-1234-1234-1234-123456789abc", "data_encryption": "luks2", "inventory": inventory,
            "docker_packages": {p: "1.2.3-1" for p in host.PACKAGES}, "docker_key_sha256": host.sha(KEY),
            "ssh_source_cidrs": ["192.0.2.12/32"], "network_review_sha256": "a" * 64, "account_quote_sha256": "b" * 64,
            "approval": "approved-empty-host-only", "learning_hold": True}


class FixtureOS:
    def __init__(self, m):
        self.m = m
        self.commands = []
        self.installed = False
        self.fail_packages_once = False
        self.has_workloads = False
        self.override = {}

    def facts(self, mount):
        p = host.validate(self.m)
        return {"os": "ubuntu", "version": "24.04", "arch": "x86_64", "systemd": True,
                "machine_id": self.m["machine_id"], "hostname": self.m["hostname"], "cpu": p["cpu"],
                "memory_bytes": p["ram_gb"] * 10**9, "root_bytes": p["root_gb"] * 10**9,
                "data_bytes": p["data_gb"] * 10**9, "data_free": p["data_gb"] * 10**9,
                "mount_uuid": self.m["data_uuid"], "mount_fstype": "ext4", "luks2": True, "mount_rw": True, **self.override}

    def run(self, args):
        self.commands.append(args)
        if args == ["containerd", "config", "default"]:
            return 'version = 3\nroot = "/var/lib/containerd"\nstate = "/run/containerd"\n'
        return ""

    def empty(self):
        if self.installed:
            raise ValueError("unknown docker")

    def packages(self, m, key):
        self.commands.append(["fixture-package-install"])
        if self.fail_packages_once:
            self.fail_packages_once = False
            raise ValueError("injected apt failure")
        self.installed = True

    def account(self, account, create=False):
        if create: self.commands.append(["fixture-account", account])

    def verify(self, m):
        assert self.installed

    def no_workloads(self):
        if self.has_workloads:
            raise ValueError("workloads exist")


class HostTransactions(unittest.TestCase):
    def test_uncreated_peer_reservation_does_not_require_learning_creation(self):
        m = manifest()
        for field, suffix in (("machine_id", "machine"), ("instance_id", "instance"), ("data_volume_id", "volume")):
            m["inventory"]["learning"][field] = "reserved-learning-" + suffix
        host.validate(m)
        m["inventory"]["learning"]["data_volume_id"] = "unobserved-real-volume"
        with self.assertRaises(ValueError): host.validate(m)

    def test_current_and_test_hosts_cannot_use_reserved_identity(self):
        for role in ("prod", "test"):
            m = manifest()
            for field, suffix in (("machine_id", "machine"), ("instance_id", "instance"), ("data_volume_id", "volume")):
                m["inventory"][role][field] = "reserved-" + role + "-" + suffix
            with self.assertRaises(ValueError): host.validate(m)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="map-bootstrap-filesystem-")
        self.root = Path(self.temp.name).resolve()
        self.m = manifest()
        (self.root / "srv/map-prod").mkdir(parents=True)
        (self.root / "usr/sbin").mkdir(parents=True)
        self.os = FixtureOS(self.m)

    def tearDown(self): self.temp.cleanup()

    def install(self):
        return host.install(self.m, KEY, host.sha(json.dumps(self.m, sort_keys=True, separators=(",", ":")).encode()), self.root, self.os)

    def test_install_twice_preserves_secret_and_no_repeat_commands(self):
        self.assertEqual(self.install()["status"], "host_prepared")
        host.inject_secret(self.m, "JWT_SECRET", b"synthetic-private-value", self.root)
        commands = copy.deepcopy(self.os.commands)
        self.assertEqual(self.install()["status"], "host_prepared")
        self.assertEqual(commands, self.os.commands)
        p = self.root / "srv/map-prod/secrets/JWT_SECRET"
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(p.read_bytes(), b"synthetic-private-value")

    def test_preflight_does_not_write(self):
        before = list(self.root.rglob("*"))
        host.preflight(self.m, self.root, self.os)
        self.assertEqual(before, list(self.root.rglob("*")))

    def test_bad_host_role_uuid_ram_and_encryption_fail_before_install(self):
        for field, value in (("machine_id", "0" * 32), ("mount_uuid", "wrong"), ("memory_bytes", 1), ("luks2", False), ("mount_rw", False)):
            with self.subTest(field=field):
                self.os.override = {field: value}
                with self.assertRaises(ValueError): self.install()
                self.assertFalse(self.os.commands)
        self.os.override = {}

    def test_existing_data_survives(self):
        p = self.root / "srv/map-prod/retain"
        p.write_text("existing data")
        with self.assertRaises(ValueError): self.install()
        self.assertEqual(p.read_text(), "existing data")

    def test_unmanaged_docker_directory_is_rejected(self):
        p = self.root / "var/lib/docker"
        p.mkdir(parents=True)
        (p / "existing").write_text("retain")
        with self.assertRaises(ValueError): self.install()

    def test_pending_approval_cannot_write(self):
        self.m["approval"] = "pending"
        with self.assertRaises(ValueError): self.install()
        self.assertFalse((self.root / "var/lib/map-bootstrap").exists())

    def test_approval_pin_and_key_mismatch(self):
        with self.assertRaises(ValueError): host.install(self.m, KEY, "0" * 64, self.root, self.os)
        with self.assertRaises(ValueError): host.install(self.m, KEY + b"bad", "0" * 64, self.root, self.os)
        self.assertFalse(self.os.commands)

    def test_cross_role_inventory_forbidden(self):
        for field in ("machine_id", "instance_id", "deploy_account", "data_volume_id", "secret_scope"):
            m = copy.deepcopy(self.m)
            m["inventory"]["admin"][field] = m["inventory"]["prod"][field]
            with self.assertRaises(ValueError): host.validate(m)

    def test_symlink_path_rejected(self):
        (self.root / "etc").symlink_to(self.root / "usr", target_is_directory=True)
        with self.assertRaises(ValueError): self.install()
        self.assertFalse(self.os.commands)

    def test_apt_interruption_resumes_and_does_not_unmask_early(self):
        self.os.fail_packages_once = True
        with self.assertRaisesRegex(ValueError, "apt failure"): self.install()
        self.assertFalse((self.root / "usr/sbin/policy-rc.d").exists())
        self.assertFalse(any("unmask" in c for c in self.os.commands))
        self.assertEqual(self.install()["status"], "host_prepared")

    def test_secrets_are_allowlisted_and_never_replaced(self):
        self.install()
        with self.assertRaises(ValueError): host.inject_secret(self.m, "ADMIN_CONTROL_DATABASE_PASSWORD", b"denied", self.root)
        host.inject_secret(self.m, "JWT_SECRET", b"original", self.root)
        host.inject_secret(self.m, "JWT_SECRET", b"original", self.root)
        with self.assertRaises(ValueError): host.inject_secret(self.m, "JWT_SECRET", b"replacement", self.root)

    def test_learning_cannot_receive_serving_secrets(self):
        self.m = manifest("learning"); self.os = FixtureOS(self.m)
        (self.root / "srv/map-learning").mkdir()
        self.install()
        for name in ("LOCATION_MASTER_KEY", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "PRODUCTION_SSH_KEY", "GEMINI_API_KEY"):
            with self.assertRaises(ValueError): host.inject_secret(self.m, name, b"denied", self.root)

    def test_rollback_preserves_data_secrets_packages_account(self):
        self.install()
        host.inject_secret(self.m, "JWT_SECRET", b"retain", self.root)
        p = self.root / "srv/map-prod/backups/closed-backup"
        p.write_text("retain data")
        result = host.rollback(self.m, self.root, self.os)
        self.assertEqual(result["data_deleted"], 0)
        self.assertTrue(p.exists())
        self.assertEqual((self.root / "srv/map-prod/secrets/JWT_SECRET").read_bytes(), b"retain")
        self.assertTrue((self.root / "etc/docker/daemon.json").exists())
        self.assertTrue(any("mask" in c for c in self.os.commands))
        self.assertTrue((self.root / "var/lib/map-bootstrap/rollback-config/etc/docker/daemon.json").exists())
        self.assertTrue(host.rollback(self.m, self.root, self.os)["idempotent"])

    def test_active_data_volumes_or_containers_block_rollback(self):
        self.install(); self.os.has_workloads = True
        before = copy.deepcopy(self.os.commands)
        with self.assertRaises(ValueError): host.rollback(self.m, self.root, self.os)
        self.assertEqual(before, self.os.commands)
        self.assertTrue((self.root / "etc/docker/daemon.json").exists())

    def test_modified_configuration_cannot_be_reinstalled_or_rolled_back(self):
        self.install()
        p = self.root / "etc/docker/daemon.json"
        p.write_text("administrator edit")
        with self.assertRaises(ValueError): self.install()
        with self.assertRaises(ValueError): host.rollback(self.m, self.root, self.os)
        self.assertEqual(p.read_text(), "administrator edit")

    def test_interrupted_atomic_file_write_leaves_no_partial_final(self):
        destination = self.root / "one-file"
        with patch.object(host.os, "fsync", side_effect=OSError("synthetic ENOSPC")):
            with self.assertRaises(OSError): host.write_once(destination, b"complete bytes")
        self.assertFalse(destination.exists())
        host.write_once(destination, b"complete bytes")
        self.assertEqual(destination.read_bytes(), b"complete bytes")

    def test_other_systemd_dropin_blocks_unmanaged_host(self):
        p = self.root / "etc/systemd/system/docker.service.d/99-other.conf"
        p.parent.mkdir(parents=True); p.write_text("retain")
        with self.assertRaises(ValueError): self.install()
        self.assertEqual(p.read_text(), "retain")

    def test_rollback_interruption_resumes_with_guards_preserved(self):
        self.install()
        old = self.os.run
        def interrupted(args):
            if "mask" in args: raise ValueError("injected rollback interruption")
            return old(args)
        self.os.run = interrupted
        with self.assertRaises(ValueError): host.rollback(self.m, self.root, self.os)
        state = json.loads((self.root / "var/lib/map-bootstrap/transaction.json").read_text())
        self.assertEqual(state["status"], "rolling_back")
        self.assertTrue((self.root / "etc/docker/daemon.json").exists())
        self.os.run = old
        self.assertEqual(host.rollback(self.m, self.root, self.os)["status"], "rolled_back")

    def test_real_stage_bound_to_host_then_image_cache_adapter(self):
        import test_artifacts as fixture
        self.m = manifest("learning"); self.os = FixtureOS(self.m)
        (self.root / "srv/map-learning").mkdir()
        self.install()
        bundle = self.root / "bundle"
        contract, pin = fixture.learning_bundle(bundle)
        before = copy.deepcopy(self.os.commands)
        with self.assertRaisesRegex(ValueError, "target host/account"):
            host.artifact_cache(self.m, bundle, pin, "fixture-release", self.root, self.os)
        self.assertEqual(before, self.os.commands)
        role = fixture.artifacts.read_json(bundle / "role.json")
        role.update(host_identity=self.m["hostname"], deploy_account="map-deploy-learning")
        fixture.write(bundle / "role.json", role)
        contract, pin = fixture.seal(bundle, contract)
        old = self.os.run
        def image_adapter(argv):
            if "inspect" in argv:
                return json.dumps([{"Os":"linux", "Architecture":"amd64", "RepoDigests":[contract["images"]["dataset-worker"]["image"]]}])
            return old(argv)
        self.os.run = image_adapter
        result = host.artifact_cache(self.m, bundle, pin, "fixture-correct-host", self.root, self.os)
        self.assertEqual(result["status"], "images_cached")
        self.assertFalse(result["receiver_executed"])
        self.assertFalse(any("run" in c for c in self.os.commands))


if __name__ == "__main__": unittest.main(verbosity=2)
