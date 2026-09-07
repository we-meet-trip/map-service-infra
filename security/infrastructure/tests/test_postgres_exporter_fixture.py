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
        self.origins = {}
        self.use_internal_origins = False
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
        origin = (f'http://172.20.0.{len(self.containers) + 1}:{port}' if self.use_internal_origins
                  else f'http://127.0.0.1:{19000 + len(self.containers)}')
        self.origins[(name, port)] = self.origin_override or origin
        return name, self.origins[(name, port)]

    def origin(self, name, port):
        return self.origins[(name, port)]

    def owns_origin(self, origin):
        return origin in self.origins.values()

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
        with self.assertRaisesRegex(M.FixtureError, 'numeric_fixture_origin_required'):
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

    def test_owned_internal_bridge_endpoints_pass_without_published_ports(self):
        self.s.use_internal_origins = True
        result = M.check(self.s, IMAGE)
        self.assertTrue(result['exact_current_sandbox_owned_origins'])
        request_origins = [call[1] for call in self.s.calls if call[0] in ('wait', 'request')]
        self.assertEqual(request_origins, ['http://172.20.0.3:9187'] * 2)

    def test_private_unowned_and_cross_container_origins_rejected(self):
        self.s.create_network('pg-exporter-db')
        first, first_origin = self.s.create(IMAGE, 'first', 9187)
        second, second_origin = self.s.create(IMAGE, 'second', 9187)
        with self.assertRaisesRegex(M.FixtureError, 'origin_container_mismatch'):
            M.origin_only(self.s, first_origin, second, 9187)
        self.s.origins[(first, 9187)] = 'http://172.20.0.99:9187'
        with patch.object(self.s, 'owns_origin', return_value=False):
            with self.assertRaisesRegex(M.FixtureError, 'unowned_origin'):
                M.origin_only(self.s, 'http://172.20.0.99:9187', first, 9187)
        with patch.object(self.s, 'owns_origin', return_value=1):
            with self.assertRaisesRegex(M.FixtureError, 'unowned_origin'):
                M.origin_only(self.s, second_origin, second, 9187)

    def test_metadata_reserved_and_noncanonical_numeric_urls_rejected(self):
        urls = ['http://169.254.169.254:80', 'http://0.0.0.0:80', 'http://239.1.2.3:80',
                'http://192.0.2.1:80', 'http://8.8.8.8:80', 'http://100.64.0.1:80',
                'http://127.0.0.2:80', 'http://2130706433:80', 'http://127.1:80',
                'http://localhost:80', 'http://127.0.0.1:080', ' http://127.0.0.1:80',
                'http://127.0.0.1:80\n', 'http://127.0.0.1:80/', 'http://127.0.0.1:80?x=y',
                'http://user@127.0.0.1:80', 'http://[::1]:80', 'http://%31%32%37.0.0.1:80']
        for origin in urls:
            with self.subTest(origin=origin), self.assertRaisesRegex(M.FixtureError, 'numeric_fixture_origin_required'):
                M.origin_only(self.s, origin, 'unused', 9187)

    def test_stale_origin_after_first_scrape_fails_before_second_http_request(self):
        original_wait = self.s.wait
        def change_origin(origin, path):
            result = original_wait(origin, path)
            key = next(key for key, value in self.s.origins.items() if value == origin)
            self.s.origins[key] = 'http://172.20.0.55:9187'
            return result
        with patch.object(self.s, 'wait', side_effect=change_origin):
            with self.assertRaisesRegex(M.FixtureError, 'origin_container_mismatch'):
                M.check(self.s, IMAGE)
        self.assertFalse(any(call[0] == 'request' for call in self.s.calls))


if __name__ == '__main__':
    unittest.main()
