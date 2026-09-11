import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
worker = module("dataset_worker", "dataset-worker.py")
validator = module("role_validator", "verify-role-manifest.py")


class DatasetWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.output.mkdir(mode=0o700)
        self.program = ROOT / "training/segment_stats.py"
        rows = [{"schema_version":1, "l1_eligible":True, "user_segment":{"age_band":"synthetic", "gender":"unknown"},
                 "candidates":[{"content_id":"synthetic-place", "saved":True}]} for _ in range(3)]
        self.data = self.root / "sessions.jsonl"
        self.data.write_text("\n".join(json.dumps(r) for r in rows))
        self.job = {"schema_version":1,"job_id":"synthetic-check","data_scope":"synthetic","approved":True,
                    "dataset":self.data.name,"dataset_sha256":hashlib.sha256(self.data.read_bytes()).hexdigest(),
                    "rows":3,"max_bytes":1048576,"max_seconds":30,"min_support":3}
        self.job_path = self.root / "job.json"

    def execute(self):
        self.job_path.write_text(json.dumps(self.job))
        with patch.dict(os.environ, {}, clear=True):
            return worker.run(self.job_path, self.root, self.output, self.program)

    def test_actual_pinned_aggregator_candidate_checksum_and_idempotency(self):
        result = self.execute()
        self.assertEqual(result["status"], "candidate")
        candidate = self.output / self.job["job_id"] / "candidate.json"
        self.assertEqual(hashlib.sha256(candidate.read_bytes()).hexdigest(), result["candidate_sha256"])
        self.assertEqual(json.loads(candidate.read_text())["sessions_used"], 3)
        self.assertFalse(result["serving_promotion"])
        self.assertEqual(self.execute()["status"], "already_completed")
        self.assertEqual(candidate.stat().st_mode & 0o777, 0o600)

    def test_real_user_and_unapproved_jobs_remain_on_hold(self):
        for key, value in (("data_scope", "real-user"), ("approved", False)):
            original = self.job[key]
            self.job[key] = value
            with self.assertRaises(ValueError): self.execute()
            self.job[key] = original
        self.assertEqual(list(self.output.iterdir()), [])

    def test_digest_traversal_row_and_budget_mismatches_reject(self):
        for key, value in (("dataset_sha256", "0"*64), ("dataset", "../outside.jsonl"), ("rows",4), ("max_bytes",1), ("max_seconds",301)):
            original = self.job[key]
            self.job[key] = value
            with self.assertRaises(ValueError): self.execute()
            self.job[key] = original

    def test_changed_program_never_executes(self):
        self.program = self.root / "unapproved.py"
        self.program.write_text("raise RuntimeError('must not execute')")
        with self.assertRaisesRegex(ValueError, "program digest"): self.execute()

    def test_conflicting_reuse_cannot_replace_candidate(self):
        self.execute()
        self.job["min_support"] = 4
        with self.assertRaisesRegex(ValueError, "conflicting"): self.execute()

    def test_serving_secret_environment_is_rejected(self):
        self.job_path.write_text(json.dumps(self.job))
        for key in ("LOCATION_CRYPTO_KEYS", "USER_ADMIN_INTERNAL_TOKEN", "HUB_ADMIN_INTERNAL_TOKEN"):
            with patch.dict(os.environ, {key:"synthetic-forbidden"}, clear=True):
                with self.assertRaisesRegex(ValueError, "credentials forbidden"):
                    worker.run(self.job_path, self.root, self.output, self.program)
            self.assertEqual(list(self.output.iterdir()), [])


class RoleManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"schema_version":1,"role":"learning","data_scope":"synthetic","host_identity":"learning-isolated-1",
                         "deploy_account":"map-learning-deploy","compose_sha256":"0"*64}
        self.worker = {"entrypoint":["python","/opt/map/dataset-worker.py"], "command":["--job","/input/job.json","--dataset-root","/input","--output-root","/output","--program","/opt/map/segment_stats.py"], "security_opt":["no-new-privileges:true"],"image":"python@sha256:"+"a"*64,"network_mode":"none","read_only":True,"user":"10001:10001",
                       "cap_drop":["ALL"],"environment":{"MAP_LEARNING_HOLD":"true"},"mem_limit":536870912,"cpus":1,"pids_limit":64,
                       "volumes":[{"source":"/private/"+target.split("/")[-1],"target":target,"read_only":target != "/output"}
                                  for target in ("/input","/output","/opt/map/dataset-worker.py","/opt/map/segment_stats.py")]}

    def validate(self, config=None, **kwargs):
        return validator.validate(self.manifest, config or {"services":{"dataset-worker":self.worker}},
                                  host_identity=kwargs.get("host_identity","learning-isolated-1"),deploy_account="map-learning-deploy")

    def test_valid_offline_role_does_not_claim_host_exists(self):
        self.assertFalse(self.validate()["independent_host_provisioned"])

    def test_worker_dangerous_access_and_unbounded_resources_reject(self):
        for key, value in (("network_mode","host"),("environment",{"REDIS_PASSWORD":"synthetic"}),("image","python:latest"),
                           ("user","root"),("mem_limit",0),("cpus",4),("privileged",True),("secrets",["source_key"])):
            original = copy.deepcopy(self.worker)
            self.worker[key] = value
            with self.assertRaises(ValueError): self.validate()
            self.worker = original

    def test_wrong_host_and_real_scope_reject(self):
        with self.assertRaises(ValueError): self.validate(host_identity="application-test-1")
        self.manifest["data_scope"] = "production"
        with self.assertRaises(ValueError): self.validate()

    def test_cross_host_api_contract_rejects_docker_dns_and_cleartext(self):
        for url in ("http://user:8080", "https://user:8080", "https://127.0.0.1", "https://u:p@api.private.example"):
            with self.assertRaises(ValueError): validator.remote_https(url)
        validator.remote_https("https://test-management.private.example")

    def test_remote_monitoring_requires_host_tls_auth_and_environment(self):
        job = {"scheme":"https", "authorization":{"credentials_file":"/etc/prometheus/credentials/test.token"},
               "static_configs":[{"targets":["test-management.private.example:443"],"labels":{"map_environment":"test"}}]}
        self.assertTrue(validator.validate_scrapes({"scrape_configs":[job]}))
        for key, value in (("scheme","http"),("authorization",{}),("tls_config",{"insecure_skip_verify":True}),
                           ("static_configs",[{"targets":["postgres-exporter:9187"],"labels":{"map_environment":"test"}}])):
            bad = copy.deepcopy(job)
            bad[key] = value
            with self.assertRaises(ValueError): validator.validate_scrapes({"scrape_configs":[bad]})

if __name__ == "__main__": unittest.main()
