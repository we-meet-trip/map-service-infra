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

    def test_observability_limits_leave_room_for_the_dashboard_working_set(self):
        """놀고 있을 때 이미 275MiB 를 쓰는 화면 서비스에 상한을 바짝 붙이면
        그릴 때마다 커널이 되찾아 가고, 그 사이 내부 API 가 시간 초과로 끝난다.
        죽지 않으므로 재시작 수에도 남지 않는다. 여유를 계약으로 고정한다."""
        services = self.render("docker-compose.admin.yml")
        floor = {"grafana": 640 * 1024 ** 2, "prometheus": 512 * 1024 ** 2}
        for name, minimum in floor.items():
            limit = services[name]["deploy"]["resources"]["limits"]["memory"]
            self.assertGreaterEqual(int(limit), minimum, f"{name} 상한이 실사용 여유 아래로 내려갔다")
        for name, service in services.items():
            self.assertIn("memory", service.get("deploy", {}).get("resources", {}).get("limits", {}),
                          f"{name} 에 메모리 상한이 없다")

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
