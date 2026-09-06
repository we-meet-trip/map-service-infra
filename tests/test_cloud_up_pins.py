"""Exercise the real startup shell before any stateful service can start."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class InfrastructurePinStartupTests(unittest.TestCase):
    def run_startup(self, pinned=True, missing_pin=False, detached=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts/cloud-up.sh", root / "scripts/cloud-up.sh")
            (root / ".env.test").write_text(
                "POSTGRES_PASSWORD=synthetic\nHUB_DATABASE_URL=synthetic\n"
                "GEMINI_API_KEY=synthetic\nADMIN_DATABASE_URL=synthetic\n"
                "MAP_ADMIN_PASSWORD=synthetic\n")
            pins = root / "pins"
            pins.mkdir()
            for name in ("compose.images.yml", "compose.admin-images.yml",
                         "compose.infrastructure.yml", "compose.admin-infrastructure.yml"):
                if not (missing_pin and name == "compose.admin-infrastructure.yml"):
                    (pins / name).write_text("services: {}\n")
            binary = root / "bin"
            binary.mkdir()
            docker = binary / "docker"
            docker.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                              "with open(os.environ['CALL_LOG'],'a') as f:\n"
                              " f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                              "sys.exit(77 if 'up' in sys.argv else 0)\n")
            docker.chmod(0o700)
            log = root / "calls.jsonl"
            env = {k: v for k, v in os.environ.items()
                   if k not in ("RELEASE_BUNDLE", "INFRA_IMAGE_BUNDLE")}
            env.update(PATH=str(binary) + os.pathsep + os.environ["PATH"], CALL_LOG=str(log))
            if pinned:
                env.update(RELEASE_BUNDLE=str(pins), INFRA_IMAGE_BUNDLE=str(pins))
            role_args = ['--target-exporters'] if detached else ['--admin', '--monitoring']
            result = subprocess.run(["bash", "scripts/cloud-up.sh", "--test", "--registry",
                                     "--vision", "--edge", *role_args],
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
