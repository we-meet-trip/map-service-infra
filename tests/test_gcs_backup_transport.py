"""GCS backup transport and runner: no Docker, network, cloud account or user data.

A generated stand-in for gcloud keeps objects in a temporary directory and
enforces create-only uploads. age runs for real when installed; otherwise a
copy stands in for it. PostgreSQL is replaced only at the docker/psql process
boundary of pg_backup.py restore.
"""
import contextlib
import copy
import datetime as dt
import fcntl
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import gcs_backup_transport as transport

spec = importlib.util.spec_from_file_location('gcs_production_backup', ROOT / 'scripts/gcs-production-backup.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
pg = runner.ncp.pg
HAS_AGE = bool(shutil.which('age') and shutil.which('age-keygen'))
# TransportTests stub the LUKS2 probe; its own test puts this real one back.
ENCRYPTED_VOLUME = transport.encrypted_volume
# Ops Agent keeps journal lines only for unit names matching this pattern.
LOG_UNIT = r'^map-prod-.*backup.*\.service$'

FAKE_GCLOUD = '''#!{python}
import json, os, pathlib, shutil, sys
root = pathlib.Path({root!r})
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps({{'args': args, 'composite': os.environ.get('CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED'),
                          'env': sorted(os.environ)}}) + '\\n')
if os.environ.get('CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED') != 'False':
    sys.exit(9)
create = '--if-generation-match=0' in args
source, target = [item for item in args[2:] if not item.startswith('--')]
def local(url):
    return root / 'objects' / url[len('gs://'):]
if target.startswith('gs://'):
    path = local(target)
    if path.exists():
        sys.stderr.write('HTTPError 412: precondition failed' if create else 'HTTPError 403: no overwrite')
        sys.exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, path)
else:
    path = local(source)
    if not path.exists():
        sys.stderr.write(source + ' not found: 404.')
        sys.exit(1)
    shutil.copyfile(path, target)
    # A matching download gets one byte flipped, as a faulty transfer would.
    flip = root / 'corrupt'
    if flip.exists() and source.endswith(flip.read_text()):
        data = bytearray(pathlib.Path(target).read_bytes())
        data[0] ^= 1
        pathlib.Path(target).write_bytes(bytes(data))
'''


class FakeGcs:
    def __init__(self, root):
        self.root = root
        (root / 'objects').mkdir(parents=True)
        self.binary = root / 'gcloud'
        self.binary.write_text(FAKE_GCLOUD.format(python=sys.executable, root=str(root)))
        self.binary.chmod(0o755)

    def calls(self):
        path = self.root / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def object(self, remote):
        return self.root / 'objects' / remote[len('gs://'):]

    def corrupt(self, suffix):
        (self.root / 'corrupt').write_text(suffix)


class FakePostgres:
    """Answers pg_backup.restore's docker/psql calls from the SQL it receives."""

    def __init__(self, scratch):
        self.scratch, self.sql = scratch, bytearray()

    def counts(self):
        path = self.scratch / 'restored.sql.gz'
        path.write_bytes(gzip.compress(bytes(self.sql)))
        return pg.dump_row_counts(path)

    def run(self, args, **kwargs):
        query, out = args[-1], ''
        if 'oid=10' in query:
            out = 'postgres\n'
        elif query.startswith('SELECT table_schema'):
            schemas = {}
            for table in self.counts():
                schemas[table.split('.')[0]] = schemas.get(table.split('.')[0], 0) + 1
            out = '\n'.join(f'{name}:{count}' for name, count in sorted(schemas.items()))
        elif query.startswith('SELECT count(*) FROM'):
            out = str(self.counts()['.'.join(re.findall(r'"([^"]+)"', query))])
        return SimpleNamespace(stdout=out, returncode=0)

    def popen(self, args, **kwargs):
        sql = self.sql

        class Sink(io.RawIOBase):
            def writable(self):
                return True

            def write(self, data):
                sql.extend(data)
                return len(data)

        return SimpleNamespace(stdin=Sink(), wait=lambda: 0, poll=lambda: 0)


def postgres_dump(command, path):
    if 'pg_dumpall' in command:
        text = 'CREATE ROLE postgres;\nALTER ROLE postgres WITH SUPERUSER;\nCREATE ROLE map_user_runtime;\n'
    else:
        text = ('COPY hub_data.places (id) FROM stdin;\n1\n2\n\\.\n'
                'COPY user_service.users (id) FROM stdin;\n1\n\\.\n')
    with gzip.open(path, 'wt', encoding='utf-8') as stream:
        stream.write(text)
    path.chmod(0o600)


def config():
    return {'schema_version': 1, 'enrollment_sha256': 'e' * 64, 'bucket': 'map-prod-backups',
            'age_recipient': 'age1' + 'q' * 58,
            'postgres': {'container_id': 'a' * 64, 'image_id': 'sha256:' + 'b' * 64,
                         'user': 'postgres', 'database': 'map_prod'},
            'redis': {'container_id': 'c' * 64, 'image_id': 'sha256:' + 'd' * 64},
            'admin': {'container_id': '1' * 64, 'image_id': 'sha256:' + '2' * 64,
                      'user': 'admin_provisioner', 'database': 'admin_control'}}


class TransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.gcs = FakeGcs(self.root / 'gcs')
        for item in (patch.object(transport, 'GCLOUD', str(self.gcs.binary)),
                     patch.object(transport, 'encrypted_volume', lambda directory: None)):
            item.start()
            self.addCleanup(item.stop)
        self.keys = self.root / 'keys'
        self.keys.mkdir(mode=0o700)
        self.identity = self.keys / 'identity.txt'
        self.cfg = config()

    def bundle(self, kind='pg'):
        job = self.root / ('job-' + kind)
        job.mkdir(mode=0o700)
        if kind == 'pg':
            item = {'Id': self.cfg['postgres']['container_id']}
            with patch.object(runner.ncp.redis, 'run', return_value='1'), \
                    patch.object(pg, 'dump_gzip', side_effect=postgres_dump), \
                    patch.object(pg, 'bootstrap_role', return_value='postgres'):
                helper = runner.Backend().collect('pg', self.cfg, item, job)
        else:
            rdb = job / 'map-redis-prod-fixture.rdb'
            transport.transfer.write_bytes(rdb, b'REDIS0011-SYNTHETIC')
            now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
            helper = job / 'map-redis.manifest.json'
            transport.transfer.write_json(helper, {
                'schema_version': 1, 'kind': 'redis-rdb', 'environment': 'prod', 'snapshot_at': now,
                'image_id': self.cfg['redis']['image_id'], 'primary_restore': {'rdb_check': 'PASS'},
                'files': [{'name': rdb.name, 'bytes': rdb.stat().st_size, 'sha256': transport.transfer.digest(rdb)}]})
            item = {'Id': self.cfg['redis']['container_id']}
        contract = runner.ncp.closed_contract('redis' if kind == 'redis' else 'pg', helper, item['Id'])
        transport.transfer.snapshot(contract, 'prod', job / 'closed')
        return job / 'closed'

    @contextlib.contextmanager
    def crypto(self):
        if HAS_AGE:
            subprocess.run(['age-keygen', '-o', str(self.identity)], check=True, capture_output=True)
            self.identity.chmod(0o600)
            self.recipient = subprocess.run(['age-keygen', '-y', str(self.identity)], check=True,
                                            capture_output=True, text=True).stdout.strip()
            yield
            return
        # Explicitly not encryption: orchestration only, when age is absent.
        self.identity.write_text('AGE-SECRET-KEY-1SYNTHETIC\n')
        self.identity.chmod(0o600)
        self.recipient = 'age1' + 'q' * 58

        def copy_file(source, target, *_):
            shutil.copyfile(source, target)
            Path(target).chmod(0o600)
        with patch.object(transport.transfer, 'age_encrypt', side_effect=copy_file), \
                patch.object(transport.transfer, 'age_decrypt', side_effect=copy_file):
            yield

    def restore(self, helper):
        fake = FakePostgres(self.root)
        output = io.StringIO()
        mask = os.umask(0o077)
        try:
            with patch.object(pg, 'run', side_effect=fake.run), \
                    patch.object(pg.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                    patch.object(pg.subprocess, 'Popen', side_effect=fake.popen), \
                    patch.object(sys, 'argv', ['pg_backup.py', 'restore', '--prod', str(helper)]), \
                    contextlib.redirect_stdout(output):
                code = pg.main()
        finally:
            os.umask(mask)
        return code, json.loads(output.getvalue()), bytes(fake.sql)

    def test_upload_download_then_pg_backup_restore_accepts_the_bundle(self):
        with self.crypto():
            receipt = transport.gcs_upload(self.bundle(), 'map-prod-backups', 'pg', self.recipient)
            self.assertTrue(receipt['gcs_remote_verified'])
            self.assertRegex(receipt['remote'], r'^gs://map-prod-backups/prod/pg/[a-f0-9]{32}$')
            if HAS_AGE:
                self.assertNotIn(b'COPY hub_data', self.gcs.object(receipt['remote'] + '/backup.age').read_bytes())
            target = self.root / 'restore' / 'recovered'
            target.parent.mkdir(mode=0o700)
            result = transport.gcs_download(receipt['remote'], target, self.identity, receipt['transport_sha256'])
        self.assertEqual(result['source_created_at'], receipt['source_created_at'])
        self.assertFalse(self.identity.exists())
        code, report, sql = self.restore(target / 'postgres.helper.json')
        self.assertEqual(code, 0)
        self.assertEqual(report['restore'], 'complete')
        self.assertEqual(report['verified_table_row_counts'], 2)
        self.assertEqual(report['source_bootstrap_role'], 'postgres')
        self.assertNotIn(b'CREATE ROLE postgres;', sql)

    def test_uploads_are_create_only_without_composite_parts_and_manifest_last(self):
        with self.crypto():
            receipt = transport.gcs_upload(self.bundle('redis'), 'map-prod-backups', 'redis', self.recipient)
        calls = self.gcs.calls()
        self.assertEqual([call['composite'] for call in calls], ['False'] * 4)
        uploads = [call['args'] for call in calls if call['args'][-1].startswith('gs://')]
        self.assertEqual([args[-1] for args in uploads],
                         [receipt['remote'] + '/backup.age', receipt['remote'] + '/transport.json'])
        self.assertTrue(all('--if-generation-match=0' in args for args in uploads))
        meta = json.loads(self.gcs.object(receipt['remote'] + '/transport.json').read_text())
        self.assertEqual((meta['schema'], meta['role'], meta['endpoint'], meta['encryption']),
                         ('map-gcs-age-transport-v1', 'prod', 'gs://map-prod-backups', 'age-x25519'))

    def test_existing_object_name_is_never_replaced(self):
        fixed = '0' * 32
        existing = self.gcs.object('gs://map-prod-backups/prod/pg/' + fixed + '/backup.age')
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'earlier copy')
        with self.crypto(), patch.object(transport.uuid, 'uuid4', return_value=SimpleNamespace(hex=fixed)):
            with self.assertRaisesRegex(transport.BackupError, 'gcs_object_already_exists'):
                transport.gcs_upload(self.bundle(), 'map-prod-backups', 'pg', self.recipient)
        self.assertEqual(existing.read_bytes(), b'earlier copy')

    def test_upload_fails_when_either_object_reads_back_different(self):
        with self.crypto():
            closed = self.bundle()
            for suffix, code in (('/backup.age', 'remote_checksum_mismatch'),
                                 ('/transport.json', 'remote_manifest_checksum_mismatch')):
                self.gcs.corrupt(suffix)
                with self.subTest(code=code), self.assertRaisesRegex(transport.BackupError, code):
                    transport.gcs_upload(closed, 'map-prod-backups', 'pg', self.recipient)

    def test_download_requires_trusted_fresh_untampered_same_bucket_transport(self):
        with self.crypto():
            receipt = transport.gcs_upload(self.bundle(), 'map-prod-backups', 'pg', self.recipient)
            key = self.identity.read_bytes()
            restore = self.root / 'restore'
            restore.mkdir(mode=0o700)

            def attempt(name, remote=receipt['remote'], sha=receipt['transport_sha256'], **kwargs):
                self.identity.write_bytes(key)
                self.identity.chmod(0o600)
                return transport.gcs_download(remote, restore / name, self.identity, sha, **kwargs)

            def later(**delta):
                return patch.object(transport.transfer, 'now_utc',
                                    return_value=dt.datetime.now(dt.timezone.utc) + dt.timedelta(**delta))

            with self.assertRaisesRegex(transport.BackupError, 'gcs_object_not_found'):
                attempt('missing', remote='gs://map-prod-backups/prod/pg/' + 'e' * 32)
            before = len(self.gcs.calls())
            with self.assertRaisesRegex(transport.BackupError, 'transport_trust_anchor_mismatch'):
                attempt('untrusted', sha='f' * 64)
            self.assertFalse(any(call['args'][-2].endswith('/backup.age') for call in self.gcs.calls()[before:]))
            self.assertFalse(self.identity.exists())
            foreign = 'gs://other-backups/prod/pg/' + receipt['remote'][-32:]
            shutil.copytree(self.gcs.object(receipt['remote']), self.gcs.object(foreign))
            with self.assertRaisesRegex(transport.BackupError, 'transport_contract_mismatch'):
                attempt('foreign', remote=foreign)
            with later(hours=2):
                with self.assertRaisesRegex(transport.BackupError, 'backup_rpo_age_exceeded'):
                    attempt('stale', max_age=3600)
                attempt('older-allowed', max_age=3 * 3600)
            # Without an explicit limit, any backup the bucket still keeps is accepted.
            with later(days=transport.RETENTION_DAYS - 1):
                attempt('within-retention')
            with later(days=transport.RETENTION_DAYS, hours=1):
                with self.assertRaisesRegex(transport.BackupError, 'backup_rpo_age_exceeded'):
                    attempt('past-retention')
            # Input errors found after the identity check still shred it.
            for name, code, inputs in (('within-retention', 'target_already_exists', {}),
                                       ('bad-remote', 'invalid_remote_prefix',
                                        {'remote': 'gs://map-prod-backups/dump/pg/' + 'a' * 32}),
                                       ('no-anchor', 'trusted_transport_sha256_required', {'sha': None})):
                with self.subTest(code=code):
                    with self.assertRaisesRegex(transport.BackupError, code):
                        attempt(name, **inputs)
                    self.assertFalse(self.identity.exists())
            cipher = self.gcs.object(receipt['remote'] + '/backup.age')
            cipher.write_bytes(cipher.read_bytes()[:-1] + b'X')
            with self.assertRaisesRegex(transport.BackupError, 'download_checksum_mismatch'):
                attempt('tampered')
        for name in ('untrusted', 'foreign', 'stale', 'past-retention', 'bad-remote', 'no-anchor', 'tampered'):
            self.assertFalse((restore / name).exists())
        self.assertTrue((restore / 'within-retention' / 'manifest.json').exists())
        self.assertFalse(self.identity.exists())

    def test_identity_must_be_private_file_in_private_directory(self):
        self.identity.write_text('AGE-SECRET-KEY-1SYNTHETIC\n')
        self.identity.chmod(0o644)
        with self.assertRaisesRegex(transport.BackupError, 'private_owned_0600_file_required'):
            transport.private_identity(self.identity)
        self.identity.chmod(0o600)
        self.keys.chmod(0o755)
        with self.assertRaisesRegex(transport.BackupError, 'private_identity_directory_required'):
            transport.private_identity(self.identity)
        self.assertTrue(self.identity.exists())

    def test_identity_must_sit_on_a_luks2_mapping(self):
        self.identity.write_text('AGE-SECRET-KEY-1SYNTHETIC\n')
        self.identity.chmod(0o600)
        mapper = '/dev/mapper/map-prod-data'
        for source, kind, allowed in (('/dev/sdb1', 'LUKS2', False), (mapper, 'LUKS1', False),
                                      (mapper, 'PLAIN', False), (mapper, 'LUKS2', True)):
            calls = []

            def system(argv, source=source, kind=kind):
                calls.append(argv)
                if argv[0] == 'findmnt':
                    return json.dumps({'filesystems': [{'source': source}]})
                return source + ' is active and is in use.\n  type:    ' + kind + '\n  cipher:  aes-xts-plain64\n'
            with self.subTest(source=source, kind=kind), \
                    patch.object(transport, 'encrypted_volume', ENCRYPTED_VOLUME), \
                    patch.object(transport, 'run_system', side_effect=system):
                if allowed:
                    self.assertEqual(transport.private_identity(self.identity), self.identity)
                    self.assertEqual(calls[-1], ['cryptsetup', 'status', mapper])
                else:
                    with self.assertRaisesRegex(transport.BackupError, 'encrypted_identity_volume_required'):
                        transport.private_identity(self.identity)
                self.assertEqual(calls[0][:4], ['findmnt', '--json', '--target', str(self.keys)])
        self.assertTrue(self.identity.exists())

    def test_gcloud_gets_only_the_pinned_environment_and_its_output_stays_private(self):
        inherited = {'HOME': str(self.root), 'CLOUDSDK_CONFIG': str(self.root / 'gcloud'),
                     'CLOUDSDK_CORE_ACCOUNT': 'other@example.invalid',
                     'GOOGLE_APPLICATION_CREDENTIALS': str(self.root / 'key.json')}
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, inherited), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(transport.BackupError) as caught:
            transport.gcloud_copy('gs://map-prod-backups/prod/pg/' + 'e' * 32 + '/transport.json', self.root / 'copy')
        # Only a constant code leaves; gcloud's own stderr text is never relayed.
        self.assertEqual(str(caught.exception), 'gcs_object_not_found')
        self.assertEqual(out.getvalue() + err.getvalue(), '')
        self.assertEqual(self.gcs.calls()[-1]['env'],
                         ['CLOUDSDK_CONFIG', 'CLOUDSDK_CORE_DISABLE_PROMPTS',
                          'CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED', 'HOME', 'LANG', 'PATH'])

    def test_only_a_regular_gcloud_outside_snap_is_used(self):
        for real, regular, allowed in (('/usr/lib/google-cloud-sdk/bin/gcloud', True, True),
                                       ('/usr/bin/snap', True, False),
                                       ('/snap/google-cloud-cli/123/bin/gcloud', True, False),
                                       ('/usr/bin/gcloud', False, False)):
            with self.subTest(real=real, regular=regular), \
                    patch.object(transport.os.path, 'realpath', return_value=real), \
                    patch.object(transport.os.path, 'isfile', return_value=regular):
                if allowed:
                    self.assertEqual(transport.gcloud_env()['CLOUDSDK_CORE_DISABLE_PROMPTS'], '1')
                else:
                    with self.assertRaisesRegex(transport.BackupError, 'apt_gcloud_required'):
                        transport.gcloud_env()

    def test_remote_and_bucket_shapes(self):
        self.assertEqual(transport.remote_root('map-prod-backups', 'admin'), 'gs://map-prod-backups/prod/admin')
        self.assertEqual(transport.parse_remote('gs://map-prod-backups/prod/admin/' + 'a' * 32),
                         ('map-prod-backups', 'admin'))
        for remote in ('gs://map-prod-backups/pg/' + 'a' * 32, 'gs://map-prod-backups/prod/pg/../x',
                       'gs://map-prod-backups/dump/pg/' + 'a' * 32, 's3://map-prod-backups/prod/pg/' + 'a' * 32,
                       'gs://Bad_Bucket/prod/pg/' + 'a' * 32):
            with self.subTest(remote=remote), self.assertRaises(transport.BackupError):
                transport.parse_remote(remote)

    def test_retention_matches_public_notice(self):
        # The privacy notice promises a 7-day expiry for recovery backups, and the
        # production bucket lifecycle takes its Delete age from this Terraform local.
        self.assertEqual(transport.RETENTION_DAYS, 7)
        terraform = (ROOT / 'gcp/terraform/envs/prod/locals.tf').read_text()
        days = re.findall(r'(?m)^\s*backup_retention_days\s*=\s*(\d+)\s*(?:#.*)?$', terraform)
        self.assertEqual(days, [str(transport.RETENTION_DAYS)])
        # The bucket module applies the "" key to every object, not one prefix.
        storage = (ROOT / 'gcp/terraform/envs/prod/storage.tf').read_text()
        blocks = [block for block in re.split(r'(?m)^}', storage)
                  if re.search(r'\bname\s*=\s*"map-prod-backups"', block)]
        self.assertEqual(len(blocks), 1)
        self.assertRegex(blocks[0], r'\bdelete_after_days\s*=\s*\{\s*""\s*=\s*local\.backup_retention_days\s*,?\s*\}')


