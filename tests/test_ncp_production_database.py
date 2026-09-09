import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ncp_database', ROOT / 'scripts/ncp-production-database.py')
database = importlib.util.module_from_spec(spec); spec.loader.exec_module(database)


def config():
    return {'schema_version': 1, 'environment': 'prod', 'project': 'map-prod',
            'topology': 'gcp-test-admin-ncp-prod', 'release_name': 'fixture-release',
            'enrollment_sha256': 'a' * 64, 'security_approval_sha256': 'b' * 64,
            'release_manifest_sha256': 'c' * 64, 'bootstrap_approval_sha256': 'd' * 64,
            'postgres_image_id': 'sha256:' + 'e' * 64, 'user_image_id': 'sha256:' + 'f' * 64}


class FixtureBackend:
    """Injected state machine only; no SQL, Docker, cloud or real source checkout."""
    def __init__(self):
        self.events = []; self.fail = None; self.hub_runs = 0
        self.request = {'postgres': {'container_id': '1' * 64, 'image_id': 'sha256:' + '2' * 64}}
        self.images = {service: 'ghcr.io/we-meet-trip/map-service-' + service + '@sha256:' + '3' * 64
                       for service in ('hub', 'agent')}
    def event(self, value):
        self.events.append(value)
        if self.fail == value: raise database.receiver.ReceiverError('synthetic_failure')
    def verify_phase(self, config, state):
        self.event('verify')
        return {'request': self.request, 'images': self.images, 'role_sql': {'hub': 'hub_sql', 'agent': 'agent_sql'},
                'sources': {name: {'image': image, 'image_id': 'sha256:' + '4' * 64, 'source_sha': '5' * 40}
                            for name, image in self.images.items()}}
    def secrets(self):
        return {name: f'{number:064x}' for number, name in enumerate(database.SECRET_NAMES, 1)}
    def fresh_guard(self, request, secrets): self.event('fresh'); return '6' * 64
    def extension(self, request): self.event('postgis')
    def roles(self, request, sql): self.event(sql)
    def logins(self, request, secrets): self.event('logins')
    def hub_scope(self, request, enabled): self.event('grant' if enabled else 'revoke')
    def migrate(self, inputs, secrets, service, state):
        if service == 'hub': self.hub_runs += 1
        self.event(service + str(self.hub_runs) if service == 'hub' else 'agent')
        return {'status': 'PASS', 'service': service}
    def runtime_probe(self, request, secrets, service): self.event('probe_' + service)
    def preserved(self, request, before): self.event('preserved')


class ProductionDatabaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = database.receiver.private_dir(self.root / 'deploy')
        database.receiver.private_dir(self.root / 'secrets')
        self.backend = FixtureBackend()
        self.patch_data = patch.object(database, 'DATA', self.root)
        self.patch_data.start(); self.addCleanup(self.patch_data.stop)

    def test_first_and_normal_hub_migrations_straddle_mandatory_revoke(self):
        result = database.execute(config(), backend=self.backend, state=self.state)
        self.assertEqual(self.backend.events, ['verify', 'verify', 'fresh', 'postgis', 'hub_sql', 'agent_sql', 'logins',
            'grant', 'hub1', 'revoke', 'hub_sql', 'hub2', 'hub_sql', 'agent', 'agent_sql', 'probe_hub', 'probe_agent', 'preserved'])
        self.assertEqual(result['public_serving'], 'HOLD')
        self.assertFalse(result['admin_exporter_roles_created'])
        for service in ('hub', 'agent'):
            path = self.root / 'secrets' / (service + '-migration.env')
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(database.receiver.ReceiverError, 'prior_database_attempt'):
            database.execute(config(), backend=self.backend, state=self.state)
        self.assertEqual(self.backend.events.count('postgis'), 1)

    def test_lost_grant_response_also_runs_the_finalizer(self):
        self.backend.fail = 'grant'
        with self.assertRaises(database.receiver.ReceiverError):
            database.execute(config(), backend=self.backend, state=self.state)
        self.assertEqual(self.backend.events[-1], 'revoke')
        self.assertNotIn('hub1', self.backend.events)
        self.assertTrue((self.state / 'database-provision-attempt.json').exists())

    def test_failed_initial_hub_migration_revokes_and_never_retries_or_starts_agent(self):
        self.backend.fail = 'hub1'
        with self.assertRaises(database.receiver.ReceiverError):
            database.execute(config(), backend=self.backend, state=self.state)
        self.assertEqual(self.backend.events[-1], 'revoke')
        self.assertNotIn('hub2', self.backend.events); self.assertNotIn('agent', self.backend.events)
        self.assertFalse((self.state / 'database-provision-complete.json').exists())

    def test_missing_scope_revoke_cannot_produce_success(self):
        self.backend.fail = 'revoke'
        with self.assertRaises(database.receiver.ReceiverError):
            database.execute(config(), backend=self.backend, state=self.state)
        self.assertFalse((self.state / 'database-provision-complete.json').exists())
        self.assertNotIn('hub2', self.backend.events)

    def test_existing_database_or_foreign_secret_blocks_before_ddl(self):
        self.backend.fail = 'fresh'
        with self.assertRaises(database.receiver.ReceiverError):
            database.execute(config(), backend=self.backend, state=self.state)
        self.assertNotIn('postgis', self.backend.events)
        self.assertFalse((self.state / 'database-provision-attempt.json').exists())
        self.backend.fail = None
        path = self.root / 'secrets/hub-migration.env'; path.write_text('preserve')
        with self.assertRaisesRegex(database.receiver.ReceiverError, 'existing_migration_credentials'):
            database.execute(config(), backend=self.backend, state=self.state)
        self.assertEqual(path.read_text(), 'preserve')
        self.assertNotIn('postgis', self.backend.events)

    def test_user_contract_drift_rejects_completion_and_preserves_evidence(self):
        self.backend.fail = 'preserved'
        with self.assertRaises(database.receiver.ReceiverError):
            database.execute(config(), backend=self.backend, state=self.state)
        self.assertTrue((self.state / 'receipts/agent-normal-migration.json').exists())
        self.assertFalse((self.state / 'database-provision-complete.json').exists())

    def test_both_service_dsns_match_fixed_prod_target_and_dedicated_roles(self):
        for service in ('hub', 'agent'):
            serving, secret = database.serving_config(self.backend.request, self.backend.images,
                                                      self.backend.secrets(), service)
            item = database.receiver.migration.service_contract(service)
            self.assertEqual(database.receiver.migration.contract(item, serving, secret)[0], 'map-prod')
            serving['name'] = 'map-test'
            with self.assertRaises(database.receiver.migration.JobError):
                database.receiver.migration.contract(item, serving, secret)

    def test_original_role_sql_is_passed_without_rewriting(self):
        backend = database.Backend(); sql = '\\set ON_ERROR_STOP on\nBEGIN;\nSELECT 1;\nCOMMIT;\n'
        with patch.object(backend, 'sql') as run:
            backend.roles(self.backend.request, sql)
        self.assertEqual(run.call_args.args[1], sql)

    def test_runtime_probe_uses_scram_host_and_all_four_dml_rights(self):
        backend = database.Backend(); captured = []
        with patch.object(backend, 'sql', side_effect=lambda request, sql, **kwargs: captured.append((sql, kwargs)) or 't'):
            backend.runtime_probe(self.backend.request, self.backend.secrets(), 'hub')
        sql = captured[0][0]
        for action in ('SELECT', 'INSERT', 'UPDATE', 'DELETE'):
            self.assertIn("has_table_privilege('hub_data.places','" + action + "')", sql)
        self.assertIn("inet_server_addr()<<inet'127.0.0.0/8'", sql)
        with patch.object(backend, 'docker', return_value='t') as docker:
            backend.sql(self.backend.request, 'SELECT true;', password='a' * 64, role='map_hub_runtime')
        self.assertIn('-h postgres', docker.call_args.args[0][-1])
        self.assertNotIn('a' * 64, str(docker.call_args.args[0]))

    def test_hub_deadline_and_create_grant_are_one_transaction(self):
        backend = database.Backend()
        with patch.object(backend, 'sql', side_effect=['', 't']) as sql:
            backend.hub_scope(self.backend.request, True)
        text = sql.call_args_list[0].args[1]
        self.assertTrue(text.startswith('BEGIN;')); self.assertTrue(text.endswith('COMMIT;'))
        self.assertLess(text.index('20 minutes'), text.index('GRANT CREATE'))


if __name__ == '__main__':
    unittest.main()
