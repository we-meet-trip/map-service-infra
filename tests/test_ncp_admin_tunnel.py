import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('admin_tunnel', ROOT / 'scripts/prepare-ncp-admin-tunnel.py')
tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunnel)


class AdminTunnelTests(unittest.TestCase):
    def test_tokens_and_existing_targets_remain_isolated(self):
        tokens = {name: str(index) * 40 for index, name in enumerate(tunnel.TOKENS, 1)}
        current = {'test': {'ADMIN_DATABASE_URL': 'postgresql://preserved',
                            'ADMIN_REDIS_URL': 'redis://preserved', 'USER_BASE_URL': 'http://user:8080'}}
        ports = {'user': 18080, 'agent': 18000, 'hub': 18001}
        result = tunnel.merge_targets(current, tokens, '172.30.10.1', ports)
        self.assertEqual(result['test'], current['test'])
        self.assertNotIn('prod', current)
        self.assertEqual(result['prod']['ADMIN_DATABASE_URL'], '')
        self.assertEqual(result['prod']['ADMIN_REDIS_URL'], '')
        self.assertEqual(result['prod']['HUB_BASE_URL'], 'http://172.30.10.1:18001')
        raw = 'APP_ENV=prod\n' + '\n'.join(name + '=' + value for name, value in tokens.items())
        self.assertEqual(tunnel.read_tokens(raw), tokens)
        for invalid in (raw.replace('APP_ENV=prod', 'APP_ENV=test'),
                        raw.replace(tokens['HUB_ADMIN_INTERNAL_TOKEN'], tokens['INTERNAL_SERVICE_TOKEN']),
                        raw + '\nINTERNAL_SERVICE_TOKEN=duplicate'):
            with self.assertRaises(ValueError):
                tunnel.read_tokens(invalid)
        with self.assertRaises(ValueError):
            tunnel.merge_targets({'prod': {'USER_BASE_URL': 'http://existing'}}, tokens, '172.30.10.1', ports)

    def test_addresses_ports_and_remote_root_are_rejected(self):
        valid = dict(ncp_host='203.0.113.10', source_ip='198.51.100.10',
                     cidr='172.30.10.0/29', gateway='172.30.10.1',
                     local_ports={'user': 18080, 'agent': 18000, 'hub': 18001}, ssh_user='map-tunnel', ssh_port=22)
        tunnel.addressing(**valid)
        for override in ({'gateway': '0.0.0.0'}, {'cidr': '8.8.8.0/24'}, {'ssh_user': 'root'},
                         {'ncp_host': '127.0.0.1'}, {'local_ports': {'user': 22, 'agent': 8000, 'hub': 8000}}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                tunnel.addressing(**{**valid, **override})

    def test_receiver_keeps_host_overlay_and_rejects_unsupported_checkout(self):
        spec = importlib.util.spec_from_file_location('ncp_tunnel_deploy', ROOT / 'scripts/deploy-gcp.py')
        deploy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deploy)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'scripts').mkdir()
            script = root / 'scripts/cloud-up.sh'
            script.write_text(deploy.ADMIN_NCP_MARKER + '\n')
            overlay = root / 'host-compose.yml'; overlay.write_text('services: {}\n')
            with patch.object(deploy, 'REPO', root), patch.object(deploy, 'ADMIN_NCP_OVERLAY', overlay), \
                    patch.object(deploy, 'ADMIN_DETACHED', False), \
                    patch.object(deploy, 'validate_host_metadata') as owner_check:
                command = deploy.compose_command(admin=True)
                self.assertIn(str(overlay), command)
                owner_check.assert_called_once()
                self.assertNotIn(str(overlay), deploy.compose_command(admin=False))
                script.write_text('# old release without preservation\n')
                with self.assertRaises(deploy.DeployError):
                    deploy.compose_command(admin=True)

    @unittest.skipUnless(shutil.which('ssh-keygen') and shutil.which('docker'), 'OpenSSH and Compose required')
    def test_private_preparation_and_real_compose_merge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / 'dedicated-key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(identity)], check=True)
            known = root / 'known_hosts'
            known.write_text('203.0.113.10 ' + identity.with_suffix('.pub').read_text())
            runtime = root / 'production.env'
            tokens = {name: str(index) * 40 for index, name in enumerate(tunnel.TOKENS, 1)}
            runtime.write_text('APP_ENV=prod\n' + '\n'.join(name + '=' + value for name, value in tokens.items()) + '\n')
            runtime.chmod(0o600)
            existing = {'test': {'USER_BASE_URL': 'http://preserve-test', 'ADMIN_DATABASE_URL': 'postgresql://preserve-test'}}
            target_file = root / 'targets.json'; target_file.write_text(json.dumps(existing)); target_file.chmod(0o600)
            args = argparse.Namespace(ncp_host='203.0.113.10', gcp_source_ip='198.51.100.10',
                bridge_cidr='172.30.10.0/29', bridge_gateway='172.30.10.1', ncp_ssh_user='map-tunnel',
                ncp_ssh_port=22, local_ports={'user': 18080, 'agent': 18000, 'hub': 18001},
                production_env_file=runtime, current_admin_targets_file=target_file,
                identity_file=identity, known_hosts_file=known, output_dir=root / 'prepared')
            plan = tunnel.prepare(args)
            self.assertEqual(plan['status'], 'PREPARED_NOT_DEPLOYED')
            for path in args.output_dir.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = json.loads((args.output_dir / 'admin-targets.env').read_text().split('=', 1)[1])
            self.assertEqual(original['test'], existing['test'])
            self.assertEqual(len(original['prod']), 8)
            authorized = (args.output_dir / 'ncp-authorized_keys').read_text()
            self.assertIn('restrict,port-forwarding,from="198.51.100.10/32"', authorized)
            for port in (8080, 8000, 8001):
                self.assertIn(f'permitopen="127.0.0.1:{port}"', authorized)
            sshd = (args.output_dir / 'ncp-sshd.conf').read_text()
            self.assertIn('AllowTcpForwarding local', sshd)
            self.assertIn('PermitListen none', sshd)
            for forbidden in ('5432', '6379'):
                self.assertNotIn(forbidden, authorized + sshd)
            result = subprocess.run(['ssh', '-G', '-F', str(args.output_dir / 'ssh_config'), 'map-ncp-admin'],
                                    capture_output=True, text=True, check=True)
            forwards = [line for line in result.stdout.splitlines() if line.startswith('localforward ')]
            self.assertEqual(len(forwards), 3)
            self.assertTrue(all('[172.30.10.1]:' in line and '127.0.0.1' in line for line in forwards))
            env = {**os.environ, 'ADMIN_DATABASE_URL': 'postgresql://unchanged-control',
                   'ADMIN_REDIS_URL': 'redis://unchanged-control', 'MAP_STACK_ENV': 'test',
                   'NCP_ADMIN_TARGETS_ENV_FILE': str(args.output_dir / 'admin-targets.env')}
            result = subprocess.run(['docker', 'compose', '--env-file', '.env.example',
                '-f', 'docker-compose.admin.yml', '-f', 'docker-compose.admin.test.yml',
                '-f', 'docker-compose.admin.ncp.yml', '--profile', '*', 'config', '--format', 'json'],
                cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, 'Compose render failed; no credential output emitted')
            config = json.loads(result.stdout)
            services = config['services']
            actual = services['admin']['environment']
            self.assertEqual(json.loads(actual['ADMIN_TARGETS']), original)
            self.assertEqual(actual['ADMIN_DATABASE_URL'], env['ADMIN_DATABASE_URL'])
            self.assertEqual(actual['ADMIN_REDIS_URL'], env['ADMIN_REDIS_URL'])
            self.assertEqual(set(services['admin']['networks']), {'default', 'ncp-admin'})
            for name, service in services.items():
                if name != 'admin':
                    self.assertNotIn('ncp-admin', service['networks'])
                    self.assertNotIn('ADMIN_TARGETS', service.get('environment', {}))
            self.assertTrue(config['networks']['ncp-admin']['external'])
            self.assertEqual(config['networks']['default']['name'], 'map-test-net')
            with self.assertRaises(ValueError):
                tunnel.prepare(args)
