"""Render the actual service boundary with synthetic values; never contact servers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless(shutil.which("docker"),"Docker Compose CLI required")
class AdminTokenRuntimeTests(unittest.TestCase):
    def render(self, compose, **values):
        env={**os.environ,"USER_ADMIN_INTERNAL_TOKEN":"synthetic-management-only",**values}
        run=subprocess.run(["docker","compose","--env-file",".env.example","-f",compose,
                            "--profile","*","config","--format","json"],cwd=ROOT,env=env,
                           capture_output=True,text=True,timeout=20)
        self.assertEqual(run.returncode,0,"Compose must render")
        return json.loads(run.stdout)["services"]

    def test_management_secret_only_reaches_user_and_admin(self):
        services=self.render("docker-compose.yml")
        self.assertEqual(services["user"]["environment"]["USER_ADMIN_INTERNAL_TOKEN"],"synthetic-management-only")
        for name,service in services.items():
            if name!="user": self.assertNotIn("USER_ADMIN_INTERNAL_TOKEN",service.get("environment",{}))
        admin=self.render("docker-compose.admin.yml")
        self.assertEqual(admin["admin"]["environment"]["USER_ADMIN_INTERNAL_TOKEN"],"synthetic-management-only")
        for name,service in admin.items():
            if name!="admin": self.assertNotIn("USER_ADMIN_INTERNAL_TOKEN",service.get("environment",{}))

    def test_directions_budget_and_user_normal_budget_ignore_agent_budget(self):
        services=self.render("docker-compose.yml",HUB_TIMEOUT_SECONDS="29",USER_HUB_TIMEOUT_SECONDS="5",HUB_DIRECTIONS_TIMEOUT_SECONDS="20")
        self.assertEqual(services["agent"]["environment"]["HUB_TIMEOUT_SECONDS"],"29")
        self.assertEqual(services["user"]["environment"]["HUB_TIMEOUT_SECONDS"],"5")
        self.assertEqual(services["user"]["environment"]["HUB_DIRECTIONS_TIMEOUT_SECONDS"],"20")
        services=self.render("docker-compose.yml",USER_HUB_TIMEOUT_SECONDS="6",HUB_DIRECTIONS_TIMEOUT_SECONDS="21")
        self.assertEqual(services["user"]["environment"]["HUB_TIMEOUT_SECONDS"],"6")
        self.assertEqual(services["user"]["environment"]["HUB_DIRECTIONS_TIMEOUT_SECONDS"],"21")

    def test_runtime_caps_and_privilege_escalation_are_removed_without_changing_data(self):
        services=self.render("docker-compose.yml")
        for name in ("user","agent","hub","yolo"):
            self.assertEqual(services[name]["cap_drop"],["ALL"])
            self.assertEqual(services[name]["security_opt"],["no-new-privileges:true"])
            self.assertFalse(services[name].get("cap_add"))
        for name in ("postgres","redis"):
            self.assertNotIn("cap_drop",services[name])
        admin=self.render("docker-compose.admin.yml")
        self.assertEqual(admin["admin"]["cap_drop"],["ALL"])
        for name in ("admin","admin-web"):
            self.assertEqual(admin[name]["security_opt"],["no-new-privileges:true"])
        # Stock nginx root master still changes uid/gid for workers; only NNP until a nonroot image is validated.
        self.assertNotIn("cap_drop",admin["admin-web"])
