"""Small stdlib tests only: no Docker, network, NCP, or real user data.

Local fixture roundtrips execute actual file IO. NCP/age adapter tests explicitly
mock external CLIs and are never evidence of provider access or encryption.
"""
import contextlib
import datetime as dt
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('ncp_backup', ROOT / 'scripts/ncp-bootstrap-backup.py')
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='map-ncp-backup-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'closed'
        self.source.mkdir(mode=0o700)
        self.payload = b'SYNTHETIC-NO-USER-DATA\x00\xff\n'
        self.contract = self.source / 'input.json'
        self.snapshot = self.root / 'snapshot'
        self.meta = self.synthetic_contract()

    def record(self, name, payload):
        path = self.source / name
        path.write_bytes(payload)
        path.chmod(0o600)
        return {'name': name, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}

    def synthetic_contract(self, role='prod'):
        meta = {'schema_version': 1, 'role': role, 'source': 'synthetic-stopped-fixture',
                'source_id': role + '-isolated-fixture', 'environment': role,
                'created_at': backup.now_utc().isoformat(), 'format': 'synthetic-stopped-v1',
                'consistency': 'stopped-synthetic-fixture', 'stopped': True, 'synthetic': True,
                'files': [self.record('payload.bin', self.payload)]}
        self.save(meta)
        return meta

    def save(self, meta=None):
        self.contract.write_text(json.dumps(meta if meta is not None else self.meta))
        self.contract.chmod(0o600)

    def create(self, role='prod'):
        return backup.snapshot(self.contract, role, self.snapshot)

    def assert_error(self, code, callable, *args, **kwargs):
        with self.assertRaisesRegex(backup.BackupError, code):
            callable(*args, **kwargs)

    def test_real_file_roundtrip_all_roles_with_rollback_to_fresh_directory(self):
        for role in backup.ROLES:
            with self.subTest(role=role):
                meta = self.synthetic_contract(role)
                snapshot = self.root / (role + '-snapshot')
                backup.snapshot(self.contract, role, snapshot)
                original_sha = backup.digest(snapshot / 'manifest.json')
                offhost = self.root / (role + '-simulated-offhost')
                uploaded = backup.fixture_transfer(snapshot, role, offhost)
                self.assertFalse(uploaded['ncp_remote_verified'])
                self.assertFalse(uploaded['encryption_executed'])
                recovered = self.root / (role + '-fresh-recovered')
                backup.fixture_transfer(offhost, role, recovered)
                rollback = self.root / (role + '-fresh-rollback')
                backup.restore(recovered, role, rollback)
                self.assertEqual((rollback / 'payload.bin').read_bytes(), self.payload)
                self.assertEqual(backup.digest(rollback / 'manifest.json'), original_sha)
                self.assertEqual(backup.verify(rollback, role)[0]['contract'], meta)
                for directory in (snapshot, offhost, recovered, rollback):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                    self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir()))

    def test_payload_corruption_rejected_before_restore_publication(self):
        self.create()
        (self.snapshot / 'payload.bin').write_bytes(b'X' * len(self.payload))
        target = self.root / 'must-not-exist'
        self.assert_error('backup_checksum_mismatch', backup.restore, self.snapshot, 'prod', target)
        self.assertFalse(target.exists())

    def test_input_checksum_mismatch_rejected(self):
        (self.source / 'payload.bin').write_bytes(b'corruption')
        self.assert_error('backup_size_mismatch', self.create)
        self.assertFalse(self.snapshot.exists())

    def test_same_length_input_checksum_mismatch_rejected(self):
        self.meta['files'][0]['sha256'] = '0' * 64
        self.save()
        self.assert_error('backup_checksum_mismatch', self.create)

    def test_cross_role_snapshot_and_restore_rejected(self):
        self.assert_error('backup_role_or_version_mismatch', self.create, 'admin')
        self.create()
        self.assert_error('snapshot_role_or_schema_mismatch', backup.restore,
                          self.snapshot, 'learning', self.root / 'learning')

    def test_environment_mixing_rejected(self):
        self.meta['environment'] = 'test'
        self.save()
        self.assert_error('source_environment_mismatch', self.create)

    def test_live_directory_and_false_stopped_marker_rejected(self):
        for field, value, error in [('source', 'live-volume-copy', 'closed_logical_backup_required'),
                                    ('stopped', False, 'stopped_synthetic_fixture_required'),
                                    ('synthetic', False, 'stopped_synthetic_fixture_required'),
                                    ('consistency', 'running', 'stopped_synthetic_fixture_required')]:
            with self.subTest(field=field):
                original = self.meta[field]
                self.meta[field] = value
                self.save()
                self.assert_error(error, self.create)
                self.meta[field] = original

    def test_symlink_payload_rejected(self):
        path = self.source / 'payload.bin'
        path.unlink()
        path.symlink_to(self.contract)
        self.assert_error('symlink_rejected', self.create)

    def test_symlink_source_parent_rejected(self):
        link = self.root / 'linked-source'
        link.symlink_to(self.source, target_is_directory=True)
        self.assert_error('symlink_rejected', backup.snapshot, link / 'input.json', 'prod', self.snapshot)

    def test_directory_payload_rejected(self):
        path = self.source / 'payload.bin'
        path.unlink()
        path.mkdir()
        self.assert_error('regular_single_link_file_required', self.create)

    def test_hardlink_payload_rejected(self):
        os.link(self.source / 'payload.bin', self.root / 'alias')
        self.assert_error('regular_single_link_file_required', self.create)

    def test_traversal_and_absolute_entry_rejected(self):
        for name in ('../input.json', '/etc/passwd', 'folder/payload.bin', '..', '.hidden'):
            with self.subTest(name=name):
                self.meta['files'][0]['name'] = name
                self.save()
                self.assert_error('unsafe_artifact_name', self.create)

    def test_traversal_target_rejected(self):
        self.create()
        self.assert_error('path_traversal_rejected', backup.restore, self.snapshot, 'prod',
                          self.root / 'closed' / '..' / 'target')

    def test_snapshot_existing_target_rejected_without_modification(self):
        self.snapshot.mkdir()
        sentinel = self.snapshot / 'existing-data'
        sentinel.write_bytes(b'PRESERVE')
        self.assert_error('target_already_exists', self.create)
        self.assertEqual(sentinel.read_bytes(), b'PRESERVE')

    def test_restore_existing_empty_directory_also_rejected(self):
        self.create()
        target = self.root / 'empty'
        target.mkdir()
        self.assert_error('target_already_exists', backup.restore, self.snapshot, 'prod', target)
        self.assertEqual(list(target.iterdir()), [])

    def test_restore_symlink_target_rejected(self):
        self.create()
        target = self.root / 'link'
        target.symlink_to(self.source, target_is_directory=True)
        self.assert_error('symlink_rejected', backup.restore, self.snapshot, 'prod', target)

    def test_rpo_uses_original_source_time_not_packaging(self):
        old = (backup.now_utc() - dt.timedelta(hours=2)).isoformat()
        self.meta['created_at'] = old
        self.save()
        self.assert_error('backup_rpo_age_exceeded', self.create)
        backup.snapshot(self.contract, 'prod', self.snapshot, max_age=3 * 3600)
        self.assert_error('backup_rpo_age_exceeded', backup.verify, self.snapshot, 'prod')

    def test_future_and_timezone_ambiguous_source_rejected(self):
        for value, error in [((backup.now_utc() + dt.timedelta(minutes=3)).isoformat(), 'future_backup_timestamp'),
                             ('2026-09-07T03:00:00', 'explicit_utc_timestamp_required'),
                             ('2026-09-07T03:00:00+09:00', 'explicit_utc_timestamp_required')]:
            with self.subTest(value=value):
                self.meta['created_at'] = value
                self.save()
                self.assert_error(error, self.create)

    def test_missing_manifest_is_uncommitted_snapshot(self):
        self.snapshot.mkdir(mode=0o700)
        self.record('unused.bin', b'x')
        self.assert_error('input_missing', backup.verify, self.snapshot, 'prod')

    def test_unknown_snapshot_entry_rejected(self):
        self.create()
        backup.write_bytes(self.snapshot / 'unexpected', b'x')
        self.assert_error('unexpected_snapshot_entry', backup.verify, self.snapshot, 'prod')

    def test_broad_snapshot_permission_rejected(self):
        self.create()
        (self.snapshot / 'payload.bin').chmod(0o644)
        self.assert_error('private_owned_0600_file_required', backup.verify, self.snapshot, 'prod')

    def test_duplicate_json_keys_and_duplicate_files_rejected(self):
        self.contract.write_text('{"schema_version":1,"schema_version":2}')
        self.assert_error('duplicate_json_key', self.create)
        self.meta['files'] *= 2
        self.save()
        self.assert_error('duplicate_artifact_name', self.create)

    def test_source_mutation_during_copy_aborts_without_committing(self):
        original = backup.copy_checked
        def mutate(source, target, record):
            source.write_bytes(b'Y' * record['bytes'])
            original(source, target, record)
        with mock.patch.object(backup, 'copy_checked', side_effect=mutate):
            self.assert_error('copy_checksum_mismatch', self.create)
        self.assertFalse(self.snapshot.exists())

    def test_low_disk_space_rejected_before_writing(self):
        with mock.patch.object(backup.shutil, 'disk_usage', return_value=mock.Mock(free=0)):
            self.assert_error('insufficient_staging_disk_space', self.create)
        self.assertFalse(self.snapshot.exists())

    def test_cli_reports_no_payload_or_contract_values(self):
        self.meta['source_id'] = 'SENSITIVE-MARKER-NEVER-LOG'
        self.save()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(backup.main(['snapshot', '--role', 'prod', '--contract', str(self.contract),
                                         '--output', str(self.snapshot)]), 0)
            self.assertEqual(backup.main(['restore', '--role', 'admin', '--snapshot', str(self.snapshot),
                                         '--target', str(self.root / 'recovered')]), 1)
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn('SENSITIVE-MARKER', combined)
        self.assertNotIn('SYNTHETIC-NO-USER-DATA', combined)
        self.assertNotIn(str(self.contract), combined)

    def pg_contract(self, role='admin'):
        created = backup.now_utc().replace(microsecond=0)
        records = [self.record('map-control.roles.sql.gz', gzip.compress(b'-- synthetic roles\n')),
                   self.record('map-control.sql.gz', gzip.compress(b'-- synthetic database\n'))]
        helper = {'version': 1, 'environment': 'prod', 'database': 'map_control',
                  'created_at': created.strftime('%Y%m%dT%H%M%SZ'), 'roles_have_passwords': False,
                  'files': [{'name': item['name'], 'sha256': item['sha256']} for item in records]}
        helper_path = self.source / 'map-control.manifest.json'
        helper_path.write_text(json.dumps(helper))
        helper_path.chmod(0o600)
        meta = {'schema_version': 1, 'role': role, 'source': 'pg_backup.py',
                'source_id': 'isolated-control-pg', 'environment': role, 'database': 'map_control',
                'created_at': created.isoformat(), 'format': 'pg-logical-gzip-v1',
                'consistency': 'closed-logical-backup', 'files': records,
                'helper_manifest': helper_path.name, 'helper_manifest_sha256': backup.digest(helper_path)}
        self.save(meta)
        return meta, helper, helper_path

    def test_postgres_closed_helper_contract_and_fresh_file_recovery(self):
        meta, helper, helper_path = self.pg_contract()
        self.create('admin')
        target = self.root / 'recovered'
        backup.restore(self.snapshot, 'admin', target)
        self.assertEqual(json.loads((target / helper_path.name).read_text()), helper)
        self.assertEqual(backup.verify(target, 'admin')[0]['contract'], meta)
        self.assert_error('local_offhost_simulation_synthetic_only', backup.fixture_transfer,
                          self.snapshot, 'admin', self.root / 'not-real-offhost')

    def test_postgres_source_database_and_creation_must_match_helper(self):
        for field, value, error in [('database', 'other_db', 'postgres_source_mismatch'),
                                    ('created_at', backup.now_utc().isoformat(), 'source_timestamp_mismatch')]:
            with self.subTest(field=field):
                meta, _, _ = self.pg_contract()
                if field == 'created_at':
                    value = (backup.now_utc() - dt.timedelta(minutes=10)).isoformat()
                meta[field] = value
                self.save(meta)
                self.assert_error(error, self.create, 'admin')

    def test_postgres_helper_checksum_and_password_roles_rejected(self):
        meta, helper, path = self.pg_contract()
        path.write_text(json.dumps({**helper, 'roles_have_passwords': True}))
        self.assert_error('helper_manifest_checksum_mismatch', self.create, 'admin')
        meta['helper_manifest_sha256'] = backup.digest(path)
        self.save(meta)
        self.assert_error('postgres_source_mismatch', self.create, 'admin')

    def test_learning_cannot_import_postgres_helper(self):
        self.pg_contract('learning')
        self.assert_error('postgres_role_format_mismatch', self.create, 'learning')

    def redis_contract(self):
        created = backup.now_utc().replace(microsecond=0).isoformat()
        records = [self.record('map-redis-prod-fixture.rdb', b'REDIS0011-SYNTHETIC')]
        helper = {'schema_version': 1, 'kind': 'redis-rdb', 'environment': 'prod',
                  'snapshot_at': created, 'created_at': created, 'image_id': 'sha256:' + '1' * 64,
                  'primary_restore': {'rdb_check': 'PASS'}, 'files': records}
        path = self.source / 'map-redis.manifest.json'
        path.write_text(json.dumps(helper))
        path.chmod(0o600)
        meta = {'schema_version': 1, 'role': 'prod', 'source': 'redis_backup.py',
                'source_id': 'isolated-serving-redis', 'environment': 'prod', 'created_at': created,
                'format': 'redis-rdb-v1', 'consistency': 'closed-logical-backup', 'files': records,
                'helper_manifest': path.name, 'helper_manifest_sha256': backup.digest(path)}
        self.save(meta)
        return meta, helper, path

    def test_redis_requires_existing_helper_restore_pass_and_snapshot_timestamp(self):
        meta, helper, path = self.redis_contract()
        self.create()
        self.assertEqual(backup.verify(self.snapshot, 'prod')[0]['contract']['created_at'], helper['snapshot_at'])
        helper['primary_restore']['rdb_check'] = 'FAIL'
        path.write_text(json.dumps(helper))
        meta['helper_manifest_sha256'] = backup.digest(path)
        self.save(meta)
        self.assert_error('redis_verified_source_required', backup.snapshot, self.contract, 'prod', self.root / 'bad')

    def test_archive_roundtrip_regular_entries_only(self):
        self.create()
        archive = self.root / 'payload.zip'
        backup.pack(self.snapshot, archive, 'prod', 3600)
        recovered = self.root / 'unpacked'
        backup.unpack(archive, recovered)
        backup.verify(recovered, 'prod')
        self.assertEqual((recovered / 'payload.bin').read_bytes(), self.payload)

    def test_archive_symlink_traversal_and_compression_rejected(self):
        for index, (name, mode, compression) in enumerate([
                ('../escape', stat.S_IFREG | 0o600, zipfile.ZIP_STORED),
                ('payload.bin', stat.S_IFLNK | 0o777, zipfile.ZIP_STORED),
                ('payload.bin', stat.S_IFREG | 0o600, zipfile.ZIP_DEFLATED)]):
            with self.subTest(index=index):
                archive = self.root / ('hostile-' + str(index) + '.zip')
                with zipfile.ZipFile(archive, 'x') as out:
                    for member in (name, 'manifest.json'):
                        item = zipfile.ZipInfo(member)
                        item.create_system = 3
                        item.external_attr = mode << 16
                        item.compress_type = compression
                        out.writestr(item, b'x')
                self.assert_error('unsafe_archive', backup.unpack, archive, self.root / ('bad-' + str(index)))
                self.assertFalse((self.root / 'escape').exists())

    def test_explicit_credentials_isolated_from_inherited_provider_environment(self):
        credential = self.root / 'credential.ini'
        credential.write_text('[default]\naws_access_key_id=TESTKEY\naws_secret_access_key=TESTSECRET\n')
        credential.chmod(0o600)
        with mock.patch.dict(os.environ, {'AWS_ACCESS_KEY_ID': 'WRONG', 'AWS_PROFILE': 'wrong',
                                         'AWS_ENDPOINT_URL': 'https://untrusted.invalid'}):
            with backup.aws_credentials(credential) as env:
                self.assertNotIn('AWS_ACCESS_KEY_ID', env)
                self.assertNotIn('AWS_PROFILE', env)
                self.assertNotIn('AWS_ENDPOINT_URL', env)
                self.assertEqual(env['AWS_EC2_METADATA_DISABLED'], 'true')
                self.assertNotEqual(env['AWS_SHARED_CREDENTIALS_FILE'], str(credential))
                private = Path(env['AWS_SHARED_CREDENTIALS_FILE'])
                self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
                self.assertEqual(private.read_bytes(), credential.read_bytes())
        credential.chmod(0o644)
        with self.assertRaisesRegex(backup.BackupError, 'private_owned_0600_file_required'):
            with backup.aws_credentials(credential):
                self.fail('unsafe credentials accepted')

    def test_aws_cli_fixed_https_private_acl_and_no_secret_argv(self):
        with mock.patch.object(backup, 'command') as command:
            backup.aws_copy(self.source / 'payload.bin', 's3://bucket/prod/x/backup.age',
                            {'AWS_SHARED_CREDENTIALS_FILE': '/private/input.ini'}, upload=True)
        argv = command.call_args.args[0]
        self.assertEqual(argv[argv.index('--endpoint-url') + 1], backup.ENDPOINT)
        self.assertEqual(argv[-2:], ['--acl', 'private'])
        self.assertNotIn('--no-verify-ssl', argv)
        self.assertFalse(any('SECRET' in item for item in argv))

    def test_remote_role_scope_and_traversal_rejected(self):
        for remote in ('s3://bucket/admin/backup', 's3://bucket/prod/../admin',
                       's3://bucket/prod//bad', 'https://evil.invalid', 's3://bucket/prod/./x'):
            with self.subTest(remote=remote):
                with self.assertRaises(backup.BackupError):
                    backup.remote_prefix(remote, 'prod')

    def test_cloud_download_needs_trusted_manifest_hash_before_network(self):
        with mock.patch.object(backup, 'aws_copy') as cp:
            self.assert_error('trusted_transport_sha256_required', backup.ncp_download,
                              's3://bucket/prod/backup', 'prod', self.root / 'target',
                              self.root / 'credentials', self.root / 'identity', None)
            cp.assert_not_called()

    @unittest.skipUnless(shutil.which('age') and shutil.which('age-keygen'),
                         'age CLIs absent; cryptography not executed')
    def test_real_age_encryption_roundtrip_wrong_identity_and_tamper_rejected(self):
        """Executes real installed age, using ephemeral synthetic-only keys."""
        self.create()
        identity = self.root / 'synthetic-identity.txt'
        backup.command(['age-keygen', '--output', str(identity)])
        identity.chmod(0o600)
        public = subprocess.run(['age-keygen', '-y', str(identity)], capture_output=True,
                                text=True, check=True).stdout.strip()
        archive, ciphertext = self.root / 'backup.zip', self.root / 'backup.age'
        backup.pack(self.snapshot, archive, 'prod', 3600)
        backup.age_encrypt(archive, ciphertext, public)
        self.assertNotIn(self.payload, ciphertext.read_bytes())
        plaintext = self.root / 'decrypted.zip'
        backup.age_decrypt(ciphertext, plaintext, identity)
        self.assertEqual(backup.digest(archive), backup.digest(plaintext))
        backup.unpack(plaintext, self.root / 'decrypted')
        backup.verify(self.root / 'decrypted', 'prod')
        wrong = self.root / 'wrong-identity.txt'
        backup.command(['age-keygen', '--output', str(wrong)])
        wrong.chmod(0o600)
        self.assert_error('external_command_failed', backup.age_decrypt, ciphertext,
                          self.root / 'wrong-output.zip', wrong)
        damaged = bytearray(ciphertext.read_bytes())
        damaged[-1] ^= 1
        ciphertext.write_bytes(damaged)
        self.assert_error('external_command_failed', backup.age_decrypt, ciphertext,
                          self.root / 'tampered-output.zip', identity)


    def test_mocked_encrypted_transport_roundtrip_and_corruption_gate(self):
        """Mock adapter only: no NCP or cryptography is executed."""
        self.create()
        remote_objects = {}
        transfers = []
        def cp(source, target, env, *, upload=False):
            transfers.append((str(source), str(target), upload))
            if upload:
                remote_objects[str(target)] = Path(source).read_bytes()
            else:
                Path(target).write_bytes(remote_objects[str(source)])
                Path(target).chmod(0o600)
        @contextlib.contextmanager
        def credentials(_, private_dir=None):
            self.assertIsNotNone(private_dir)
            yield {}
        def identity_copy(source, target, *_):
            # Explicitly NOT encryption: this mock only verifies orchestration.
            shutil.copyfile(source, target)
            Path(target).chmod(0o600)
        with mock.patch.object(backup, 'aws_copy', side_effect=cp), \
                mock.patch.object(backup, 'aws_credentials', credentials), \
                mock.patch.object(backup, 'age_encrypt', side_effect=identity_copy), \
                mock.patch.object(backup, 'age_decrypt', side_effect=identity_copy):
            result = backup.ncp_upload(self.snapshot, 'prod', 's3://bucket/prod/snapshots', 'unused', 'unused')
            self.assertEqual([item[2] for item in transfers], [True, False, True, False])
            self.assertTrue(transfers[0][1].endswith('/backup.age'))
            self.assertTrue(transfers[2][1].endswith('/transport.json'))
            target = self.root / 'downloaded'
            backup.ncp_download(result['remote'], 'prod', target, 'unused', 'unused', result['transport_sha256'])
            self.assertEqual((target / 'payload.bin').read_bytes(), self.payload)
            remote_objects[result['remote'] + '/backup.age'] = b'tampered'
            self.assert_error('download_checksum_mismatch', backup.ncp_download, result['remote'], 'prod',
                              self.root / 'tampered', 'unused', 'unused', result['transport_sha256'])
            self.assertFalse((self.root / 'tampered').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