class FixtureBackend:
    """Generated bytes only: no Docker, database, age or network."""

    def __init__(self):
        self.events, self.verified = [], True

    def verify_host(self, value):
        self.events.append('verify_host')

    def source(self, value, kind):
        self.events.append('source_' + kind)
        name = {'pg': 'postgres', 'redis': 'redis', 'admin': 'admin'}[kind]
        return {'Id': value[name]['container_id'], 'Image': value[name]['image_id']}

    def collect(self, kind, value, item, directory):
        self.events.append('collect_' + kind)
        now = dt.datetime.now(dt.timezone.utc)
        if kind == 'redis':
            names = ('map-redis-prod-fixture.rdb',)
        else:
            names = ('map-prod.roles.sql.gz', 'map-prod.sql.gz')
        for name in names:
            runner.transfer.write_bytes(directory / name, b'synthetic fixture bytes')
        files = [{'name': name, 'sha256': runner.transfer.digest(directory / name)} for name in names]
        if kind == 'redis':
            files[0]['bytes'] = (directory / names[0]).stat().st_size
            helper = {'schema_version': 1, 'kind': 'redis-rdb', 'environment': 'prod', 'snapshot_at': now.isoformat(),
                      'image_id': item['Image'], 'primary_restore': {'rdb_check': 'PASS'}, 'files': files}
        else:
            helper = {'version': 1, 'environment': 'prod', 'roles_have_passwords': False,
                      'database': 'map_prod' if kind == 'pg' else 'admin_control',
                      'created_at': now.strftime('%Y%m%dT%H%M%SZ'), 'files': files}
        path = directory / 'fixture.helper.json'
        runner.transfer.write_json(path, helper)
        return path

    def upload(self, snapshot, value, kind):
        self.events.append('upload_' + kind)
        verified = runner.transfer.verify(snapshot, 'prod')[1]
        return {'role': 'prod', 'transport': 'gcs-age-encrypted',
                'remote': transport.remote_root(value['bucket'], kind) + '/' + 'f' * 32,
                'transport_sha256': 'f' * 64, 'gcs_remote_verified': self.verified,
                'source_created_at': verified['source_created_at'], 'database_restore_executed': False}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cfg = config()
        self.backend = FixtureBackend()

    def test_configuration_is_exact_and_admin_pin_is_optional(self):
        path = self.root / 'backup-gcs.json'
        runner.transfer.write_json(path, self.cfg)
        self.assertEqual(runner.configuration(path), self.cfg)
        without_admin = {key: value for key, value in self.cfg.items() if key != 'admin'}
        bad = [{**self.cfg, 'remote': 's3://x/prod/y'}, {**self.cfg, 'bucket': 'Map_Prod'},
               {**self.cfg, 'age_recipient': 'age1short'}, {**self.cfg, 'enrollment_sha256': 'unpinned'},
               {**self.cfg, 'admin': {**self.cfg['admin'], 'container_id': 'a' * 64}}]
        database = copy.deepcopy(self.cfg)
        database['postgres']['database'] = 'map_test'
        for value in [without_admin] + bad + [database]:
            path.unlink()
            runner.transfer.write_json(path, value)
            if value is without_admin:
                self.assertEqual(runner.configuration(path), without_admin)
                continue
            with self.subTest(value=value), self.assertRaises(runner.transfer.BackupError):
                runner.configuration(path)

    def test_template_has_the_accepted_shape_and_only_placeholders(self):
        template = json.loads((ROOT / 'deploy/gcp/backup-gcs.template.json').read_text())

        def shape(value):
            return {key: sorted(item) if isinstance(item, dict) else None for key, item in value.items()}
        self.assertEqual(shape(template), shape(self.cfg))
        self.assertEqual((template['bucket'], template['postgres']['database']), ('map-prod-backups', 'map_prod'))
        path = self.root / 'backup-gcs.json'
        runner.transfer.write_json(path, template)
        with self.assertRaises(runner.transfer.BackupError):
            runner.configuration(path)

    def test_all_kinds_upload_keep_gcs_receipts_status_and_remove_plaintext(self):
        for kind in runner.KINDS:
            with self.subTest(kind=kind):
                result = runner.execute(kind, self.cfg, data=self.root, backend=self.backend)
                self.assertTrue(result['success'])
                self.assertFalse(result['rpo_1h_overdue'])
                status = json.loads((self.root / 'deploy' / (kind + '-backup-gcs-status.json')).read_text())
                self.assertEqual(status['code'], 'COMPLETE')
        self.assertEqual(len(list((self.root / 'deploy/backup-receipts-gcs').glob('*.json'))), 3)
        self.assertEqual({kind: list((self.root / 'backups-gcs' / kind).iterdir()) for kind in runner.KINDS},
                         {kind: [] for kind in runner.KINDS})
        self.assertFalse((self.root / 'deploy/backup-receipts').exists())

    def test_admin_without_pin_fails_before_host_or_source_access(self):
        without_admin = {key: value for key, value in self.cfg.items() if key != 'admin'}
        with self.assertRaisesRegex(runner.transfer.BackupError, 'admin_source_not_configured'):
            runner.execute('admin', without_admin, data=self.root, backend=self.backend)
        self.assertEqual(self.backend.events, [])
        self.assertFalse((self.root / 'deploy').exists())
        self.assertTrue(runner.execute('pg', without_admin, data=self.root, backend=self.backend)['success'])
        out = io.StringIO()
        self.addCleanup(os.umask, os.umask(0o022))
        with patch.object(runner.os, 'geteuid', return_value=0), \
                patch.object(runner, 'configuration', return_value=without_admin), \
                patch.object(runner, 'write_status'), patch.object(runner.signal, 'signal'), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(['admin']), 1)
        self.assertEqual(out.getvalue().splitlines(), ['MAP_BACKUP_RESULT=FAILED'])

    def test_unverified_receipt_never_replaces_last_success(self):
        first = runner.execute('pg', self.cfg, data=self.root, backend=self.backend)
        self.backend.verified = False
        with self.assertRaisesRegex(runner.transfer.BackupError, 'incomplete'):
            runner.execute('pg', self.cfg, data=self.root, backend=self.backend)
        failed = runner.write_status(self.root / 'deploy', 'pg', 'BACKUP_FAILED')
        self.assertEqual(failed['snapshot_at'], first['snapshot_at'])
        self.assertEqual(len(list((self.root / 'backups-gcs' / 'pg').iterdir())), 1)

    def test_failed_jobs_of_one_kind_never_block_another_kind(self):
        self.backend.verified = False
        for _ in range(4):
            with self.assertRaisesRegex(runner.transfer.BackupError, 'remote_backup_evidence_incomplete'):
                runner.execute('admin', self.cfg, data=self.root, backend=self.backend)
        with self.assertRaisesRegex(runner.transfer.BackupError, 'backup_failed_jobs_require_review'):
            runner.execute('admin', self.cfg, data=self.root, backend=self.backend)
        self.backend.verified = True
        self.assertTrue(runner.execute('pg', self.cfg, data=self.root, backend=self.backend)['success'])
        self.assertEqual(len(list((self.root / 'backups-gcs' / 'admin').iterdir())), 4)

    def test_admin_backend_pins_the_control_db_and_dumps_it_with_its_own_pin(self):
        admin = self.cfg['admin']

        def inspect(project, service):
            labels = {'com.docker.compose.project': project, 'com.docker.compose.service': service}
            return json.dumps({'Id': admin['container_id'], 'Image': admin['image_id'],
                               'State': {'Running': True}, 'Config': {'Labels': labels}})
        with patch.object(runner.ncp.redis, 'run', return_value=inspect('map-admin-prod', 'admin-control-db')) as run:
            item = runner.Backend().source(self.cfg, 'admin')
        self.assertEqual(run.call_args.args[0][-1], admin['container_id'])
        with patch.object(runner.ncp.redis, 'run', return_value=inspect('map-prod', 'postgres')), \
                self.assertRaisesRegex(runner.transfer.BackupError, 'production_source_drift'):
            runner.Backend().source(self.cfg, 'admin')
        dumps = []

        def dump(command, path):
            dumps.append(command)
            postgres_dump(command, path)
        job = runner.ncp.private_directory(self.root / 'job')
        with patch.object(runner.ncp.redis, 'run', return_value='1'), \
                patch.object(pg, 'dump_gzip', side_effect=dump), \
                patch.object(pg, 'bootstrap_role', return_value='admin_provisioner'):
            helper = runner.Backend().collect('admin', self.cfg, item, job)
        self.assertEqual(dumps[-1], ['docker', 'exec', admin['container_id'], 'pg_dump', '--clean', '--if-exists',
                                     '-U', 'admin_provisioner', '-d', 'admin_control'])
        self.assertEqual(runner.transfer.read_json(helper)['database'], 'admin_control')

    def test_damaged_status_file_does_not_block_the_next_status(self):
        state = runner.ncp.private_directory(self.root / 'deploy')
        runner.transfer.write_bytes(state / 'redis-backup-gcs-status.json', b'{not json')
        result = runner.execute('redis', self.cfg, data=self.root, backend=self.backend)
        self.assertEqual((result['code'], result['success']), ('COMPLETE', True))

    def test_deployment_lock_defers_before_source_access(self):
        state = runner.ncp.private_directory(self.root / 'deploy')
        with runner.ncp.lock_file(state / 'deploy.lock') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = runner.execute('redis', self.cfg, data=self.root, backend=self.backend, lock_wait=0)
        self.assertEqual(result['code'], 'LOCK_BUSY')
        self.assertEqual(self.backend.events, ['verify_host'])

    def test_main_prints_exactly_one_result_line(self):
        fresh = {'code': 'LOCK_BUSY', 'success': False, 'rpo_1h_overdue': False, 'remote': None, 'transport_sha256': None}
        cases = [({**fresh, 'code': 'COMPLETE', 'success': True}, 0, ['MAP_BACKUP_RESULT=COMPLETE kind=admin']),
                 (fresh, 0, []), ({**fresh, 'rpo_1h_overdue': True}, 1, ['MAP_BACKUP_RESULT=FAILED']),
                 (runner.transfer.BackupError('production_source_drift'), 1, ['MAP_BACKUP_RESULT=FAILED'])]
        self.addCleanup(os.umask, os.umask(0o022))
        for outcome, code, lines in cases:
            out, err = io.StringIO(), io.StringIO()
            effect = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
            with self.subTest(outcome=outcome), patch.object(runner.os, 'geteuid', return_value=0), \
                    patch.object(runner, 'configuration', return_value=self.cfg), \
                    patch.object(runner, 'execute', **effect), patch.object(runner, 'write_status'), \
                    patch.object(runner.signal, 'signal'), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(runner.main(['admin']), code)
                self.assertEqual([line for line in out.getvalue().splitlines() if line.startswith('MAP_BACKUP_RESULT')], lines)

    def test_runner_imports_only_sources_no_other_account_can_change(self):
        scripts = Path('/opt/map-ops-gcp/scripts')
        good = {str(path): SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_nlink=2)
                for path in (scripts, *scripts.parents)}
        good.update({str(scripts / name): SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_nlink=1)
                     for name in runner.IMPORTS})
        changes = [None, ('/opt', {'st_mode': stat.S_IFDIR | 0o775}), (str(scripts), {'st_uid': 1000}),
                   (str(scripts / 'pg_backup.py'), {'st_uid': 1000}),
                   (str(scripts / 'backup_job.py'), {'st_nlink': 2}),
                   (str(scripts / 'gcs_backup_transport.py'), {'st_mode': stat.S_IFREG | 0o664}),
                   (str(scripts / 'ncp-bootstrap-host.py'), {'st_mode': stat.S_IFLNK | 0o777})]
        for change in changes:
            table = copy.deepcopy(good)
            if change:
                vars(table[change[0]]).update(change[1])
            with self.subTest(change=change), \
                    patch.object(Path, 'lstat', autospec=True, side_effect=lambda path: table[str(path)]):
                if change is None:
                    runner.trusted_source(scripts)
                    continue
                with self.assertRaisesRegex(RuntimeError, 'root_source_ownership_required'):
                    runner.trusted_source(scripts)

    def test_units_keep_ncp_schedule_and_sandbox_with_private_gcloud_home(self):
        schedule = {'pg': '*:00,30:00', 'redis': '*:15,45:00', 'admin': '*:05,35:00'}
        for kind, calendar in schedule.items():
            unit = 'map-prod-' + kind + '-backup-gcs'
            service = (ROOT / 'deploy/gcp' / (unit + '.service')).read_text()
            timer = (ROOT / 'deploy/gcp' / (unit + '.timer')).read_text()
            with self.subTest(kind=kind):
                self.assertRegex(unit + '.service', LOG_UNIT)
                self.assertIn('OnCalendar=*-*-* ' + calendar + '\n', timer)
                for line in ('Persistent=true', 'AccuracySec=1s', 'Unit=' + unit + '.service'):
                    self.assertIn(line + '\n', timer)
                # Bytecode is looked up only in the empty per-run directory, never
                # in a __pycache__ inside the checkout; -B alone still reads one.
                for line in ('ExecStart=/usr/bin/python3 -B -X pycache_prefix=/run/' + unit +
                             ' /opt/map-ops-gcp/scripts/gcs-production-backup.py ' + kind,
                             'RuntimeDirectory=' + unit, 'RuntimeDirectoryMode=0700', 'Environment=PATH=/usr/bin:/bin',
                             'Environment=HOME=/run/' + unit, 'Environment=CLOUDSDK_CONFIG=/run/' + unit,
                             'Environment=CLOUDSDK_CORE_DISABLE_PROMPTS=1',
                             'Environment=CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED=False',
                             'NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=yes',
                             'ReadWritePaths=/srv/map-prod /var/run/docker.sock', 'TimeoutStartSec=900',
                             'KillMode=control-group'):
                    self.assertIn(line + '\n', service)
                self.assertNotIn('/snap/', service)
                self.assertNotIn('map-service-infra', service)


if __name__ == '__main__':
    unittest.main()
