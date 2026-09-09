"""Real interpolation checks; no image pull, Docker daemon, or credentials."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
VALUES = {
    'PROD_POSTGRES_EXPORTER_IMAGE': 'synthetic/postgres-exporter@sha256:' + 'a' * 64,
    'PROD_REDIS_EXPORTER_IMAGE': 'synthetic/redis-exporter@sha256:' + 'b' * 64,
    'PROD_NODE_EXPORTER_IMAGE': 'synthetic/node-exporter@sha256:' + 'c' * 64,
    'PROD_POSTGRES_EXPORTER_DSN': 'postgresql://map_metrics:synthetic@postgres:5432/map_prod?sslmode=disable',
    'PROD_REDIS_EXPORTER_USER': 'map_prod_metrics',
    'PROD_REDIS_EXPORTER_PASSWORD': 'synthetic-only',
}


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI required')
class ProductionExporterTests(unittest.TestCase):
    def render(self, values):
        # Do not inherit any real project credential variables.
        env = {key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ}
        return subprocess.run(['docker', 'compose', '--env-file', '/dev/null', '--profile', 'monitoring',
                               '-f', str(ROOT / 'docker-compose.prod-exporters.yml'),
                               'config', '--format', 'json'], env={**env, **values},
                              capture_output=True, text=True, timeout=20)

    def test_production_network_and_loopback_only_ports(self):
        run = self.render(VALUES)
        self.assertEqual(run.returncode, 0, 'synthetic Compose must render')
        model = json.loads(run.stdout)
        self.assertEqual(model['name'], 'map-prod')
        self.assertEqual(model['networks']['default']['name'], 'map-prod-net')
        self.assertTrue(model['networks']['default']['external'])
        self.assertFalse(model['networks']['default'].get('ipam'))
        self.assertEqual(set(model['services']), {'postgres-exporter', 'redis-exporter', 'node-exporter'})
        expected_ports = {'postgres-exporter': (19187, 9187), 'redis-exporter': (19121, 9121), 'node-exporter': (19100, 9100)}
        for name, value in model['services'].items():
            self.assertRegex(value['image'], r'@sha256:[a-f0-9]{64}$')
            self.assertTrue(value['read_only'])
            self.assertEqual(value['cap_drop'], ['ALL'])
            self.assertEqual(value['security_opt'], ['no-new-privileges:true'])
            self.assertEqual(value['mem_limit'], 64 * 1024**2)
            self.assertEqual(len(value['ports']), 1)
            self.assertEqual(value['ports'][0]['host_ip'], '127.0.0.1')
            self.assertEqual((int(value['ports'][0]['published']), value['ports'][0]['target']), expected_ports[name])
        mount = model['services']['node-exporter']['volumes'][0]
        self.assertEqual((mount['source'], mount['target'], mount['read_only']), ('/', '/host', True))

    def test_each_production_input_is_required_without_test_fallback(self):
        for key in VALUES:
            with self.subTest(key=key):
                values = {**VALUES, key: '', 'TARGET_REDIS_EXPORTER_PASSWORD': 'test-must-not-fill-prod'}
                self.assertNotEqual(self.render(values).returncode, 0)


if __name__ == '__main__':
    unittest.main()
