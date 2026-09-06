import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_release_manifest import fixture

ROOT = Path(__file__).resolve().parent.parent
with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
    spec = importlib.util.spec_from_file_location('scan_release_images', ROOT / 'scripts/scan_release_images.py')
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)


def report(data, service='user', severity=None):
    entry = data['services'][service]
    ref = audit.reference(entry)
    results = [{'Class': 'os-pkgs', 'Type': 'debian', 'Vulnerabilities': []}]
    if service != 'admin-web':
        results.append({'Class': 'lang-pkgs', 'Type': 'jar' if service == 'user' else 'python-pkg',
                        'Vulnerabilities': []})
    if severity:
        results[0]['Vulnerabilities'].append({'VulnerabilityID': 'CVE-2026-12345', 'PkgName': 'test-package',
                                              'InstalledVersion': '1.0', 'Severity': severity})
    return {'SchemaVersion': 2, 'Trivy': {'Version': '0.74.0'}, 'ArtifactType': 'container_image',
            'ArtifactName': ref, 'CreatedAt': datetime.now(timezone.utc).isoformat(),
            'Metadata': {'Reference': ref, 'RepoDigests': [ref], 'ImageID': 'sha256:' + 'e' * 64,
                         'Size': 1048576, 'ImageConfig': {'os': 'linux', 'architecture': 'amd64', 'config': {'Labels': {
                             'org.opencontainers.image.revision': entry['source_sha'],
                             'org.opencontainers.image.source': 'https://github.com/' + entry['source_repo'],
                             'org.opencontainers.image.version': data['release_tag']}}}}, 'Results': results}


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.entry = self.data['services']['user']

    def parse(self, document, **kwargs):
        return audit.parse_report(document, self.entry, self.data['release_tag'], **kwargs)

    def test_size_identity_and_unfixed_critical_are_kept(self):
        d = report(self.data, severity='CRITICAL')
        value = self.parse(d)
        self.assertEqual(value['counts'], {'HIGH': 0, 'CRITICAL': 1})
        self.assertEqual(value['trivy_metadata_size_bytes'], 1048576)
        self.assertEqual(value['reference'], audit.reference(self.entry))

    def test_mutable_foreign_confusable_and_injected_digest_fail_before_execution(self):
        digests = ['latest', 'sha256:' + 'a' * 63, 'sha256:' + 'A' * 64,
                   'sha256:' + 'a' * 64 + '\n', 'sha256:' + 'a' * 64 + ':latest',
                   'sha256:' + 'a' * 64 + '@sha256:' + 'b' * 64, '--output=/tmp/inject', None]
        for digest in digests:
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                audit.reference({**self.entry, 'digest': digest})
        for image in ['ghcr.io.evil/we-meet-trip/map-service-user', self.entry['image'] + ':latest',
                      self.entry['image'] + '/../../evil', 'ghcr.io/other/map-service-user']:
            with self.subTest(image=image), self.assertRaises(ValueError):
                audit.reference({**self.entry, 'image': image})

    def test_forged_report_reference_platform_source_or_scanner_fails(self):
        changes = [lambda d: d.update(ArtifactName=self.entry['image'] + ':tag'),
                   lambda d: d['Metadata'].update(Reference=self.entry['image'] + ':tag'),
                   lambda d: d['Metadata'].update(RepoDigests=[self.entry['image'] + '@sha256:' + 'f' * 64]),
                   lambda d: d['Metadata'].update(RepoDigests=audit.reference(self.entry)),
                   lambda d: d['Metadata']['ImageConfig'].update(architecture='arm64'),
                   lambda d: d['Metadata']['ImageConfig'].update(os='windows'),
                   lambda d: d['Metadata']['ImageConfig']['config']['Labels'].update({'org.opencontainers.image.revision':'f'*40}),
                   lambda d: d['Metadata']['ImageConfig']['config']['Labels'].update({'org.opencontainers.image.source':'https://evil.invalid'}),
                   lambda d: d['Metadata']['ImageConfig']['config']['Labels'].update({'org.opencontainers.image.version':'wrong'}),
                   lambda d: d['Trivy'].update(Version='0.73.0'),
                   lambda d: d.update(SchemaVersion=2.0), lambda d: d.update(ArtifactType='filesystem')]
        for change in changes:
            d = report(self.data); change(d)
            with self.subTest(change=changes.index(change)), self.assertRaises(ValueError):self.parse(d)

    def test_missing_package_scope_and_unexpected_severity_fail(self):
        for results in [None, [], [{'Class':'lang-pkgs','Type':'jar'}], [{'Class':'os-pkgs','Type':'debian'}]]:
            d = report(self.data);d['Results'] = results
            with self.subTest(results=results), self.assertRaises(ValueError):self.parse(d)
        for severity in ['UNKNOWN','LOW','CRITICAL\n',None]:
            d = report(self.data,severity='HIGH');d['Results'][0]['Vulnerabilities'][0]['Severity'] = severity
            with self.subTest(severity=severity), self.assertRaises(ValueError):self.parse(d)

    def test_invalid_size_time_and_stale_reports_fail(self):
        for size in [0,-1,True,1.2,float('nan'),None,101*1024**3]:
            d=report(self.data);d['Metadata']['Size']=size
            with self.subTest(size=size), self.assertRaises(ValueError):self.parse(d)
        for time in ['not-a-time','2026-09-07T01:00:00','2026-09-07T01:00:00+09:00']:
            d=report(self.data);d['CreatedAt']=time
            with self.subTest(time=time), self.assertRaises(ValueError):self.parse(d)
        d=report(self.data);d['CreatedAt']=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):self.parse(d,started=datetime.now(timezone.utc))

    def test_duplicate_keys_nonfinite_and_symlink_files_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'input.json'
            for raw in ['{"critical":1,"critical":0}', '{"size":NaN}', '{"size":Infinity}']:
                p.write_text(raw)
                with self.assertRaises(ValueError):audit.read_json(p)
            alias=Path(temp)/'alias.json';alias.symlink_to(p)
            with self.assertRaises(ValueError):audit.read_json(alias)

    def test_command_keeps_scope_and_passes_no_secret_or_docker_socket(self):
        ref=audit.reference(self.entry)
        args=audit.docker_scan_args(ref,Path('/tmp/out'),Path('/tmp/cache'),'user')
        self.assertEqual(args[-1],ref)
        self.assertIn(audit.SCANNER,args)
        self.assertIn('--ignore-unfixed=false',args)
        self.assertEqual(args[args.index('--image-src')+1],'remote')
        self.assertEqual(args[args.index('--severity')+1],'HIGH,CRITICAL')
        self.assertEqual(args[args.index('--scanners')+1],'vuln')
        self.assertNotIn('/var/run/docker.sock',' '.join(args))
        for bad in [ref+':latest',ref+'\n',self.entry['image']+':latest']:
            with self.assertRaises(ValueError):audit.docker_scan_args(bad,Path('/tmp/o'),Path('/tmp/c'),'user')
        with self.assertRaises(ValueError):audit.docker_scan_args(ref,Path('/tmp/o'),Path('/tmp/c'),'../user')

    def test_command_errors_never_expose_registry_output(self):
        with patch.object(audit.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'secret-stdout','secret-token')):
            with self.assertRaisesRegex(ValueError,'^scanner command failed$'):audit.command(['docker','run'])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.bundle=self.root/'bundle';self.output=self.root/'audit';self.cache=self.root/'cache'
        self.data=fixture();audit.release.write_bundle(self.bundle,self.data)

    def tearDown(self):self.temp.cleanup()

    def scanner(self,severity=None,fail_service=None):
        def fake(args):
            ref=args[-1]
            service=next(s for s,e in self.data['services'].items() if audit.reference(e)==ref)
            if service==fail_service:raise ValueError('secret scanner response')
            (self.output/(service+'.json')).write_text(json.dumps(report(self.data,service,severity)))
            (self.cache/'db').mkdir(exist_ok=True)
            (self.cache/'db/metadata.json').write_text(json.dumps({'Version':2,'UpdatedAt':datetime.now(timezone.utc).isoformat()}))
        return fake

    def run_scan(self,severity=None,fail_service=None):
        with patch.object(audit,'command',side_effect=self.scanner(severity,fail_service)) as command:
            summary=audit.scan_bundle(self.bundle,self.output,self.cache)
        self.assertEqual(command.call_count,6)
        return summary

    def refresh_checksums(self):
        (self.output/'SHA256SUMS').write_text(audit.checksums(self.output))

    def test_complete_six_digest_audit_allows_high_for_gcp_but_never_ncp(self):
        result=self.run_scan('HIGH')
        self.assertEqual(result['counts'],{'HIGH':6,'CRITICAL':0})
        self.assertEqual(result['security_status'],'FINDINGS_REQUIRE_REVIEW')
        self.assertTrue(result['gcp_test_gate']);self.assertFalse(result['ncp_residual_risk_accepted'])
        self.assertEqual(audit.enforce(self.bundle,self.output),result)
        self.assertEqual(set(result['services']),set(audit.release.SERVICES))
        self.assertTrue((self.output/'scanner-db-metadata.json').is_file())

    def test_critical_blocks_only_after_all_raw_reports_are_retained(self):
        result=self.run_scan('CRITICAL')
        self.assertEqual(result['security_status'],'BLOCK_CRITICAL')
        self.assertEqual(len(result['services']),6)
        with self.assertRaisesRegex(ValueError,'CRITICAL'):audit.enforce(self.bundle,self.output)
        self.assertTrue((self.output/'SHA256SUMS').is_file())

    def test_scan_failure_retains_partial_evidence_with_closed_gate(self):
        with patch.object(audit,'command',side_effect=self.scanner('HIGH','hub')):
            with self.assertRaisesRegex(ValueError,'^exact registry image scan incomplete$'):
                audit.scan_bundle(self.bundle,self.output,self.cache)
        d=audit.read_json(self.output/'summary.json')
        self.assertFalse(d['completed']);self.assertFalse(d['gcp_test_gate'])
        self.assertEqual(d['security_status'],'SCAN_INCOMPLETE')
        self.assertEqual(len(d['services']),2)
        with self.assertRaisesRegex(ValueError,'incomplete'):audit.enforce(self.bundle,self.output)

    def test_summary_cannot_hide_critical_even_with_rewritten_checksums(self):
        self.run_scan('CRITICAL')
        p=self.output/'summary.json';d=audit.read_json(p);d['counts']['CRITICAL']=0
        d['security_status']='NO_HIGH_CRITICAL_IN_SCAN_SCOPE';d['gcp_test_gate']=True
        p.write_text(json.dumps(d));self.refresh_checksums()
        with self.assertRaisesRegex(ValueError,'summary mismatch'):audit.enforce(self.bundle,self.output)

    def test_bundle_changes_are_rejected_after_scan(self):
        self.run_scan()
        changed=copy.deepcopy(self.data);changed['services']['hub']['digest']='sha256:'+'f'*64
        audit.release.write_bundle(self.bundle,changed)
        with self.assertRaisesRegex(ValueError,'release changed'):audit.enforce(self.bundle,self.output)

    def test_corrupted_report_checksum_and_reused_directory_are_rejected(self):
        self.run_scan()
        p=self.output/'hub.json';p.write_text(p.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'checksum'):audit.enforce(self.bundle,self.output)
        with patch.object(audit,'command') as command, self.assertRaisesRegex(ValueError,'fresh'):
            audit.scan_bundle(self.bundle,self.output,self.cache)
        command.assert_not_called()

    def test_missing_db_metadata_cannot_be_hidden_by_new_checksums(self):
        self.run_scan()
        (self.output/'scanner-db-metadata.json').unlink()
        self.refresh_checksums()
        with self.assertRaisesRegex(ValueError,'inventory'):audit.enforce(self.bundle,self.output)

    def test_invalid_bundle_is_rejected_before_network_or_output_creation(self):
        (self.bundle/'release.json').write_text('{}')
        with patch.object(audit,'command') as command, self.assertRaises(ValueError):
            audit.scan_bundle(self.bundle,self.output,self.cache)
        command.assert_not_called();self.assertFalse(self.output.exists())

    def test_workflow_uploads_security_evidence_then_enforces_before_bundle(self):
        workflow=(ROOT/'.github/workflows/image-release.yml').read_text()
        scan=workflow.index('name: scan six exact registry images')
        retain=workflow.index('name: retain exact image security evidence')
        gate=workflow.index('name: enforce critical vulnerability gate')
        publish=workflow.index('name: upload verified release bundle')
        self.assertLess(scan,retain);self.assertLess(retain,gate);self.assertLess(gate,publish)
        self.assertIn('release-tools/scripts/scan_release_images.py',workflow)
        self.assertNotIn('continue-on-error',workflow[scan:publish])
        self.assertIn('always()',workflow[retain:gate])


if __name__=='__main__':unittest.main()
