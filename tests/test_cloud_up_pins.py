"""Exercise the real startup shell with a docker that only records its calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class InfrastructurePinStartupTests(unittest.TestCase):
    def run_startup(self, pinned=True, missing_pin=False, detached=False, extra=(), start=False, rollover=False):
        # Without `start` the first start fails, so the run ends before anything
        # stateful. With it every call succeeds and the run goes through the
        # application start, all at once or, with `rollover`, one service at a time.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts/lib").mkdir(parents=True)
            shutil.copyfile(ROOT / "scripts/cloud-up.sh", root / "scripts/cloud-up.sh")
            # Stand-ins for the backup, migration and replacement steps on the way.
            drain = "import sys; sys.stdin.read()\n"
            for name, body in (("pg-backup.sh", "#!/bin/sh\n"), ("service-migration-job.py", drain),
                               ("service-rollover.py", drain), ("lib/migrations.sh", "verify_hub_revision() { :; }\n")):
                (root / "scripts" / name).write_text(body)
            (root / "scripts/pg-backup.sh").chmod(0o700)
            (root / ".env.test").write_text(
                "POSTGRES_PASSWORD=synthetic\nHUB_DATABASE_URL=synthetic\n"
                "GEMINI_API_KEY=synthetic\nADMIN_DATABASE_URL=synthetic\n"
                "MAP_ADMIN_PASSWORD=synthetic\nUSER_DATABASE_USER=map_user_runtime\nUSER_DATABASE_PASSWORD=synthetic-runtime\n"
                "POSTGRES_USER=synthetic\nPOSTGRES_DB=synthetic\n")
            pins = root / "pins"
            pins.mkdir()
            for name in ("compose.images.yml", "compose.admin-images.yml",
                         "compose.infrastructure.yml", "compose.admin-infrastructure.yml"):
                if not (missing_pin and name == "compose.admin-infrastructure.yml"):
                    (pins / name).write_text("services: {}\n")
            binary = root / "bin"
            binary.mkdir()
            docker = binary / "docker"
            # Records each call and answers the two reads the script acts on:
            # the hub table count and the rendered project name.
            docker.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                              "with open(os.environ['CALL_LOG'],'a') as f:\n"
                              " f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                              "if 'information_schema' in ' '.join(sys.argv): print(1)\n"
                              "if 'config' in sys.argv: print(json.dumps({'name': 'map'}))\n"
                              + ("" if start else "sys.exit(77 if 'up' in sys.argv else 0)\n"))
            docker.chmod(0o700)
            # The service-by-service start creates its receipt directory in the host's
            # real deployment state; a no-op mkdir keeps the run from touching it.
            (binary / "mkdir").write_text("#!/bin/sh\n")
            (binary / "mkdir").chmod(0o700)
            log = root / "calls.jsonl"
            env = {k: v for k, v in os.environ.items()
                   if k not in ("RELEASE_BUNDLE", "INFRA_IMAGE_BUNDLE", "CUTOVER_ROLLOVER")}
            env.update(PATH=str(binary) + os.pathsep + os.environ["PATH"], CALL_LOG=str(log))
            if pinned:
                env.update(RELEASE_BUNDLE=str(pins), INFRA_IMAGE_BUNDLE=str(pins))
            if rollover:
                env.update(CUTOVER_ROLLOVER="1", ROLLOVER_PROBE_ORIGIN="http://127.0.0.1:8290",
                           PROXY_UPSTREAMS_DIR=str(root / "upstreams"))
            role_args = ['--target-exporters'] if detached else ['--admin', '--monitoring']
            result = subprocess.run(["bash", "scripts/cloud-up.sh", "--test", "--registry",
                                     "--vision", "--edge", *extra, *role_args],
                                    cwd=root, env=env, capture_output=True, text=True)
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, calls

    def test_automatic_pull_only_fetches_six_applications(self):
        result, calls = self.run_startup()
        self.assertEqual(result.returncode, 77)
        pulls = [call for call in calls if "pull" in call]
        self.assertEqual([call[call.index("pull") + 1:] for call in pulls],
                         [["user", "agent", "hub", "yolo"], ["admin", "admin-web"]])
        self.assertTrue(all(any("infrastructure.yml" in arg for arg in call) for call in calls))
        starts = [call for call in calls if "up" in call]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][starts[0].index("up") + 1:],
                         ["-d", "--no-recreate", "postgres", "redis"])

    def service_calls(self, calls):
        return [call for call in calls if call[:1] == ["compose"] and "docker-compose.yml" in call]

    def started(self, calls):
        # What each start that waits for health names, in order.
        return [call[call.index("--wait-timeout") + 2:] for call in self.service_calls(calls) if "--wait" in call]

    def test_edge_alone_never_merges_or_enables_dynamic_dns(self):
        # A host that owns its DNS record has no updater token; the edge must
        # render and start without one, whether the stack starts at once or
        # one service at a time.
        for rollover, starts in ((False, [["user", "agent", "hub", "proxy", "yolo", "edge"]]),
                                 (True, [["proxy"], ["edge"]])):
            with self.subTest(rollover=rollover):
                result, calls = self.run_startup(start=True, rollover=rollover)
                self.assertEqual(result.returncode, 0, result.stderr)
                for call in self.service_calls(calls):
                    self.assertIn("docker-compose.edge.yml", call)
                    self.assertNotIn("docker-compose.dns.yml", call)
                    self.assertNotIn("dns", call)
                self.assertEqual(self.started(calls), starts)

    def test_dns_flag_adds_only_its_file_and_profile(self):
        for rollover, starts in ((False, [["user", "agent", "hub", "proxy", "yolo", "edge", "dns"]]),
                                 (True, [["proxy"], ["edge"], ["dns"]])):
            with self.subTest(rollover=rollover):
                result, calls = self.run_startup(extra=("--dns",), start=True, rollover=rollover)
                self.assertEqual(result.returncode, 0, result.stderr)
                services = self.service_calls(calls)
                for call in services:
                    self.assertIn("docker-compose.dns.yml", call)
                pull = next(call for call in services if "pull" in call)
                self.assertEqual(pull[pull.index("pull") + 1:], ["user", "agent", "hub", "yolo"])
                profiles = [pull[index + 1] for index, value in enumerate(pull) if value == "--profile"]
                self.assertEqual(profiles, ["full", "vision", "edge", "dns"])
                self.assertEqual(self.started(calls), starts)

    def test_missing_infrastructure_pin_stops_before_docker(self):
        result, calls = self.run_startup(missing_pin=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])

    def test_fresh_public_edge_requires_verified_artifact_before_any_pull(self):
        result, calls = self.run_startup(pinned=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('install-caddy-artifact.py', result.stderr)
        self.assertEqual(calls, [])

    def test_application_role_never_pulls_or_merges_retired_admin_images(self):
        result, calls = self.run_startup(detached=True)
        self.assertEqual(result.returncode, 77)
        pulls = [call for call in calls if 'pull' in call]
        self.assertEqual([call[call.index('pull') + 1:] for call in pulls],
                         [['user', 'agent', 'hub', 'yolo']])
        self.assertFalse(any('admin' in call or 'admin-web' in call for call in calls))
        self.assertFalse(any(any('compose.admin-images' in item for item in call) for call in calls))


if __name__ == "__main__":
    unittest.main()
