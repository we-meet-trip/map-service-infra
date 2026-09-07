"""Stdlib mocks validate rejection boundaries; no Docker/PG/network is executed."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('pg_exporter_fixture',
    Path(__file__).parents[1] / 'fixtures' / 'postgres_exporter.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
IMAGE = 'sha256:' + 'a' * 64
METRICS = b'pg_up 1\npg_exporter_last_scrape_error 0\npg_stat_database_numbackends{datid="5",datname="map_fixture"} 2\n'


class Sandbox:
    def __init__(self):
        self.token = '123456789abc'
        self.containers, self.volumes, self.networks = [], [], []
        self.calls = []
        self.metadata = {}
        self.metrics = METRICS
        self.network_override = None
        self.origin_override = None
        self.command_failure = None
        self.existing_volume = b''
        self.admin = {
            'server_version_num': M.PG_VERSION_NUM, 'password_encryption': 'scram-sha-256',
            'scram_stored': True, 'host_auth_methods': ['scram-sha-256', 'scram-sha-256'],
            'direct_memberships': ['pg_monitor'],
            'role': {'rolname': M.MONITOR, 'rolsuper': False, 'rolcreatedb': False,
                     'rolcreaterole': False, 'rolreplication': False, 'rolbypassrls': False,
                     'rolcanlogin': True, 'rolinherit': True}}
        self.connected = {'current_user': M.MONITOR, 'session_user': M.MONITOR,
            'database': M.DATABASE, 'tcp': True, 'pg_monitor_member': True,
            'public_schema_create_privilege': False, 'database_create_privilege': False}

    def create_network(self, label):
        name = self.network_override or 'map-infra-' + self.token + '-' + label
        self.networks.append(name)
        return name

    def create(self, image, label, port, data_path=None, seed=None, extra=(), command=()):
        self.calls.append(('create', image, label, extra, command))
        name = 'map-infra-' + self.token + '-' + label
        self.containers.append(name)
        mounts = []
        if data_path:
            volume = name + '-data'
            self.volumes.append(volume)
            mounts = [{'Type': 'volume', 'Name': volume, 'Destination': data_path}]
        self.metadata[name] = {'Name': '/' + name, 'Image': M.PG_CONFIG if image == M.PG_IMAGE else image,
            'Config': {'Labels': {'map.infra.fixture': self.token}}, 'Mounts': mounts,
            'NetworkSettings': {'Networks': {self.networks[0]: {}}}}
        return name, self.origin_override or 'http://127.0.0.1:19000'

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        sql = kwargs.get('input', b'').decode()
        if self.command_failure and self.command_failure in sql:
            raise ValueError('private synthetic password must not escape')
        if args[1:3] == ['network', 'inspect']:
            return json.dumps([{'Name': args[-1], 'Internal': True,
                               'Labels': {'map.infra.fixture': self.token}}]).encode()
        if args[1:3] == ['volume', 'ls']:
            return self.existing_volume
        if args[1:3] == ['volume', 'inspect']:
            return json.dumps([{'Name': args[-1], 'Labels': {'map.infra.fixture': self.token}}]).encode()
        if args[1] == 'inspect':
            return json.dumps([self.metadata[args[-1]]]).encode()
        if 'psql' in args:
            if 'row_to_json' in sql:
                return json.dumps(self.admin).encode()
            if 'inet_client_addr' in sql:
                return json.dumps(self.connected).encode()
            return b'DO\n'
        return b''

    def wait(self, origin, path):
        self.calls.append(('wait', origin, path))
        return self.metrics

    def request(self, origin, path):
        self.calls.append(('request', origin, path))
        return self.metrics


class ExporterFixtureTests(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.env = patch.dict(M.os.environ, {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.platform = patch.object(M.sys, 'platform', 'linux')
        self.platform.start(); self.addCleanup(self.platform.stop)

    def test_success_requires_real_metric_and_scram_role_assertions(self):
        result = M.check(self.s, IMAGE)
        self.assertTrue(result['actual_postgresql_scrape'])
        self.assertTrue(result['monitor_table_and_schema_ddl_denied_sqlstate_42501'])
        self.assertEqual(result['second_scrape']['pg_stat_database_numbackends'], 2)
        sql_calls = [(args, kwargs) for args, *tail in self.s.calls if isinstance(args, list)
                     for kwargs in tail if 'psql' in args]
        denial = [(args, k) for args, k in sql_calls if b"SQLSTATE '42501'" in k.get('input', b'')]
        self.assertEqual(len(denial), 2)
        self.assertTrue(all('-h' in args and M.MONITOR in args for args, _ in denial))
        creates = [call for call in self.s.calls if call[0] == 'create']
        self.assertEqual(len(creates), 2)
        self.assertIn(M.PG_IMAGE, creates[0])
        self.assertIn('DATA_SOURCE_NAME=postgresql://map_metrics:', '\n'.join(creates[1][3]))
        self.assertNotIn('password', json.dumps(result).lower())

    def test_pg_up_zero_missing_error_and_missing_database_metric_fail(self):
        for metrics, error in (
            (METRICS.replace(b'pg_up 1', b'pg_up 0'), 'pg_up_not_one'),
            (METRICS.replace(b'pg_exporter_last_scrape_error 0', b''), 'last_scrape_error_not_zero'),
            (METRICS.replace(b'pg_exporter_last_scrape_error 0', b'pg_exporter_last_scrape_error 1'), 'last_scrape_error_not_zero'),
            (METRICS.split(b'pg_stat_database_numbackends')[0], 'database_stats_missing'),
            (METRICS.replace(b'map_fixture', b'foreign_db'), 'database_stats_missing'),
            (METRICS.replace(b'pg_up 1', b'pg_up NaN'), 'metric_nonfinite'),
        ):
            with self.subTest(error=error):
                with self.assertRaisesRegex(M.FixtureError, error):
                    M.metrics_checks(metrics)

    def test_privilege_flags_and_extra_memberships_rejected(self):
        for flag in ('rolsuper', 'rolcreatedb', 'rolcreaterole', 'rolreplication', 'rolbypassrls'):
            admin = copy.deepcopy(self.s.admin); admin['role'][flag] = True
            with self.subTest(flag=flag), self.assertRaisesRegex(M.FixtureError, 'privileged_role_rejected'):
                M.role_checks(admin, self.s.connected)
        self.s.admin['direct_memberships'].append('pg_write_server_files')
        with self.assertRaisesRegex(M.FixtureError, 'role_membership_escape'):
            M.check(self.s, IMAGE)

    def test_socket_trust_and_ddl_privileges_do_not_pass_as_scram(self):
        for field, value, error in (
            ('tcp', False, 'monitor_tcp_identity'),
            ('current_user', M.OWNER, 'monitor_tcp_identity'),
            ('public_schema_create_privilege', True, 'monitor_ddl_privilege'),
            ('database_create_privilege', True, 'monitor_ddl_privilege'),
        ):
            connected = {**self.s.connected, field: value}
            with self.subTest(field=field), self.assertRaisesRegex(M.FixtureError, error):
                M.role_checks(self.s.admin, connected)
        self.s.admin['host_auth_methods'] = ['trust']
        with self.assertRaisesRegex(M.FixtureError, 'scram_host_auth_required'):
            M.check(self.s, IMAGE)

    def test_ddl_command_failure_is_not_reported_as_expected_denial(self):
        self.s.command_failure = 'DO $fixture$'
        with self.assertRaisesRegex(M.FixtureError, '^pg_exporter_command_failed:ddl-denial$'):
            M.check(self.s, IMAGE)

    def test_foreign_network_origin_and_existing_volume_rejected(self):
        self.s.network_override = 'production-network'
        with self.assertRaisesRegex(M.FixtureError, 'foreign_network'):
            M.check(self.s, IMAGE)
        self.s = Sandbox(); self.s.origin_override = 'http://map-production:9187'
        with self.assertRaisesRegex(M.FixtureError, 'loopback_origin_required'):
            M.check(self.s, IMAGE)
        self.s = Sandbox(); self.s.existing_volume = b'existing-volume\n'
        with self.assertRaisesRegex(M.FixtureError, 'volume_already_exists'):
            M.check(self.s, IMAGE)
        self.assertFalse(self.s.containers)

    def test_unowned_container_or_second_network_rejected(self):
        self.s.networks.append('map-infra-' + self.s.token + '-pg-exporter-db')
        name, _ = self.s.create(IMAGE, 'exporter', 9187)
        self.s.metadata[name]['NetworkSettings']['Networks'] = {'foreign': {}}
        with self.assertRaisesRegex(M.FixtureError, 'container_foreign_network'):
            M.owned_container(self.s, name, self.s.networks[0], IMAGE)

    def test_mutable_candidate_and_local_execution_rejected(self):
        with self.assertRaisesRegex(M.FixtureError, 'exact_candidate_required'):
            M.check(self.s, 'postgres-exporter:latest')
        with patch.dict(M.os.environ, {'GITHUB_ACTIONS': 'false'}):
            with self.assertRaisesRegex(M.FixtureError, 'remote_hosted_ci_only'):
                M.check(self.s, IMAGE)

    def test_old_sandbox_without_owned_network_contract_rejected(self):
        self.s.create_network = None
        with self.assertRaisesRegex(M.FixtureError, 'sandbox_network_contract_missing'):
            M.check(self.s, IMAGE)
        self.assertFalse(self.s.containers)


if __name__ == '__main__':
    unittest.main()
