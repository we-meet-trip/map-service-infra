import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("watchdog", ROOT / "scripts/cutover_watchdog.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.d = guard.receiver()
        self.d.STATE = Path(self.temp.name)
        self.calls = []
        self.scope = "inactive"
        self.items = {s: {"id": hashlib.sha256((s + "container").encode()).hexdigest(),
                         "image": "sha256:" + hashlib.sha256(s.encode()).hexdigest(),
                         "restart": "no", "running": True}
                      for s in set(guard.PUBLIC) | set(self.d.release.SERVICES)}
        self.approved = {s: f"{self.d.release.REGISTRY}/map-service-{s}@{self.items[s]['image']}"
                         for s in self.d.release.SERVICES}
        self.policy = {"schema_version": 1, "instance_id": self.d.INSTANCE_ID,
                       "candidate_allowed": [self.approved], "rollback_verified": []}
        self.latch = {"schema_version": 1, "instance_id": self.d.INSTANCE_ID, "run_id": "123",
                      "infra_sha": "a" * 40, "phase": "complete", "bundle": "/private/bundle",
                      "candidate": self.approved, "prior_rollback_compatible": False}
        self.write("rollback-policy.json", self.policy)
        self.write("security-cutover.json", self.latch)
        (self.d.STATE / "public-restart.yml").write_text(guard.OVERRIDE)
        for name in ("validate_host_metadata", "validate_state_directory", "verify_instance", "smoke"):
            mocked = patch.object(self.d, name)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.object(self.d, "command", side_effect=self.command)
        mocked.start()
        self.addCleanup(mocked.stop)
        guard.write_ready_receipt(self.d, self.latch, "complete")
        self.calls.clear()

    def write(self, name, data):
        (self.d.STATE / name).write_text(json.dumps(data))

    def command(self, args, **kwargs):
        self.calls.append(tuple(args))
        self.assertEqual(kwargs.get("env"), guard.ENV)
        if args[:3] == ["systemctl", "show", guard.RECEIVER_UNIT]:
            return self.scope
        if args[:2] == ["docker", "ps"]:
            service = next(x.split("=")[-1] for x in args if x.startswith("label=com.docker.compose.service="))
            item = self.items[service]
            return item["id"] if "-aq" in args or item["running"] else ""
        if args[:2] == ["docker", "inspect"]:
            return json.dumps(next(x for x in self.items.values() if x["id"] == args[-1]))
        if args[:3] == ["docker", "image", "inspect"]:
            return args[-1].split("@")[-1]
        if args[:2] == ["docker", "exec"]:
            self.assertIn(args[3], ("nginx",))
            self.assertIn(args[4], ("-t", "-s"))
            return ""
        if args[:2] in (["docker", "start"], ["docker", "stop"], ["docker", "update"]):
            item = next(x for x in self.items.values() if x["id"] == args[-1])
            if args[1] == "update":
                item["restart"] = "no"
            else:
                item["running"] = args[1] == "start"
            return ""
        raise AssertionError("unexpected command")

    def mutations(self):
        return [x for x in self.calls if len(x) > 1 and x[1] in ("start", "stop", "update")]

    def test_healthy_complete_neither_stops_nor_restarts(self):
        self.assertEqual(guard.recover_once(self.d), "completed_public_supervised")
        self.assertEqual(self.mutations(), [])
        self.d.smoke.assert_not_called()

    def test_boot_starts_only_exact_receipted_public_services_edge_last(self):
        for s in guard.PUBLIC:
            self.items[s]["running"] = False
        guard.recover_once(self.d)
        self.assertEqual([x[-1] for x in self.mutations()], [self.items[s]["id"] for s in ("yolo", "user", "proxy", "edge")])
        self.d.smoke.assert_called_once_with(include_public=False)

    def test_failed_boot_private_readiness_does_not_open_edge_or_stop_services(self):
        self.items["edge"]["running"] = False
        self.d.smoke.side_effect = RuntimeError("private data must not be logged")
        with self.assertRaises(RuntimeError):
            guard.recover_once(self.d)
        self.assertEqual(self.mutations(), [])

    def test_all_nonterminal_phases_stop_four_and_preserve_other_services(self):
        for phase in self.d.CUTOVER_PHASES - set(guard.TOLERATED):
            with self.subTest(phase=phase):
                self.write("security-cutover.json", {**self.latch, "phase": phase})
                for item in self.items.values():
                    item["running"] = True
                self.calls.clear()
                self.assertEqual(guard.recover_once(self.d), "public_quarantined")
                self.assertEqual([x[-1] for x in self.mutations()], [self.items[s]["id"] for s in guard.PUBLIC])
                self.assertTrue(all(self.items[s]["running"] for s in set(self.items) - set(guard.PUBLIC)))

    def test_a_replacement_in_progress_returns_traffic_instead_of_closing_anything(self):
        upstreams = Path(self.temp.name) / "upstreams"
        upstreams.mkdir()
        (upstreams / "hub.conf").write_text("set $hub_upstream http://map-test-hub-rollover:8000;\n")
        self.write("security-cutover.json", {**self.latch, "phase": "rollover"})
        self.calls.clear()
        with patch.object(guard, "UPSTREAMS", upstreams):
            self.assertEqual(guard.recover_once(self.d), "rollover_returned_to_canonical")
        self.assertEqual(list(upstreams.iterdir()), [])
        self.assertEqual(self.mutations(), [])
        self.assertTrue(all(item["running"] for item in self.items.values()))
        self.assertTrue(any(c[:2] == ("docker", "exec") and c[-1] == "reload" for c in self.calls))

    def test_a_failed_replacement_leaves_the_serving_containers_alone(self):
        upstreams = Path(self.temp.name) / "upstreams"
        upstreams.mkdir()
        (upstreams / "user.conf").write_text("set $bff_upstream http://map-test-user-rollover:8080;\n")
        self.write("security-cutover.json", {**self.latch, "phase": "rollover_failed_serving"})
        self.calls.clear()
        with patch.object(guard, "UPSTREAMS", upstreams):
            self.assertEqual(guard.recover_once(self.d), "rollover_returned_to_canonical")
        self.assertEqual(list(upstreams.iterdir()), [])
        self.assertEqual(self.mutations(), [])
        self.assertTrue(all(item["running"] for item in self.items.values()))

    def test_a_failed_replacement_with_a_missing_container_still_closes_the_entry_points(self):
        upstreams = Path(self.temp.name) / "upstreams"
        upstreams.mkdir()
        self.write("security-cutover.json", {**self.latch, "phase": "rollover_failed_serving"})
        self.items["proxy"]["running"] = False
        self.calls.clear()
        with patch.object(guard, "UPSTREAMS", upstreams):
            self.assertEqual(guard.recover_once(self.d), "public_quarantined")

    def test_a_replacement_with_a_missing_container_still_closes_the_entry_points(self):
        upstreams = Path(self.temp.name) / "upstreams"
        upstreams.mkdir()
        self.write("security-cutover.json", {**self.latch, "phase": "rollover"})
        self.items["user"]["running"] = False
        self.calls.clear()
        with patch.object(guard, "UPSTREAMS", upstreams):
            self.assertEqual(guard.recover_once(self.d), "public_quarantined")

    def test_stopped_pending_never_restarts_on_boot(self):
        self.write("security-cutover.json", {**self.latch, "phase": "opening_ingress"})
        for s in guard.PUBLIC:
            self.items[s]["running"] = False
        guard.recover_once(self.d)
        self.assertEqual(self.mutations(), [])

    def test_lock_busy_does_not_consume_stale_latch(self):
        self.write("security-cutover.json", {**self.latch, "phase": "opening_ingress"})
        with self.d.deployment_lock():
            with self.assertRaises(self.d.DeployError):
                guard.recover_once(self.d)
        self.assertEqual(self.calls, [])

    def test_latch_is_read_only_after_lock_acquisition(self):
        original = self.d.deployment_lock
        self.write("security-cutover.json", {**self.latch, "phase": "opening_ingress"})
        @contextlib.contextmanager
        def finish_before_lock():
            with original():
                self.write("security-cutover.json", self.latch)
                yield
        with patch.object(self.d, "deployment_lock", finish_before_lock):
            guard.recover_once(self.d)
        self.assertEqual(self.mutations(), [])

    def test_active_and_reaping_receiver_never_races_quarantine(self):
        self.write("security-cutover.json", {**self.latch, "phase": "opening_ingress"})
        for state in ("active", "activating", "deactivating"):
            self.scope = state
            self.assertEqual(guard.recover_once(self.d), "receiver_active")
        self.assertEqual(self.mutations(), [])

    def test_wrong_image_or_container_identity_cannot_be_started(self):
        for field in ("id", "image"):
            before = copy.deepcopy(self.items)
            self.items["user"][field] = ("sha256:" if field == "image" else "") + "0" * 64
            self.items["user"]["running"] = False
            with self.assertRaises(self.d.DeployError):
                guard.recover_once(self.d)
            self.assertEqual(self.mutations(), [])
            self.items = before

    def test_missing_corrupt_or_stale_receipt_does_not_stop_good_serving(self):
        path = self.d.STATE / "security-public-ready.json"
        good = path.read_bytes()
        for value in (None, b"bad", good.replace(b'"123"', b'"124"')):
            if value is None:
                path.unlink()
            else:
                path.write_bytes(value)
            with self.assertRaises((self.d.DeployError, ValueError)):
                guard.recover_once(self.d)
            self.assertEqual(self.mutations(), [])
            path.write_bytes(good)

    def test_missing_policy_blocks_restart_without_stopping_completed_release(self):
        (self.d.STATE / "rollback-policy.json").unlink()
        self.items["user"]["running"] = False
        with self.assertRaises(self.d.DeployError):
            guard.recover_once(self.d)
        self.assertEqual(self.mutations(), [])

    def test_pending_latch_still_quarantines_without_policy(self):
        (self.d.STATE / "rollback-policy.json").unlink()
        self.write("security-cutover.json", {**self.latch, "phase": "quarantined"})
        guard.recover_once(self.d)
        self.assertEqual(len(self.mutations()), 4)

    def test_unless_stopped_policy_drift_cannot_open_stopped_edge(self):
        self.items["user"]["restart"] = "unless-stopped"
        self.items["edge"]["running"] = False
        with self.assertRaises(self.d.DeployError):
            guard.recover_once(self.d)
        self.assertEqual(self.mutations(), [])

    def test_maintenance_is_explicit_stop_and_resume_requires_existing_receipt(self):
        self.write("public-maintenance.json", {"schema_version": 1, "instance_id": self.d.INSTANCE_ID, "hold": True})
        guard.recover_once(self.d)
        self.assertEqual(len(self.mutations()), 4)
        (self.d.STATE / "public-maintenance.json").unlink()
        self.calls.clear()
        guard.recover_once(self.d)
        self.assertEqual(len(self.mutations()), 4)
        self.assertTrue(all(x[1] == "start" for x in self.mutations()))

    def test_rolled_back_receipt_requires_independent_rollback_tuple(self):
        self.write("security-cutover.json", {**self.latch, "phase": "rolled_back"})
        with self.assertRaises(self.d.DeployError):
            guard.write_ready_receipt(self.d, self.latch, "rolled_back")
        self.assertEqual(self.mutations(), [])
        self.policy["rollback_verified"] = [self.approved]
        self.write("rollback-policy.json", self.policy)
        guard.write_ready_receipt(self.d, self.latch, "rolled_back")
        guard.recover_once(self.d)
        self.assertEqual(self.mutations(), [])

    def test_missing_earlier_approved_image_does_not_hide_later_exact_rollback(self):
        missing = {s: value[:-64] + "f" * 64 for s, value in self.approved.items()}
        self.policy["rollback_verified"] = [missing, self.approved]
        self.write("rollback-policy.json", self.policy)
        original = self.command
        def absent(args, **kwargs):
            if args[:3] == ["docker", "image", "inspect"] and args[-1].endswith("f" * 64):
                raise self.d.DeployError("absent local image")
            return original(args, **kwargs)
        with patch.object(self.d, "command", side_effect=absent):
            self.assertEqual(guard.approved_tuple(self.d, self.latch, "rolled_back"), self.approved)
        self.assertFalse(any(x[:2] == ("docker", "pull") for x in self.calls))

    def test_enrollment_checks_actual_readiness_before_changing_restart_policy(self):
        for item in self.items.values():
            item["restart"] = "unless-stopped"
        self.d.smoke.side_effect = self.d.DeployError("unhealthy")
        with self.assertRaises(self.d.DeployError):
            guard.enroll(self.d)
        self.assertEqual(self.mutations(), [])
        self.d.smoke.side_effect = None
        guard.enroll(self.d)
        self.assertEqual([x[-1] for x in self.mutations()], [self.items[s]["id"] for s in guard.PUBLIC])

    def test_lock_symlink_is_rejected_before_any_docker_command(self):
        target = self.d.STATE / "target"
        target.write_text("preserve")
        lock = self.d.STATE / "deploy.lock"
        lock.unlink(missing_ok=True)
        lock.symlink_to(target)
        with self.assertRaises(OSError):
            guard.recover_once(self.d)
        self.assertEqual(target.read_text(), "preserve")
        self.assertEqual(self.calls, [])

    def test_service_does_not_order_or_stop_databases_or_docker(self):
        unit = (ROOT / "deploy/map-cutover-watchdog.service").read_text()
        self.assertNotIn("Before=docker", unit)
        self.assertNotIn("Requires=", unit)
        self.assertNotIn("BindsTo=", unit)
        self.assertNotIn("ExecStop=", unit)
        wrapper = (ROOT / "scripts/receive-supervised.sh").read_text()
        for setting in ("KillMode=control-group", "TimeoutStopSec=10", "SendSIGKILL=yes", "RuntimeMaxSec=5400"):
            self.assertIn(setting, wrapper)

    def test_maintenance_blocks_a_new_receiver_before_mutation(self):
        self.write("public-maintenance.json", {"schema_version": 1, "instance_id": self.d.INSTANCE_ID, "hold": True})
        with self.assertRaises(self.d.DeployError):
            guard.require_enrolled(self.d)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
