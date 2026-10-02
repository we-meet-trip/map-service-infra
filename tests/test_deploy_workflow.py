"""The GCP test deployment stays keyless and confined to its environment."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


class KeylessDeploymentWorkflowTests(unittest.TestCase):
    def test_no_workflow_reads_a_service_account_key(self):
        for path in sorted(WORKFLOWS.glob("*.y*ml")):
            text = path.read_text()
            with self.subTest(workflow=path.name):
                self.assertNotIn("credentials_json", text)
                self.assertNotIn("GCP_SA_KEY", text)

    def test_deploy_job_federates_only_after_artifact_verification(self):
        text = (WORKFLOWS / "deploy.yml").read_text()
        self.assertIn("\npermissions: {}\n", text)
        # Repository-level secrets are outside the environment's branch policy.
        self.assertNotIn("mapcenter-b59ca", text)
        self.assertIsNone(re.search(r"secrets\.(?!TEST_DEPLOY_SSH_)", text))
        job = text[text.index("\n  deploy:\n"):]
        for required in ("environment: gcp-test", "contents: read", "id-token: write", "actions: read",
                         "workload_identity_provider: ${{ vars.TEST_WIF_PROVIDER }}",
                         "service_account: ${{ vars.TEST_DEPLOYER_SA }}",
                         "secrets.TEST_DEPLOY_SSH_KEY", "secrets.TEST_DEPLOY_SSH_KNOWN_HOSTS",
                         "GCP_PROJECT: ${{ vars.TEST_GCP_PROJECT }}", "GCP_ZONE: ${{ vars.TEST_GCP_ZONE }}",
                         "INSTANCE: ${{ vars.TEST_INSTANCE }}", '"mapdeploy@$INSTANCE"',
                         "start-iap-tunnel $INSTANCE 22 --listen-on-stdin --project=$GCP_PROJECT --zone=$GCP_ZONE",
                         "HostKeyAlias=map-test-deploy"):
            self.assertIn(required, job)
        self.assertEqual(job.count("project_id: ${{ vars.TEST_GCP_PROJECT }}"), 2)
        self.assertLess(job.index("deploy-gcp.py prepare"), job.index("google-github-actions/auth@"))
        # The variables reach the shell that runs ProxyCommand only after the format guard.
        guard = "=~ ^[a-z][-a-z0-9]{4,28}[a-z0-9]$"
        self.assertIn(guard, job)
        self.assertLess(job.index(guard), job.index("ssh -T"))


def enrollment():
    """The one-time host enrollment section, with shell line continuations joined."""
    text = (ROOT / "docs" / "CUTOVER_SUPERVISOR.md").read_text()
    start = text.index("## New test host enrollment (one time)")
    return re.sub(r"\\\n\s*", " ", text[start:text.index("\n## ", start)])


def loaded_siblings(script, seen):
    """Python files a script loads from its own directory, followed transitively."""
    for name in re.findall(r"""with_name\(["']([\w.-]+\.py)["']\)""", (ROOT / "scripts" / script).read_text()):
        if name not in seen:
            seen.add(name)
            loaded_siblings(name, seen)
    return seen


class DeploymentDocumentTests(unittest.TestCase):
    def test_enrollment_steps_run_in_a_working_order(self):
        section = enrollment()
        # .env.test exists before PostgreSQL starts alone; DNS resolves and a verified
        # bundle pins every image before the first stack; the account exists before
        # its key; the branch policy is confirmed before environment secrets are stored.
        order = ("scripts/make-test-env.sh", "--profile infra up -d postgres",
                 "dig +short test-api.mapservice.app", "deploy-gcp.py prepare", "RELEASE_BUNDLE=",
                 "./scripts/cloud-up.sh", "useradd --create-home --shell /bin/sh mapdeploy",
                 "authorized_keys", "deployment_branch_policy", "TEST_DEPLOY_SSH_KNOWN_HOSTS")
        self.assertEqual([landmark for landmark in order if landmark not in section], [])
        self.assertEqual(sorted(order, key=section.find), list(order))
        compose = re.search(r"docker compose (.*) --profile infra up -d postgres", section)[1]
        for name in re.findall(r"(?<!\S)-f (\S+)", compose):
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertIn("${RELEASE_BUNDLE:-}", (ROOT / "scripts" / "cloud-up.sh").read_text())

    def test_documented_script_calls_match_their_parsers(self):
        checked = set()
        calls = re.findall(r"python3 (?:scripts/|/usr/local/lib/map-deploy/)([\w-]+\.py)([^`\n]*)", enrollment())
        for script, arguments in calls:
            source = (ROOT / "scripts" / script).read_text()
            options = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", arguments))
            for option in options:
                self.assertRegex(source, rf"""['"]{option}['"]""", f"{script} defines no {option}")
            # These two parsers have one command each, so every required option applies.
            if script in ("deploy-gcp.py", "osrm-systemd.py") and options:
                checked.add(script)
                required = set(re.findall(r"""add_argument\(["'](--[a-z-]+)["'][^\n]*required=True""", source))
                self.assertLessEqual(required, options, script)
        self.assertEqual(checked, {"deploy-gcp.py", "osrm-systemd.py"})

    def test_documents_cover_what_the_code_loads_and_retires(self):
        redis = (ROOT / "docs" / "REDIS_OFFHOST_BACKUP.md").read_text()
        install = sorted(loaded_siblings("backup_job.py", set()))
        self.assertEqual([name for name in install if f"`scripts/{name}`" not in redis], [])
        cloud = (ROOT / "CLOUD_DEPLOY.md").read_text()
        manual = next(block for block in cloud.split("\n\n") if "수동 첫 기동" in block)
        retire = ["gh workflow disable 352153768"] + [
            f"gh secret delete {name}" for name in ("GCP_SA_KEY", "DEPLOY_SSH_KEY", "DEPLOY_SSH_KNOWN_HOSTS")]
        self.assertEqual([step for step in retire if step not in cloud], [])
        self.assertTrue("`RELEASE_BUNDLE`" in manual, "manual first start names no release bundle")


if __name__ == "__main__":
    unittest.main()
