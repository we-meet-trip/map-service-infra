import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("role_validator_compose", ROOT / "scripts/verify-role-manifest.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


@unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI not installed")
class RenderedRoleComposeTests(unittest.TestCase):
    def test_real_compose_render_passes_role_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="map-role-config-") as directory:
            temporary = Path(directory)
            runtime = temporary / "runtime.env"
            migration = temporary / "migration.env"
            secret = temporary / "bootstrap.txt"
            secret.write_text("synthetic-config-only")
            target = {"USER_BASE_URL":"https://test-management.private.example/user", "HUB_BASE_URL":"https://test-management.private.example/hub",
                      "AGENT_BASE_URL":"https://test-management.private.example/agent", "INTERNAL_SERVICE_TOKEN":"synthetic-only", "USER_ADMIN_INTERNAL_TOKEN":"synthetic-admin-only"}
            runtime.write_text("ADMIN_CONTROL_DATABASE_URL=postgresql+psycopg://map_admin_runtime:synthetic@control-postgres/admin_control\nADMIN_ENVIRONMENT=test\nADMIN_TARGETS=" + json.dumps({"test":target}) + "\n")
            migration.write_text("ADMIN_CONTROL_MIGRATION_DATABASE_URL=postgresql+psycopg://map_admin_migrator:synthetic@control-postgres/admin_control\n")
            images = {name:"synthetic/image@sha256:"+"a"*64 for name in ("ADMIN_CONTROL_POSTGRES_IMAGE","ADMIN_API_IMAGE","ADMIN_WEB_IMAGE","ADMIN_PROMETHEUS_IMAGE","ADMIN_GRAFANA_IMAGE","TRAINING_WORKER_IMAGE")}
            values = {**images,"ADMIN_MIGRATION_ENV_FILE":str(migration),"ADMIN_RUNTIME_ENV_FILE":str(runtime),
                      "ADMIN_CONTROL_PROVISIONER_PASSWORD_FILE":str(secret),"ADMIN_GRAFANA_PASSWORD_FILE":str(secret),
                      "ADMIN_CONTROL_VOLUME":"central-control-only","ADMIN_PROMETHEUS_VOLUME":"central-metrics-only","ADMIN_GRAFANA_VOLUME":"central-grafana-only",
                      "ADMIN_MONITORING_CONFIG_DIR":str(temporary),"ADMIN_GRAFANA_PROVISIONING_DIR":str(temporary),
                      "TRAINING_INPUT_DIR":str(temporary),"TRAINING_OUTPUT_DIR":str(temporary / "output"),
                      "TRAINING_PROGRAM_FILE":str(ROOT / "training/segment_stats.py")}
            envfile = temporary / "compose.env"
            envfile.write_text("\n".join(f"{key}={value}" for key,value in values.items()))
            for role, scope in (("admin","control"),("learning","synthetic")):
                compose = ROOT / f"docker-compose.role-{role}.yml"
                run = subprocess.run(["docker","compose","--profile","*","--env-file",str(envfile),"-f",str(compose),"config","--format","json"],
                                     capture_output=True,text=True,env={**os.environ,**values},timeout=20)
                self.assertEqual(run.returncode,0,run.stderr)
                manifest = {"schema_version":1,"role":role,"data_scope":scope,"host_identity":role+"-isolated-1","deploy_account":"map-"+role+"-deploy",
                            "compose_sha256":hashlib.sha256(compose.read_bytes()).hexdigest()}
                result = validator.validate(manifest,json.loads(run.stdout),host_identity=manifest["host_identity"],deploy_account=manifest["deploy_account"])
                self.assertTrue(result["valid"])
                if role == "admin":
                    rendered = json.loads(run.stdout)
                    for invalid in ("", " ", "synthetic-only"):
                        bad_target = {**target, "USER_ADMIN_INTERNAL_TOKEN":invalid}
                        rendered["services"]["admin-api"]["environment"]["ADMIN_TARGETS"] = json.dumps({"test":bad_target})
                        with self.assertRaisesRegex(ValueError, "distinct target User admin credential required"):
                            validator.validate(manifest,rendered,host_identity=manifest["host_identity"],deploy_account=manifest["deploy_account"])
