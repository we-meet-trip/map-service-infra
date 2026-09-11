import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ncp_receiver', ROOT / 'scripts/ncp-production-receiver.py')
receiver = importlib.util.module_from_spec(spec); spec.loader.exec_module(receiver)
CID = 'c' * 64


def config():
    return {'schema_version': 1, 'environment': 'prod', 'project': 'map-prod',
            'topology': 'gcp-test-admin-ncp-prod', 'release_name': 'approved-fixture',
            'enrollment_sha256': 'e' * 64, 'security_approval_sha256': 'a' * 64,
            'release_manifest_sha256': 'b' * 64, 'bootstrap_approval_sha256': 'd' * 64,
            'postgres_image_id': 'sha256:' + 'f' * 64, 'user_image_id': 'sha256:' + '1' * 64}


def manifest():
    value = {'schema_version': 1, 'release_tag': 'fixture-release', 'created_at': '2026-09-09T00:00:00Z',
             'infra_sha': 'a' * 40, 'source_ref': 'feature-launch-readiness', 'github_run_id': '123',
             'workflow_repository': receiver.release_manifest.REPOSITORY,
             'provenance': {'workflow_path': receiver.release_manifest.WORKFLOW_PATH,
                            'workflow_sha': 'b' * 40, 'event_name': 'workflow_dispatch'}, 'services': {}}
    for name in receiver.release_manifest.SERVICES:
        repo = 'admin' if name == 'admin-web' else name
        value['services'][name] = {'source_repo': 'we-meet-trip/map-service-' + repo,
                                  'source_sha': 'c' * 40,
                                  'image': receiver.release_manifest.REGISTRY + '/map-service-' + name,
                                  'digest': 'sha256:' + 'd' * 64}
    return value


def contract():
    value = {'images': {name: {'image': 'fixture.invalid/' + name + '@sha256:' + '9' * 64,
                              'source_commit': 'c' * 40, 'platform': 'linux/amd64'}
                        for name in receiver.artifacts.PROD_REQUIRED}}
    for name in ('user', 'agent', 'hub', 'yolo'):
        item = manifest()['services'][name]
        value['images'][name]['image'] = item['image'] + '@' + item['digest']
    return value


def finalized(request):
    timestamp = receiver.now()
    return {'schema_version': 1, 'status': 'PASS', 'input_request_sha256': receiver.canonical(request),
            'identities': {key: request[key] for key in (
                'environment', 'project', 'database', 'enrollment_sha256', 'security_approval_sha256',
                'release_manifest_sha256', 'runtime_contract_sha256', 'new_host_proof_sha256',
                'user', 'postgres', 'marker_sha256')},
            'migrations_executed': 4, 'finalization_complete': True, 'bootstrap_login_disabled': True,
            'bootstrap_sessions_zero': True, 'runtime_and_migrator_roles_ready': True,
            'postgres_identity_preserved': True, 'started_at': timestamp, 'completed_at': timestamp}


