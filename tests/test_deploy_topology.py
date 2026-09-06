import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('deploy_topology', Path(__file__).resolve().parents[1] / 'scripts/deploy-gcp.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class TopologyTests(unittest.TestCase):
    def test_no_policy_preserves_cohost_and_incomplete_handoff_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(deploy, 'STATE', Path(temporary)):
            with deploy.topology_scope():
                self.assertFalse(deploy.ADMIN_DETACHED)
            handoff = deploy.STATE / 'admin-handoff.json'
            handoff.write_text(json.dumps({'status': 'PASS'}))
            (deploy.STATE / 'topology.json').write_text(json.dumps({
                'schema_version': 1, 'instance_id': deploy.INSTANCE_ID, 'mode': 'application',
                'verified_admin_handoff_sha256': hashlib.sha256(handoff.read_bytes()).hexdigest(),
            }))
            with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_uid=0, st_mode=0o100600)):
                with self.assertRaisesRegex(deploy.DeployError, 'identity not verified'):
                    with deploy.topology_scope():
                        pass
            self.assertFalse(deploy.ADMIN_DETACHED)

    def test_application_smoke_does_not_depend_on_central_admin_availability(self):
        calls = []
        def http(url, expected):
            calls.append(url)
            return b'{"status":"UP"}'
        with patch.object(deploy, 'ADMIN_DETACHED', True), patch.object(deploy, 'http_status', side_effect=http):
            deploy.smoke()
        self.assertFalse(any(':8202' in url or ':8203' in url for url in calls))
        self.assertTrue(any('/healthz/app' in url for url in calls))

    def test_detached_compose_never_merges_central_app_images_and_rollback_pins_are_kept(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(deploy, 'ADMIN_DETACHED', True):
            bundle = Path(temporary)
            (bundle / 'compose.admin-images.yml').write_text('services: {admin: {image: old}}')
            command = deploy.compose_command(admin=True, bundle=bundle)
            self.assertTrue(any('docker-compose.target-exporters.yml' in item for item in command))
            self.assertFalse(any('compose.admin' in item for item in command))
            (bundle / 'compose.target-images.yml').write_text('services: {}')
            self.assertIn(str(bundle / 'compose.target-images.yml'), deploy.compose_command(admin=True, bundle=bundle))

    def test_snapshot_does_not_capture_retired_services_for_later_rollback(self):
        calls = []
        def command(args, **_kwargs):
            calls.append(args)
            if 'inspect' in args:
                return 'sha256:' + 'a' * 64
            return 'b' * 12 if 'label=com.docker.compose.service=user' in args or 'label=com.docker.compose.service=node-exporter' in args else ''
        with tempfile.TemporaryDirectory() as temporary, patch.object(deploy, 'ADMIN_DETACHED', True), patch.object(deploy, 'command', side_effect=command):
            active = deploy.snapshot_images(Path(temporary), {})
            self.assertEqual(active['map-admin-test'], ['node-exporter'])
            self.assertTrue((Path(temporary) / 'compose.target-images.yml').exists())
        self.assertFalse(any('label=com.docker.compose.service=admin' in call for call in calls))

    def test_rollback_refuses_old_admin_even_when_prior_active_file_contains_it(self):
        with patch.object(deploy, 'ADMIN_DETACHED', True), patch.object(deploy, 'command', return_value=''), patch.object(deploy, 'git'), patch.object(deploy, 'replace_environment'):
            with self.assertRaisesRegex(deploy.DeployError, 'cannot restore retired'):
                deploy.rollback('old', b'', None, Path('/previous'),
                                {'map-test': [], 'map-admin-test': ['admin']}, Path('/current'), {})
