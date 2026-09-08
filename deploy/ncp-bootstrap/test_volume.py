#!/usr/bin/env python3
"""Synthetic filesystem journal + injected OS effects; no real block operations."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("volume", REPO / "scripts/ncp-bootstrap-volume.py")
volume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(volume)
helper_spec = importlib.util.spec_from_file_location("host_volume_test_input", Path(__file__).with_name("test_host.py"))
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)
KEY = b"synthetic-volume-key-never-a-real-key" * 2


def contract(enrollment):
    profile = volume.host.validate(enrollment)
    return {"schema_version": 1, "role": enrollment["role"], "root_enrollment_sha256": volume.digest(enrollment),
            "device": "/dev/vdb", "serial": "synthetic-empty-disk", "size_bytes": profile["data_gb"] * 10**9,
            "major_minor": "252:16", "luks_uuid": "87654321-4321-4321-4321-cba987654321", "approval": "approved-empty-volume-only"}


class FixtureOS:
    def __init__(self, enrollment, c):
        self.enrollment = enrollment
        self.contract = c
        self.effects = []
        self.reads = []
        self.override = {}
        self.header = None
        self.filesystem_uuid = None
        self.mapping = False
        self.mounted = False
        self.extra_mounts = []
        self.key = KEY
        self.has_signature = False
        self.all_zero = True
        self.runtime_active = False
        self.fail = None
        self.scan_count = 0

    def effect(self, action):
        self.effects.append(action)
        if self.fail == action:
            raise ValueError("injected " + action + " interruption")

    def identity(self, enrollment):
        self.reads.append("host-identity")
        if enrollment["machine_id"] != self.enrollment["machine_id"]:
            raise ValueError("host enrollment identity mismatch")

    def probe(self, c):
        self.reads.append("disk-probe")
        rows = list(self.extra_mounts)
        if self.mounted:
            rows.append({"target": volume.names(self.enrollment)[1], "maj:min": "253:0", "fstype": "ext4", "uuid": self.filesystem_uuid, "options": "rw,nodev,nosuid"})
        return {"serial": c["serial"], "size_bytes": c["size_bytes"], "major_minor": c["major_minor"],
                "reported_major_minor": c["major_minor"], "type": "disk", "readonly": False, "parent": None,
                "children": [{"name": "own-mapper", "type": "crypt", "maj:min": "253:0"}] if self.mapping else [], "holders": ["dm-0"] if self.mapping else [],
                "root_numbers": {"252:0", "252:1"}, "mounts": rows, **self.override}

    def mapper(self, enrollment, c):
        self.reads.append("mapper-identity")
        return "253:0" if self.mapping else None

    def signatures(self, device):
        self.reads.append("signatures")
        if self.has_signature:
            raise ValueError("disk signature exists; never format")

    def scan_all(self, c):
        self.reads.append("full-scan")
        self.scan_count += 1
        if not self.all_zero:
            raise ValueError("disk contains data; never format")
        return c["size_bytes"]

    def luks(self, c, key):
        self.reads.append("luks-uuid-and-key")
        if self.header != c["luks_uuid"]:
            raise ValueError("LUKS UUID mismatch")
        if key != self.key:
            raise ValueError("key rejected")

    def filesystem(self, enrollment):
        self.reads.append("filesystem-uuid")
        if not self.mapping or self.filesystem_uuid != enrollment["data_uuid"]:
            raise ValueError("owned ext4 UUID mismatch")

    def idle(self):
        self.reads.append("runtime-idle")
        if self.runtime_active:
            raise ValueError("stop host runtime before volume close")

    def format(self, c, key):
        self.header = c["luks_uuid"]
        self.has_signature = True
        self.all_zero = False
        self.effect("luksFormat")

    def open(self, enrollment, c, key):
        self.mapping = True
        self.effect("open")

    def mkfs(self, enrollment):
        self.filesystem_uuid = enrollment["data_uuid"]
        self.effect("mkfs.ext4")

    def mount(self, enrollment):
        self.effect("mount")
        self.mounted = True

    def unmount(self, enrollment):
        self.mounted = False
        self.effect("umount")

    def close(self, enrollment):
        self.mapping = False
        self.effect("close")


class VolumeTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="map-volume-filesystem-")
        self.root = Path(self.temp.name).resolve()
        self.enrollment = helper.manifest()
        self.c = contract(self.enrollment)
        self.os = FixtureOS(self.enrollment, self.c)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **kwargs):
        return volume.prepare(self.enrollment, self.c, KEY, volume.digest(self.c), root=self.root, backend=self.os, **{"format_empty": True, **kwargs})

    def restore(self, key=KEY):
        return volume.restore_mount(self.enrollment, self.c, key, self.root, self.os)

    def rollback(self):
        return volume.rollback(self.enrollment, self.c, KEY, self.root, self.os)

    def state(self):
        return json.loads(volume.paths(self.root, self.enrollment)[0].read_text())

    def test_inspect_is_readonly_and_full_scan(self):
        result = volume.inspect(self.enrollment, self.c, self.root, self.os)
        self.assertEqual(result["bytes_read"], self.c["size_bytes"])
        self.assertEqual(result["formats_executed"], 0)
        self.assertEqual(self.os.effects, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_prepare_then_repeat_verifies_without_format_or_mount(self):
        self.assertEqual(self.prepare()["status"], "volume_ready")
        before = list(self.os.effects)
        self.assertEqual(before, ["luksFormat", "open", "mkfs.ext4", "mount"])
        result = self.prepare()
        self.assertEqual(result["formats_executed"], 0)
        self.assertEqual(self.os.effects, before)
        self.assertEqual(self.os.scan_count, 1)
        self.assertFalse(result["reboot_auto_unlock"])

    def test_approval_flag_and_pin_are_required_before_writes(self):
        with self.assertRaises(ValueError): self.prepare(format_empty=False)
        with self.assertRaises(ValueError): volume.prepare(self.enrollment, self.c, KEY, "0" * 64, True, self.root, self.os)
        self.c["approval"] = "pending"
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.os.effects, [])

    def test_pending_host_and_wrong_enrollment_pin_are_rejected(self):
        self.enrollment["approval"] = "pending"
        self.c["root_enrollment_sha256"] = volume.digest(self.enrollment)
        with self.assertRaises(ValueError): self.prepare()
        self.c["root_enrollment_sha256"] = "0" * 64
        with self.assertRaises(ValueError): volume.validate(self.enrollment, self.c)

    def test_root_child_mounted_holder_readonly_and_identity_rejected(self):
        cases = [{"root_numbers": {self.c["major_minor"]}}, {"children": [{"name": "partition"}]}, {"holders": ["dm-7"]},
                 {"readonly": True}, {"parent": "vda"}, {"type": "part"}, {"serial": "another"}, {"size_bytes": 1},
                 {"major_minor": "252:32"}, {"reported_major_minor": "252:32"},
                 {"mounts": [{"target": "/foreign", "maj:min": self.c["major_minor"]}]}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.os.override = changes
                with self.assertRaises(ValueError): self.prepare()
                self.assertEqual(self.os.effects, [])
                self.assertFalse(volume.paths(self.root, self.enrollment)[0].exists())

    def test_disk_data_without_signature_is_not_empty(self):
        self.os.all_zero = False
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.os.effects, [])

    def test_signature_and_foreign_mapper_are_rejected(self):
        self.os.has_signature = True
        with self.assertRaises(ValueError): self.prepare()
        self.os.has_signature = False
        self.os.mapping = True
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.os.effects, [])

    def test_foreign_empty_mountpoint_is_rejected_before_format(self):
        self.os.extra_mounts = [{"target": "/srv/map-prod", "maj:min": "8:16", "fstype": "ext4", "uuid": "foreign", "options": "rw"}]
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.os.effects, [])

    def test_partial_scan_cannot_authorize_format(self):
        with patch.object(self.os, "scan_all", return_value=self.c["size_bytes"] - 1):
            with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.os.effects, [])

    def test_preserve_existing_mountpoint_file(self):
        target = volume.paths(self.root, self.enrollment)[1]
        target.mkdir(parents=True)
        old = target / "retain.txt"
        old.write_text("untouched user data")
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(old.read_text(), "untouched user data")
        self.assertEqual(self.os.effects, [])

    def test_symlink_mountpoint_and_journal_rejected(self):
        (self.root / "srv").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.os.effects, [])

    def test_wrong_key_header_or_fs_uuid_never_reformats(self):
        self.prepare()
        before = list(self.os.effects)
        with self.assertRaises(ValueError): volume.verify(self.enrollment, self.c, b"bad", self.root, self.os)
        self.os.header = "wrong"
        with self.assertRaises(ValueError): self.prepare()
        self.os.header = self.c["luks_uuid"]
        self.os.filesystem_uuid = "wrong"
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.os.effects, before)

    def test_format_crash_journal_blocks_every_reformat(self):
        for step in ("luksFormat", "open", "mkfs.ext4"):
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory(prefix="map-volume-crash-") as d:
                    self.root = Path(d).resolve()
                    self.os = FixtureOS(self.enrollment, self.c)
                    self.os.fail = step
                    with self.assertRaises(ValueError): self.prepare()
                    effects = list(self.os.effects)
                    self.os.fail = None
                    with self.assertRaises(ValueError): self.prepare()
                    with self.assertRaises(ValueError): self.restore()
                    self.assertEqual(self.os.effects, effects)
                    result = self.rollback()
                    self.assertEqual(result["status"], "quarantined_closed")
                    self.assertEqual(result["data_deleted"], 0)
                    with self.assertRaises(ValueError): self.restore()

    def test_mount_failure_can_recover_without_any_reformat(self):
        self.os.fail = "mount"
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.state()["status"], "filesystem_ready")
        self.os.fail = None
        self.assertEqual(self.restore()["status"], "volume_ready")
        self.assertEqual(self.os.effects.count("luksFormat"), 1)
        self.assertEqual(self.os.effects.count("mkfs.ext4"), 1)

    def test_reboot_requires_key_uuid_and_journal_then_mount_only(self):
        self.prepare()
        self.os.mapping = self.os.mounted = False
        with self.assertRaises(ValueError): self.prepare()
        with self.assertRaises(ValueError): self.restore(b"wrong")
        before = list(self.os.effects)
        self.assertEqual(self.restore()["status"], "volume_ready")
        self.assertEqual(self.os.effects[len(before):], ["open", "mount"])

    def test_rollback_preserves_data_and_repeat_is_idempotent(self):
        self.prepare()
        target = volume.paths(self.root, self.enrollment)[1]
        (target / "synthetic-retained-data").write_bytes(b"retained")
        result = self.rollback()
        self.assertEqual(result["status"], "closed")
        self.assertEqual((target / "synthetic-retained-data").read_bytes(), b"retained")
        before = list(self.os.effects)
        self.assertEqual(self.rollback()["data_deleted"], 0)
        self.assertEqual(self.os.effects, before)
        self.assertEqual(self.os.filesystem_uuid, self.enrollment["data_uuid"])

    def test_rollback_crash_resumes_and_does_not_reformat(self):
        self.prepare()
        self.os.fail = "umount"
        with self.assertRaises(ValueError): self.rollback()
        self.assertEqual(self.state()["status"], "closing")
        self.os.fail = None
        self.assertEqual(self.rollback()["status"], "closed")
        self.assertEqual(self.os.effects.count("luksFormat"), 1)
        self.assertEqual(self.os.effects.count("mkfs.ext4"), 1)

    def test_running_runtime_submount_and_foreign_mount_block_close(self):
        self.prepare()
        before = list(self.os.effects)
        self.os.runtime_active = True
        with self.assertRaises(ValueError): self.rollback()
        self.os.runtime_active = False
        self.os.extra_mounts = [{"target": "/srv/map-prod/foreign", "maj:min": "8:16", "fstype": "ext4", "uuid": "foreign", "options": "rw"}]
        with self.assertRaises(ValueError): self.rollback()
        self.assertEqual(self.os.effects, before)

    def test_additional_unmounted_mapping_blocks_verified_operations(self):
        self.prepare()
        before = list(self.os.effects)
        self.os.override = {"holders": ["dm-0", "dm-1"]}
        with self.assertRaises(ValueError): self.prepare()
        with self.assertRaises(ValueError): self.rollback()
        self.assertEqual(self.os.effects, before)

    def test_journal_contract_pin_prevents_another_role_or_disk(self):
        self.prepare()
        c = copy.deepcopy(self.c)
        c["serial"] = "another-disk"
        with self.assertRaises(ValueError): volume.verify(self.enrollment, c, KEY, self.root, self.os)
        self.assertEqual(self.os.effects.count("luksFormat"), 1)


class ReadAndCommandBoundaries(unittest.TestCase):
    def test_full_scan_reads_final_byte_and_handles_short_stream(self):
        size = 1024 * 1024 + 7
        self.assertEqual(volume.scan_stream(io.BytesIO(b"\0" * size), size, 4096), size)
        for data in (b"\0" * (size - 1) + b"x", b"\0" * (size - 1), b"\0" * (size + 1)):
            with self.assertRaises(ValueError): volume.scan_stream(io.BytesIO(data), size, 4096)

    def test_private_key_bytes_not_copied_or_returned_by_tool(self):
        with tempfile.TemporaryDirectory(prefix="map-volume-key-") as d:
            key = Path(d).resolve() / "private-key"
            key.write_bytes(KEY)
            key.chmod(0o600)
            self.assertEqual(volume.key_bytes(key), KEY)
            key.chmod(0o644)
            with self.assertRaises(ValueError): volume.key_bytes(key)
            key.chmod(0o600)
            linked = key.with_name("linked")
            os.link(key, linked)
            with self.assertRaises(ValueError): volume.key_bytes(key)

    def test_linux_commands_use_stdin_keys_and_never_force_fs_or_close(self):
        enrollment = helper.manifest()
        c = contract(enrollment)
        calls = []
        backend = volume.Linux()
        def fake(argv, **kwargs):
            calls.append((argv, kwargs))
            return 0, ""
        backend.run = fake
        backend.format(c, KEY); backend.open(enrollment, c, KEY); backend.mkfs(enrollment)
        backend.mount(enrollment); backend.unmount(enrollment); backend.close(enrollment)
        self.assertEqual([v[0][0] for v in calls], ["cryptsetup", "cryptsetup", "mkfs.ext4", "mount", "umount", "cryptsetup"])
        self.assertEqual(calls[0][1]["input"], KEY)
        self.assertEqual(calls[1][1]["input"], KEY)
        argv_text = repr([v[0] for v in calls])
        self.assertNotIn(KEY.decode(), argv_text)
        self.assertNotIn("-F", calls[2][0])
        self.assertNotIn("--force", argv_text)
        self.assertNotIn("--lazy", argv_text)

    def test_activating_or_deactivating_runtime_cannot_be_closed(self):
        backend = volume.Linux()
        for status in ("active", "activating", "deactivating", "reloading", "unexpected"):
            with self.subTest(status=status):
                backend.run = lambda *a, **kw: (3, status)
                with self.assertRaises(ValueError): backend.idle()
        backend.run = lambda *a, **kw: (3, "inactive\n")
        backend.idle()


if __name__ == "__main__":
    unittest.main()
