import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('candidate', ROOT / 'scripts/scan-infrastructure-candidates.py')
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
f_spec = importlib.util.spec_from_file_location('fixtures', ROOT / 'security/infrastructure/fixtures.py')
f = importlib.util.module_from_spec(f_spec); f_spec.loader.exec_module(f)


class CandidateContract(unittest.TestCase):
    def test_manifest_keeps_exact_nine_and_preserved_security_images(self):
        data = c.load_spec()
        self.assertEqual(9, len(data['services']))
        self.assertEqual(596, sum(r['prior_counts']['HIGH'] for r in data['services']))
        self.assertEqual(43, sum(r['prior_counts']['CRITICAL'] for r in data['services']))
        self.assertIn('edge', data['preserve']); self.assertIn('osrm_image_id', data['preserve'])

    def test_unfixed_critical_findings_are_counted_and_linked(self):
        actual = c.findings({'Results': [{'Target': 'binary', 'Packages': [{'Name': 'stdlib', 'Version': 'go1.25.1'}],
            'Vulnerabilities': [{'VulnerabilityID': 'CVE-fixture', 'Severity': 'CRITICAL', 'Status': 'affected',
                                'PkgName': 'stdlib', 'InstalledVersion': 'go1.25.1', 'PrimaryURL': 'https://go.dev/security/'}]}]})
        self.assertEqual({'HIGH': 0, 'CRITICAL': 1}, actual['counts'])
        self.assertIsNone(actual['findings'][0]['FixedVersion'])
        self.assertEqual('stdlib', actual['packages'][0]['name'])

    def test_no_local_execution_or_docker_calls(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(c, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'remote_hosted_ci_only'):
                c.execute(c.load_spec(), Path('/never-created'), c.SERVICES)
            run.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch.object(f.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'remote_hosted_ci_only'):
                f.check('proxy', 'ignored', 'ignored', Path('/never-created'))
            run.assert_not_called()

    def test_tampered_oci_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.tar'
            with tarfile.open(path, 'w') as archive:
                index = json.dumps({'manifests': [{'digest': 'sha256:' + 'a' * 64}]}).encode()
                for name, raw in [('index.json', index), ('blobs/sha256/' + 'a' * 64, b'{}')]:
                    member = tarfile.TarInfo(name); member.size = len(raw); archive.addfile(member, io.BytesIO(raw))
            with self.assertRaisesRegex(ValueError, 'oci_blob_checksum'):
                c.oci_identity(path)

    def test_cleanup_rejects_foreign_container(self):
        sandbox = f.Sandbox(Path('/unused')); sandbox.containers = ['foreign']
        with patch.object(sandbox, 'run', return_value=json.dumps([{'Config': {'Labels': {}}}]).encode()) as run:
            with self.assertRaisesRegex(ValueError, 'container_owner_mismatch'):
                sandbox.clean()
            self.assertEqual(1, run.call_count)

    def test_unexpected_scan_exit_is_not_zero_findings(self):
        with patch.object(c.subprocess, 'run') as run:
            run.return_value.returncode = 2
            with self.assertRaisesRegex(ValueError, 'command_failed'):
                c.run(['docker'], accepted=(0, 1))


if __name__ == '__main__': unittest.main()