class FixtureBackend:
    """Never invokes Docker or PostgreSQL; only records phase ordering."""
    def __init__(self):
        self.events = []; self.fail = None
        self.enrollment = {'machine_id': '2' * 32, 'instance_id': 'fixture-ncp',
                           'data_uuid': '11111111-2222-3333-4444-555555555555'}

    def event(self, name):
        self.events.append(name)
        if self.fail == name: raise receiver.ReceiverError('synthetic_failure')

    def verify(self, value):
        self.event('verify'); return self.enrollment, contract(), manifest()

    def empty(self): self.event('empty')
    def secrets(self):
        self.event('secrets')
        return {name: f'{number:064x}' for number, name in enumerate(receiver.SECRET_NAMES, 1)}
    def prepare_postgres(self, approved, value): self.event('create'); return CID
    def postgres(self, cid, image): self.event('postgres')
    def create_database(self, cid, marker): self.event('database')


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = self.root / 'deploy'
        self.backend = FixtureBackend(); self.cfg = config()

    def prepared(self):
        result = receiver.prepare(self.cfg, backend=self.backend, state=self.state)
        request = receiver.private.read_json(self.state / 'bootstrap-request.json', private=True)
        return result, request

    def test_production_only_closed_input_schema_and_pins(self):
        self.assertEqual(receiver.configuration(self.cfg), self.cfg)
        for key, value in (('schema_version', True), ('environment', 'test'), ('project', 'map-test'),
                           ('topology', 'four-server'), ('release_name', '../other'),
                           ('enrollment_sha256', 'unpinned'), ('postgres_image_id', 'postgres:17'),
                           ('bootstrap_approval_sha256', 'not-reviewed')):
            with self.subTest(key=key), self.assertRaises(receiver.ReceiverError):
                receiver.configuration({**self.cfg, key: value})
        with self.assertRaises(receiver.ReceiverError): receiver.configuration({**self.cfg, 'admin': True})

    def test_six_image_provenance_and_four_app_placement_are_separate(self):
        receiver.release_binding(contract(), manifest())
        missing = manifest(); del missing['services']['admin']
        with self.assertRaises(ValueError): receiver.release_binding(contract(), missing)
        extra = contract(); extra['images']['admin'] = extra['images']['user']
        with self.assertRaisesRegex(receiver.ReceiverError, 'allowlist'): receiver.release_binding(extra, manifest())
        for field in ('image', 'source_commit'):
            bad = contract(); bad['images']['user'][field] = 'different'
            with self.assertRaisesRegex(receiver.ReceiverError, 'subset'): receiver.release_binding(bad, manifest())

    def test_bootstrap_approval_must_reference_exact_user_source_and_successful_artifact(self):
        approval = {'schema_version': 1, 'status': 'approved', 'source_sha': 'c' * 40,
                    'github_run_id': '321', 'artifact_sha256': 'b' * 64,
                    'bootstrap_postgres_verified': True, 'reviewer': 'fixture-reviewer'}
        receiver.bootstrap_approval(approval, manifest())
        for key, value in (('source_sha', 'a' * 40), ('bootstrap_postgres_verified', False),
                           ('status', 'candidate'), ('artifact_sha256', 'unknown')):
            with self.assertRaises(receiver.ReceiverError):
                receiver.bootstrap_approval({**approval, key: value}, manifest())

    def test_prepare_binds_empty_proof_and_does_not_admit_serving_or_repeat(self):
        result, request = self.prepared()
        self.assertEqual(result['status'], 'BOOTSTRAP_REQUIRED')
        self.assertEqual(result['public_serving'], 'HOLD')
        self.assertEqual(self.backend.events, ['verify', 'verify', 'empty', 'secrets', 'create',
                                               'postgres', 'database', 'postgres'])
        self.assertEqual(request['database'], 'map_prod')
        self.assertEqual(request['postgres']['container_id'], CID)
        self.assertEqual(request['postgres']['network'], 'map-prod-user-migration')
        self.assertEqual(set(request['secret_paths']), set(receiver.SECRET_NAMES))
        for secret in self.backend.secrets().values():
            self.assertNotIn(secret, json.dumps(request))
        created = self.backend.events.count('create')
        with self.assertRaisesRegex(receiver.ReceiverError, 'prior_attempt'):
            receiver.prepare(self.cfg, backend=self.backend, state=self.state)
        self.assertEqual(self.backend.events.count('create'), created)

    def test_host_and_empty_failures_precede_docker_creation(self):
        for stage in ('verify', 'empty', 'secrets'):
            self.backend.fail = stage
            with self.assertRaises(receiver.ReceiverError):
                receiver.prepare(self.cfg, backend=self.backend, state=self.state)
            self.assertNotIn('create', self.backend.events)
            self.assertFalse((self.state / 'first-install-attempt.json').exists())

    def test_unknown_create_or_database_failure_retains_attempt_and_blocks_retry(self):
        self.backend.fail = 'database'
        with self.assertRaises(receiver.ReceiverError):
            receiver.prepare(self.cfg, backend=self.backend, state=self.state)
        self.assertTrue((self.state / 'new-host-proof.json').exists())
        self.assertFalse((self.state / 'bootstrap-request.json').exists())
        self.backend.fail = None
        with self.assertRaisesRegex(receiver.ReceiverError, 'prior_attempt'):
            receiver.prepare(self.cfg, backend=self.backend, state=self.state)
        self.assertEqual(self.backend.events.count('create'), 1)

    def test_bootstrap_receipt_requires_finalization_not_only_java_exit(self):
        _, request = self.prepared(); receipt = finalized(request)
        receiver.verify_bootstrap_receipt(receipt, request)
        for key in ('finalization_complete', 'bootstrap_login_disabled', 'bootstrap_sessions_zero',
                    'runtime_and_migrator_roles_ready', 'postgres_identity_preserved'):
            with self.subTest(key=key), self.assertRaises(receiver.ReceiverError):
                receiver.verify_bootstrap_receipt({**receipt, key: False}, request)
        for change in ({'migrations_executed': True}, {'migrations_executed': 28},
                       {'input_request_sha256': 'f' * 64}, {'raw_sql': 'unwanted'}):
            with self.assertRaises(receiver.ReceiverError): receiver.verify_bootstrap_receipt({**receipt, **change}, request)
        changed = copy.deepcopy(receipt); changed['identities']['postgres']['container_id'] = 'd' * 64
        with self.assertRaises(receiver.ReceiverError): receiver.verify_bootstrap_receipt(changed, request)

    def test_acceptance_rechecks_request_and_pg_and_still_holds_serving(self):
        _, request = self.prepared()
        receiver.write_once(self.state / 'bootstrap-receipt.json', finalized(request))
        result = receiver.accept_bootstrap(self.cfg, backend=self.backend, state=self.state)
        self.assertEqual(result['status'], 'BOOTSTRAP_ACCEPTED')
        self.assertEqual(result['public_serving'], 'HOLD')
        self.assertEqual(self.backend.events[-1], 'postgres')

    def test_normal_user_migration_uses_existing_jobs_only_after_acceptance(self):
        _, request = self.prepared()
        receiver.write_once(self.state / 'bootstrap-receipt.json', finalized(request))
        receiver.accept_bootstrap(self.cfg, backend=self.backend, state=self.state)
        secrets = receiver.private_dir(self.root / 'secrets')
        receiver.private.write_bytes(secrets / 'USER_DATABASE_PASSWORD', b'a' * 64)
        receiver.private.write_bytes(secrets / 'user-migration.env', (
            'USER_MIGRATION_URL=jdbc:postgresql://postgres:5432/map_prod?currentSchema=user_service\n'
            'USER_MIGRATION_USERNAME=map_user_migrator\nUSER_MIGRATION_PASSWORD=' + 'b' * 64 + '\n').encode())
        calls = []
        def run_job(service, serving, credentials, operation, scratch):
            calls.append((service['name'], operation, serving['x-map-production']))
            return {'status': 'PASS', 'image_id': self.cfg['user_image_id']}
        with patch.object(receiver, 'DATA', self.root), patch.object(receiver.migration, 'run_job', side_effect=run_job):
            result = receiver.migrate_user(self.cfg, backend=self.backend, state=self.state)
        self.assertEqual([call[1] for call in calls], ['migrate', 'validate'])
        self.assertTrue(all(call[2]['database'] == 'map_prod' for call in calls))
        self.assertEqual(result['public_serving'], 'HOLD')
        self.assertEqual(result['remaining_service_migrations'], ['hub', 'agent'])

    def test_secrets_reject_reuse_nonhex_and_symlinks(self):
        secrets = receiver.private_dir(self.root / 'secrets')
        for number, name in enumerate(receiver.SECRET_NAMES, 1):
            receiver.private.write_bytes(secrets / name, f'{number:064x}\n'.encode())
        with patch.object(receiver, 'DATA', self.root):
            self.assertEqual(len(receiver.Backend().secrets()), 5)
            (secrets / 'USER_BOOTSTRAP_PASSWORD').write_bytes((secrets / 'POSTGRES_PASSWORD').read_bytes())
            with self.assertRaisesRegex(receiver.ReceiverError, 'reuse'): receiver.Backend().secrets()
            (secrets / 'USER_BOOTSTRAP_PASSWORD').write_text('not-hex')
            with self.assertRaises(receiver.ReceiverError): receiver.Backend().secrets()
            (secrets / 'USER_BOOTSTRAP_PASSWORD').unlink()
            (secrets / 'USER_BOOTSTRAP_PASSWORD').symlink_to(secrets / 'POSTGRES_PASSWORD')
            with self.assertRaises(receiver.private.BackupError): receiver.Backend().secrets()

    def test_pg_create_has_no_public_port_pull_named_volume_or_application_start(self):
        self.state.mkdir(mode=0o700)
        backend = receiver.Backend(); calls = []
        def docker(args, **kwargs):
            calls.append(args)
            if args[0] in ('create', 'start'): return CID
            if args[:2] == ['network', 'create']: return 'private-network-id'
            self.fail('unexpected Docker operation')
        with patch.object(receiver, 'DATA', self.root), patch.object(receiver, 'STATE', self.state), \
             patch.object(receiver, 'PGDATA', self.root / 'data/postgres'), \
             patch.object(backend, 'docker', side_effect=docker):
            self.assertEqual(backend.prepare_postgres(contract(), self.cfg), CID)
        create = next(args for args in calls if args[0] == 'create')
        for item in ('--pull=never', '--restart=no', '--read-only', '--log-driver=none', '--cap-drop=ALL'):
            self.assertIn(item, create)
        self.assertIn('POSTGRES_DB=postgres', create)
        self.assertNotIn('-p', create); self.assertNotIn('--publish', create)
        self.assertFalse(any('type=volume' in item for item in create))
        self.assertEqual([args for args in calls if args[0] == 'start'], [['start', CID]])
        self.assertTrue((self.state / 'postgres-created.json').exists())

    def test_container_data_mount_network_and_project_drift_are_rejected(self):
        backend = receiver.Backend()
        actual = {'id': CID, 'image': self.cfg['postgres_image_id'], 'running': True,
                  'project': 'map-prod', 'service': 'postgres', 'ports': {},
                  'network_mode': receiver.NETWORK, 'restart': 'no', 'mounts': [{
                      'Type': 'bind', 'Source': str(receiver.PGDATA),
                      'Destination': '/var/lib/postgresql/data', 'RW': True}]}
        network = {'Internal': True, 'Driver': 'bridge', 'EnableIPv6': False,
                   'Options': {receiver.migration.GATEWAY_OPTION: 'isolated'},
                   'Labels': {receiver.migration.LABEL: 'user-migration-v1', 'kr.mapservice.project': 'map-prod'},
                   'Containers': {CID: {}}}
        with patch.object(backend, 'docker', side_effect=[json.dumps(actual), json.dumps([network])]):
            backend.postgres(CID, self.cfg['postgres_image_id'])
        for key, value in (('project', 'map-test'), ('ports', {'5432/tcp': []}), ('restart', 'always'),
                           ('mounts', []), ('network_mode', 'map-test-net')):
            with patch.object(backend, 'docker', return_value=json.dumps({**actual, key: value})), \
                 self.assertRaises(receiver.ReceiverError): backend.postgres(CID, self.cfg['postgres_image_id'])
        bad_network = copy.deepcopy(network); bad_network['Containers']['f' * 64] = {}
        with patch.object(backend, 'docker', side_effect=[json.dumps(actual), json.dumps([bad_network])]), \
             self.assertRaises(receiver.ReceiverError): backend.postgres(CID, self.cfg['postgres_image_id'])

    def test_database_creation_uses_template0_and_marker_only_on_private_stdin(self):
        backend = receiver.Backend(); calls = []; marker = '8' * 64
        def docker(args, **kwargs):
            calls.append((args, kwargs))
            if 'pg_isready' in args: return 'accepting connections'
            if 'map_prod' in args: return 'map_prod|170007|map-user-bootstrap:v1:' + marker
            return ''
        with patch.object(backend, 'docker', side_effect=docker): backend.create_database(CID, marker)
        sql = next(kwargs['payload'] for _, kwargs in calls if 'payload' in kwargs)
        self.assertIn('CREATE DATABASE map_prod TEMPLATE template0', sql)
        self.assertIn(marker, sql)
        self.assertFalse(any(marker in part for args, _ in calls for part in args))


if __name__ == '__main__':
    unittest.main()
