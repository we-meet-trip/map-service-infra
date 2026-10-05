"""Render the co-hosted production administrator stack; synthetic inputs only, no daemon."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / 'docker-compose.admin.prod.yml'
TEMPLATES = ROOT / 'deploy/admin-prod'
# map-prod-net 에서 이미 쓰는 이름(운영 서비스 스택과 그 망에 붙는 DB 별칭).
# 관리자 스택이 같은 이름을 들고 붙으면 DNS 가 둘을 섞어 돌려준다.
TAKEN = {'postgres', 'redis', 'user', 'hub', 'agent', 'yolo', 'proxy', 'edge', 'osrm-foot', 'osrm-bicycle'}
TARGET = {'USER_BASE_URL': 'http://user:8080', 'AGENT_BASE_URL': 'http://agent:8000',
          'HUB_BASE_URL': 'http://hub:8000', 'INTERNAL_SERVICE_TOKEN': '', 'USER_ADMIN_INTERNAL_TOKEN': '',
          'HUB_ADMIN_INTERNAL_TOKEN': '', 'ADMIN_DATABASE_URL': '', 'ADMIN_REDIS_URL': ''}


def literal(path):
    values = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            key, separator, value = line.partition('=')
            assert separator and key not in values, line
            values[key] = value
    return values


class AdminProdTemplateTests(unittest.TestCase):
    def test_templates_hold_no_values_and_only_the_prod_api_target(self):
        runtime = literal(TEMPLATES / 'admin-runtime.env.example')
        self.assertEqual(set(runtime), {'ADMIN_ENVIRONMENT', 'ADMIN_CONTROL_DATABASE_URL', 'ADMIN_RUN_MIGRATIONS',
                                        'ADMIN_TARGETS', 'ADMIN_BOOTSTRAP_USER', 'ADMIN_BOOTSTRAP_PASSWORD'})
        self.assertEqual((runtime['ADMIN_ENVIRONMENT'], runtime['ADMIN_RUN_MIGRATIONS']), ('control', 'false'))
        for key in ('ADMIN_CONTROL_DATABASE_URL', 'ADMIN_BOOTSTRAP_USER', 'ADMIN_BOOTSTRAP_PASSWORD'):
            self.assertEqual(runtime[key], '')
        self.assertEqual(json.loads(runtime['ADMIN_TARGETS']), {'prod': TARGET})
        self.assertEqual(literal(TEMPLATES / 'admin-migration.env.example'),
                         {'ADMIN_CONTROL_MIGRATION_DATABASE_URL': ''})
        inputs = literal(TEMPLATES / 'compose.env.example')
        self.assertEqual(set(inputs), set(re.findall(r'\$\{([A-Z0-9_]+):\?', COMPOSE.read_text())))
        self.assertEqual(set(inputs.values()), {''})


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI required')
class AdminProdComposeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='map-admin-prod-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'data').mkdir()
        files = {'runtime.env': (TEMPLATES / 'admin-runtime.env.example').read_text(),
                 'migration.env': 'ADMIN_CONTROL_MIGRATION_DATABASE_URL=postgresql+psycopg://m:s@admin-control-db/c\n',
                 'password': 'synthetic-only\n'}
        for name, text in files.items():
            (root / name).write_text(text)
            (root / name).chmod(0o600)
        self.values = {
            'ADMIN_API_IMAGE': 'synthetic/admin@sha256:' + 'a' * 64,
            'ADMIN_WEB_IMAGE': 'synthetic/admin-web@sha256:' + 'b' * 64,
            'ADMIN_CONTROL_POSTGRES_IMAGE': 'synthetic/postgres@sha256:' + 'c' * 64,
            'ADMIN_RUNTIME_ENV_FILE': str(root / 'runtime.env'),
            'ADMIN_MIGRATION_ENV_FILE': str(root / 'migration.env'),
            'ADMIN_CONTROL_PROVISIONER_PASSWORD_FILE': str(root / 'password'),
            'ADMIN_CONTROL_DATA_DIR': str(root / 'data'),
        }

    def render(self, values, *profiles):
        # Do not inherit any real project credential variables.
        env = {key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ}
        args = ['docker', 'compose', '--env-file', '/dev/null', '-f', str(COMPOSE)]
        for profile in profiles:
            args += ['--profile', profile]
        return subprocess.run(args + ['config', '--format', 'json'], env={**env, **values},
                              capture_output=True, text=True, timeout=30)

    def model(self, *profiles):
        run = self.render(self.values, *profiles)
        self.assertEqual(run.returncode, 0, 'synthetic Compose must render')
        return json.loads(run.stdout)

    def test_independent_project_networks_and_names(self):
        self.assertEqual(set(self.model()['services']), {'admin', 'admin-web', 'admin-control-db'})
        model = self.model('migration')
        services = model['services']
        self.assertEqual(model['name'], 'map-admin-prod')
        self.assertEqual(set(services), {'admin', 'admin-web', 'admin-control-db', 'admin-migrate'})
        networks = model['networks']
        self.assertEqual((networks['map-prod-net']['name'], networks['map-prod-net'].get('external')),
                         ('map-prod-net', True))
        self.assertTrue(networks['admin-control'].get('internal'))
        # 내부망에 루프백 포트를 공개하면 붙지 않는다.
        self.assertFalse(networks['admin-web'].get('internal'))
        joined = {name: set(body.get('networks') or {}) for name, body in services.items()}
        self.assertEqual(joined, {'admin': {'admin-control', 'admin-web', 'map-prod-net'}, 'admin-web': {'admin-web'},
                                  'admin-control-db': {'admin-control'}, 'admin-migrate': {'admin-control'}})
        for name, body in services.items():
            aliases = {alias for network in (body.get('networks') or {}).values() for alias in (network or {}).get('aliases', [])}
            self.assertFalse(({name} | aliases) & TAKEN, name)
            self.assertFalse(body.get('build') or body.get('container_name'), name)
            self.assertRegex(body['image'], r'@sha256:[0-9a-f]{64}$')
            self.assertEqual(body['restart'], 'no' if name == 'admin-migrate' else 'unless-stopped')
            self.assertNotIn('monitoring', body.get('profiles') or [])
        published = {name: [(p['host_ip'], int(p['published']), p['target']) for p in body.get('ports') or []]
                     for name, body in services.items()}
        self.assertEqual(published, {'admin': [('127.0.0.1', 8002, 8000)], 'admin-web': [('127.0.0.1', 8003, 80)],
                                     'admin-control-db': [], 'admin-migrate': []})
        limits = {name: int(body['mem_limit']) for name, body in services.items()}
        self.assertEqual({k: limits[k] for k in ('admin', 'admin-web', 'admin-control-db')},
                         {'admin': 256 * 1024**2, 'admin-web': 64 * 1024**2, 'admin-control-db': 256 * 1024**2})
        command = services['admin-control-db']['command']
        self.assertIn('shared_buffers=32MB', command)
        self.assertIn('max_connections=20', command)

    def test_runtime_never_gets_migration_or_target_storage_credentials(self):
        hostile = Path(self.values['ADMIN_RUNTIME_ENV_FILE'])
        hostile.write_text(hostile.read_text() + 'ADMIN_RUN_MIGRATIONS=true\nADMIN_DATABASE_URL=postgresql://x\n'
                           'ADMIN_REDIS_URL=redis://x\nADMIN_CONTROL_MIGRATION_DATABASE_URL=postgresql://y\n')
        services = self.model('migration')['services']
        runtime = services['admin']['environment']
        self.assertEqual((runtime['ADMIN_RUN_MIGRATIONS'], runtime['ADMIN_DATABASE_URL'], runtime['ADMIN_REDIS_URL'],
                          runtime['ADMIN_CONTROL_MIGRATION_DATABASE_URL']), ('false', '', '', ''))
        self.assertEqual(json.loads(runtime['ADMIN_TARGETS']), {'prod': TARGET})
        self.assertEqual(set(services['admin-migrate']['environment']), {'ADMIN_CONTROL_MIGRATION_DATABASE_URL'})
        self.assertEqual(services['admin-migrate']['entrypoint'], ['alembic', 'upgrade', 'head'])
        for name in ('admin', 'admin-migrate'):
            self.assertTrue(services[name]['read_only'])
            self.assertEqual(services[name]['cap_drop'], ['ALL'])
            self.assertEqual(services[name]['security_opt'], ['no-new-privileges:true'])
            self.assertEqual(services[name]['tmpfs'], ['/tmp:rw,noexec,nosuid,size=32m'])

    def test_each_input_is_required_without_fallback(self):
        for key in self.values:
            with self.subTest(key=key):
                self.assertNotEqual(self.render({**self.values, key: ''}).returncode, 0)


if __name__ == '__main__':
    unittest.main()
