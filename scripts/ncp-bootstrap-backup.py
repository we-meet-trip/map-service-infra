#!/usr/bin/env python3
"""Stage immutable closed backups and recover into a fresh, private directory.

This does not stop a database, copy a live volume, import SQL/RDB, switch traffic,
delete old data, or create cloud resources. The original PG/Redis helper owns
logical consistency and database restore checks. `restore` and `rollback` recover
the verified backup files only; application recovery remains a separate gate.

Contract v1 (JSON, paths are flat names relative to the contract):
  schema_version, role, source, source_id, environment, created_at, format,
  consistency, files:[{name, bytes, sha256}].
Real sources require helper_manifest + helper_manifest_sha256. PostgreSQL also
requires database. Synthetic fixtures require stopped:true and synthetic:true.
Only prod/admin may consume PG; only prod may consume Redis. Learning real data
remains HOLD until an approved exporter contract is supplied.

NCP transfer requires the installed age and AWS CLIs, private credential files,
an X25519 recipient, and a trusted transport SHA256 on download. There are no
install, credential discovery, public-ACL, bucket-create, or delete commands.
Official endpoint/interface: https://cli.ncloud-docs.com/docs/en/guide-objectstorage
"""
import argparse
import configparser
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile


ROLES = ('prod', 'admin', 'learning')
ENDPOINT = 'https://kr.object.ncloudstorage.com'
SHA = re.compile(r'[0-9a-f]{64}')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}')
MAX_JSON = 1024 * 1024
MAX_FILES = 64
MAX_BACKUP_BYTES = 1024 ** 4
SCHEMA = 'map-ncp-closed-backup-v1'


class BackupError(RuntimeError):
    """Only constant, nonsensitive codes are suitable for stderr."""


def require(condition, code):
    if not condition:
        raise BackupError(code)


def utc(value):
    require(isinstance(value, str) and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value),
        'explicit_utc_timestamp_required')
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def age_seconds(created, max_age, now=None):
    require(type(max_age) is int and 1 <= max_age <= 366 * 86400, 'invalid_rpo_limit')
    age = ((now or now_utc()) - utc(created)).total_seconds()
    require(age >= -60, 'future_backup_timestamp')
    require(age <= max_age, 'backup_rpo_age_exceeded')
    return max(0, round(age, 3))


def clean_path(path, *, exists=True):
    """Reject traversal and symlinks in every supplied path component.

    Callers on macOS should use /private/tmp (not its /tmp symlink). Normalizing
    untrusted paths with resolve() would conceal symlink inputs, so do not do it.
    """
    path = Path(path)
    require('..' not in path.parts, 'path_traversal_rejected')
    path = Path(os.path.abspath(path))
    for component in reversed((path,) + tuple(path.parents)):
        if component.exists() or component.is_symlink():
            require(not component.is_symlink(), 'symlink_rejected')
    require(not exists or path.exists(), 'input_missing')
    return path


def file_info(path, *, private=False):
    path = clean_path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'regular_single_link_file_required')
    if private:
        require(stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid(),
                'private_owned_0600_file_required')
    return info


@contextlib.contextmanager
def open_read(path, *, private=False):
    before = file_info(path, private=private)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino), 'input_changed')
        yield stream
        after = os.fstat(stream.fileno())
        require((info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'input_changed')


def digest(path):
    value = hashlib.sha256()
    with open_read(path) as source:
        for block in iter(lambda: source.read(1024 ** 2), b''):
            value.update(block)
    return value.hexdigest()


def read_json(path, *, private=False):
    with open_read(path, private=private) as stream:
        raw = stream.read(MAX_JSON + 1)
    require(len(raw) <= MAX_JSON, 'manifest_size_limit')
    def unique_pairs(pairs):
        result = {}
        for key, item in pairs:
            require(key not in result, 'duplicate_json_key')
            result[key] = item
        return result
    value = json.loads(raw, object_pairs_hook=unique_pairs)
    require(isinstance(value, dict), 'manifest_object_required')
    return value


def write_bytes(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def write_json(path, data):
    write_bytes(path, (json.dumps(data, indent=2, sort_keys=True) + '\n').encode())


def safe_name(value):
    require(isinstance(value, str) and NAME.fullmatch(value) and value not in
            ('manifest.json', 'transport.json', 'backup.age', 'backup.zip'), 'unsafe_artifact_name')
    return value


def entries(value):
    require(isinstance(value, list) and 1 <= len(value) <= MAX_FILES, 'invalid_file_count')
    seen, total = set(), 0
    for item in value:
        require(isinstance(item, dict) and set(item) == {'name', 'bytes', 'sha256'}, 'invalid_file_entry')
        name = safe_name(item['name'])
        require(name not in seen, 'duplicate_artifact_name')
        seen.add(name)
        require(type(item['bytes']) is int and item['bytes'] >= 0, 'invalid_file_size')
        total += item['bytes']
        require(isinstance(item['sha256'], str) and SHA.fullmatch(item['sha256']), 'invalid_checksum')
    require(total <= MAX_BACKUP_BYTES, 'backup_size_limit')
    return value


def verify_entries(directory, records, *, private=False):
    for entry in entries(records):
        path = directory / entry['name']
        require(file_info(path, private=private).st_size == entry['bytes'], 'backup_size_mismatch')
        require(digest(path) == entry['sha256'], 'backup_checksum_mismatch')


def validate_contract(meta, directory, role, max_age):
    require(meta.get('schema_version') == 1 and meta.get('role') == role and role in ROLES,
            'backup_role_or_version_mismatch')
    require(meta.get('environment') == role, 'source_environment_mismatch')
    require(isinstance(meta.get('source_id'), str) and NAME.fullmatch(meta['source_id']),
            'source_identity_required')
    age_seconds(meta.get('created_at'), max_age)
    records = entries(meta.get('files'))
    if meta.get('source') == 'synthetic-stopped-fixture':
        require(meta.get('format') == 'synthetic-stopped-v1' and
                meta.get('consistency') == 'stopped-synthetic-fixture' and
                meta.get('stopped') is True and meta.get('synthetic') is True,
                'stopped_synthetic_fixture_required')
        require('helper_manifest' not in meta and 'helper_manifest_sha256' not in meta,
                'synthetic_helper_confusion')
        verify_entries(directory, records)
        return records
    require(meta.get('consistency') == 'closed-logical-backup' and meta.get('synthetic') is not True,
            'closed_logical_backup_required')
    helper_name = safe_name(meta.get('helper_manifest'))
    require(helper_name not in {entry['name'] for entry in records}, 'helper_payload_collision')
    helper_path = directory / helper_name
    require(SHA.fullmatch(meta.get('helper_manifest_sha256', '')) and
            digest(helper_path) == meta['helper_manifest_sha256'], 'helper_manifest_checksum_mismatch')
    helper = read_json(helper_path)
    # The existing helpers spell their environment test/prod. This adapter only
    # accepts prod outputs and binds an admin PG to an explicit expected DB name.
    require(helper.get('environment') == 'prod', 'helper_environment_mismatch')
    if meta.get('source') == 'pg_backup.py':
        require(role in ('prod', 'admin') and meta.get('format') == 'pg-logical-gzip-v1',
                'postgres_role_format_mismatch')
        require(helper.get('version') == 1 and helper.get('roles_have_passwords') is False and
                isinstance(meta.get('database'), str) and helper.get('database') == meta['database'] and
                re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', meta['database']), 'postgres_source_mismatch')
        created = dt.datetime.strptime(helper['created_at'], '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
        require(abs((created - utc(meta['created_at'])).total_seconds()) < 1, 'source_timestamp_mismatch')
        helper_files = helper.get('files')
        require(isinstance(helper_files, list) and len(helper_files) == 2 and len(records) == 2,
                'postgres_roles_and_data_required')
        require(helper_files[0]['name'].endswith('.roles.sql.gz') and
                helper_files[1]['name'].endswith('.sql.gz') and
                not helper_files[1]['name'].endswith('.roles.sql.gz'), 'postgres_dump_order_mismatch')
    elif meta.get('source') == 'redis_backup.py':
        require(role == 'prod' and meta.get('format') == 'redis-rdb-v1', 'redis_role_format_mismatch')
        require(helper.get('schema_version') == 1 and helper.get('kind') == 'redis-rdb' and
                helper.get('primary_restore', {}).get('rdb_check') == 'PASS' and
                re.fullmatch(r'sha256:[0-9a-f]{64}', helper.get('image_id', '')),
                'redis_verified_source_required')
        require(utc(helper['snapshot_at']) == utc(meta['created_at']), 'source_timestamp_mismatch')
        helper_files = helper.get('files')
        require(isinstance(helper_files, list) and len(helper_files) == 1 and len(records) == 1 and
                helper_files[0]['name'].endswith('.rdb'), 'one_redis_rdb_required')
    else:
        raise BackupError('unsupported_closed_backup_source')
    require([(item['name'], item['sha256']) for item in helper_files] ==
            [(item['name'], item['sha256']) for item in records], 'helper_payload_mismatch')
    verify_entries(directory, records)
    # Keep the unchanged original helper manifest next to its payload, so the
    # original database restore verifier can consume the recovered files.
    return records + [{'name': helper_name, 'bytes': helper_path.stat().st_size,
                       'sha256': meta['helper_manifest_sha256']}]


def copy_checked(source, target, record):
    hasher, total = hashlib.sha256(), 0
    with open_read(source) as inp:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as out:
            for block in iter(lambda: inp.read(1024 ** 2), b''):
                total += len(block)
                require(total <= record['bytes'], 'input_changed')
                hasher.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
    require(total == record['bytes'] and hasher.hexdigest() == record['sha256'], 'copy_checksum_mismatch')


def fresh_target(path):
    path = clean_path(path, exists=False)
    require(not path.exists() and not path.is_symlink(), 'target_already_exists')
    require(path.parent.is_dir(), 'target_parent_missing')
    return path


def disk_guard(directory, required_bytes):
    require(shutil.disk_usage(directory).free >= required_bytes + 16 * 1024 ** 2,
            'insufficient_staging_disk_space')


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(stage, target):
    """Exclusive new directory; manifest is the atomic committed-snapshot marker.

    No rename can replace an existing directory. Interrupted uncommitted output
    is preserved for inspection and rejected by every reader and future writer.
    """
    target.mkdir(mode=0o700, exist_ok=False)
    for path in sorted(stage.iterdir()):
        if path.name != 'manifest.json':
            os.rename(path, target / path.name)
    sync_dir(target)
    os.rename(stage / 'manifest.json', target / 'manifest.json')
    sync_dir(target)
    sync_dir(target.parent)


def verify(snapshot, role, max_age=3600):
    snapshot = clean_path(snapshot)
    require(snapshot.is_dir() and stat.S_IMODE(snapshot.stat().st_mode) == 0o700 and
            snapshot.stat().st_uid == os.geteuid(), 'private_owned_snapshot_directory_required')
    manifest = read_json(snapshot / 'manifest.json', private=True)
    require(manifest.get('schema') == SCHEMA and manifest.get('role') == role,
            'snapshot_role_or_schema_mismatch')
    require(isinstance(manifest.get('contract'), dict), 'snapshot_contract_required')
    age = age_seconds(manifest['contract'].get('created_at'), max_age)
    expected = validate_contract(manifest['contract'], snapshot, role, max_age)
    require(manifest.get('files') == expected, 'snapshot_file_contract_mismatch')
    verify_entries(snapshot, expected, private=True)
    require({path.name for path in snapshot.iterdir()} == {'manifest.json'} |
            {item['name'] for item in expected}, 'unexpected_snapshot_entry')
    return manifest, {'role': role, 'source_created_at': manifest['contract']['created_at'],
                      'rpo_age_seconds': age, 'files': len(expected),
                      'manifest_sha256': digest(snapshot / 'manifest.json')}


def snapshot(contract, role, output, max_age=3600):
    contract = clean_path(contract)
    output = fresh_target(output)
    meta = read_json(contract)
    records = validate_contract(meta, contract.parent, role, max_age)
    disk_guard(output.parent, sum(item['bytes'] for item in records) + MAX_JSON)
    with tempfile.TemporaryDirectory(prefix='.map-ncp-stage-', dir=output.parent) as work:
        stage = Path(work)
        stage.chmod(0o700)
        for entry in records:
            copy_checked(contract.parent / entry['name'], stage / entry['name'], entry)
        write_json(stage / 'manifest.json', {'schema': SCHEMA, 'role': role,
                   'packaged_at': now_utc().isoformat(), 'contract': meta, 'files': records})
        verify(stage, role, max_age)
        publish(stage, output)
    return verify(output, role, max_age)[1]


def restore(source, role, target, max_age=3600):
    source = clean_path(source)
    target = fresh_target(target)
    manifest, _ = verify(source, role, max_age)
    disk_guard(target.parent, sum(item['bytes'] for item in manifest['files']) + MAX_JSON)
    with tempfile.TemporaryDirectory(prefix='.map-ncp-recover-', dir=target.parent) as work:
        stage = Path(work)
        stage.chmod(0o700)
        for entry in manifest['files']:
            copy_checked(source / entry['name'], stage / entry['name'], entry)
        entry = {'bytes': (source / 'manifest.json').stat().st_size,
                 'sha256': digest(source / 'manifest.json')}
        copy_checked(source / 'manifest.json', stage / 'manifest.json', entry)
        verify(stage, role, max_age)
        publish(stage, target)
    return verify(target, role, max_age)[1]


def fixture_transfer(source, role, target, max_age=3600):
    manifest, _ = verify(source, role, max_age)
    require(manifest['contract'].get('source') == 'synthetic-stopped-fixture',
            'local_offhost_simulation_synthetic_only')
    source, target = clean_path(source), fresh_target(target)
    require(source != target and source not in target.parents and target not in source.parents,
            'separate_fixture_directory_required')
    result = restore(source, role, target, max_age)
    result.update({'transport': 'local-separate-directory-synthetic-fixture',
                   'ncp_remote_verified': False, 'encryption_executed': False})
    return result


def command(argv, *, env=None, stdin=None):
    # Never relay child output. Even a failed CLI may print credentials/content.
    result = subprocess.run(argv, stdin=subprocess.DEVNULL if stdin is None else stdin, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env, timeout=3600)
    require(result.returncode == 0, 'external_command_failed')


@contextlib.contextmanager
def aws_credentials(path, private_dir=None):
    """Use only an explicit 0600 file or stdin; suppress inherited AWS discovery."""
    with tempfile.TemporaryDirectory(prefix='map-ncp-auth-', dir=private_dir) as work:
        directory = Path(work).resolve()
        directory.chmod(0o700)
        if str(path) == '-':
            raw = sys.stdin.buffer.read(16385)
            require(0 < len(raw) <= 16384, 'credentials_size_limit')
            credential = directory / 'credentials'
            write_bytes(credential, raw)
        else:
            supplied = clean_path(path)
            with open_read(supplied, private=True) as inp:
                raw = inp.read(16385)
            require(0 < len(raw) <= 16384, 'credentials_size_limit')
            credential = directory / 'credentials'
            write_bytes(credential, raw)
        ini = configparser.ConfigParser(interpolation=None)
        ini.read_string(raw.decode('utf-8'))
        require(ini.sections() == ['default'] and
                set(ini['default']) == {'aws_access_key_id', 'aws_secret_access_key'} and
                all(ini['default'][name].strip() for name in ini['default']),
                'explicit_default_static_credentials_required')
        env = {key: value for key, value in os.environ.items() if not key.startswith('AWS_')}
        env.update({'AWS_SHARED_CREDENTIALS_FILE': str(credential), 'AWS_CONFIG_FILE': os.devnull,
                    'AWS_EC2_METADATA_DISABLED': 'true', 'AWS_PAGER': '', 'AWS_DEFAULT_REGION': 'kr-standard',
                    'AWS_REQUEST_CHECKSUM_CALCULATION': 'when_required',
                    'AWS_RESPONSE_CHECKSUM_VALIDATION': 'when_required'})
        yield env


def remote_prefix(remote, role):
    require(isinstance(remote, str) and re.fullmatch(
        r's3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/[A-Za-z0-9_./-]+', remote), 'invalid_remote_prefix')
    parts = remote[5:].split('/')
    require(parts[1] == role and all(part not in ('', '.', '..') for part in parts),
            'role_scoped_remote_prefix_required')
    return remote


def aws_copy(source, target, env, *, upload=False):
    argv = ['aws', '--endpoint-url', ENDPOINT, '--region', 'kr-standard', 's3', 'cp',
            str(source), str(target), '--only-show-errors']
    if upload:
        argv += ['--acl', 'private']
    command(argv, env=env)


def age_encrypt(source, target, recipient):
    source = clean_path(source)
    target = fresh_target(target)
    require(isinstance(recipient, str) and re.fullmatch(r'age1[023456789acdefghjklmnpqrstuvwxyz]{58}', recipient),
            'age_x25519_recipient_required')
    command(['age', '--encrypt', '--recipient', recipient, '--output', str(target), str(source)])
    file_info(target)
    target.chmod(0o600)


def age_decrypt(source, target, identity):
    source = clean_path(source)
    target = fresh_target(target)
    identity = clean_path(identity)
    file_info(identity, private=True)
    command(['age', '--decrypt', '--identity', str(identity), '--output', str(target), str(source)])
    file_info(target)
    target.chmod(0o600)


def pack(source, archive, role, max_age):
    manifest, _ = verify(source, role, max_age)
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_STORED) as out:
        for name in ['manifest.json'] + [entry['name'] for entry in manifest['files']]:
            with open_read(source / name, private=True) as inp:
                item = zipfile.ZipInfo(name)
                item.create_system = 3
                item.external_attr = (stat.S_IFREG | 0o600) << 16
                with out.open(item, 'w', force_zip64=True) as member:
                    shutil.copyfileobj(inp, member, length=1024 ** 2)
    archive.chmod(0o600)


def unpack(archive, target):
    """Reject links, paths, duplicates and compression bombs before extraction."""
    target.mkdir(mode=0o700)
    with zipfile.ZipFile(archive, 'r') as source:
        items = source.infolist()
        require(2 <= len(items) <= MAX_FILES + 2, 'invalid_archive_count')
        require(len({item.filename for item in items}) == len(items), 'duplicate_archive_member')
        require(sum(item.file_size for item in items) <= MAX_BACKUP_BYTES + MAX_JSON,
                'archive_size_limit')
        for item in items:
            require(item.filename == 'manifest.json' or bool(NAME.fullmatch(item.filename)),
                    'unsafe_archive_name')
            require(item.create_system == 3 and stat.S_ISREG(item.external_attr >> 16) and
                    item.compress_type == zipfile.ZIP_STORED and not item.flag_bits & 1,
                    'unsafe_archive_member')
        for item in items:
            with source.open(item) as inp:
                fd = os.open(target / item.filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    shutil.copyfileobj(inp, out, length=1024 ** 2)
                    out.flush()
                    os.fsync(out.fileno())


def ncp_upload(source, role, remote, credential, recipient, max_age=3600):
    source = clean_path(source)
    manifest, verified = verify(source, role, max_age)
    prefix = remote_prefix(remote, role)
    # Unique keys are append-only; no existing remote key is targeted or removed.
    destination = prefix + '/' + uuid.uuid4().hex
    # Keep plaintext ZIP and transient credential copies on the source's private
    # encrypted data volume, never silently on the unencrypted OS /tmp partition.
    with tempfile.TemporaryDirectory(prefix='map-ncp-encrypted-', dir=source.parent) as work:
        stage = Path(work).resolve()
        stage.chmod(0o700)
        disk_guard(stage, 4 * (sum(item['bytes'] for item in manifest['files']) + MAX_JSON))
        pack(source, stage / 'backup.zip', role, max_age)
        age_encrypt(stage / 'backup.zip', stage / 'backup.age', recipient)
        ciphertext = stage / 'backup.age'
        transport = {'schema': 'map-ncp-age-transport-v1', 'role': role,
                     'source_created_at': verified['source_created_at'],
                     'endpoint': ENDPOINT, 'encryption': 'age-x25519',
                     'manifest_sha256': verified['manifest_sha256'],
                     'ciphertext_sha256': digest(ciphertext), 'ciphertext_bytes': ciphertext.stat().st_size}
        write_json(stage / 'transport.json', transport)
        with aws_credentials(credential, stage) as env:
            aws_copy(ciphertext, destination + '/backup.age', env, upload=True)
            aws_copy(destination + '/backup.age', stage / 'readback.age', env)
            require(digest(stage / 'readback.age') == transport['ciphertext_sha256'], 'remote_checksum_mismatch')
            aws_copy(stage / 'transport.json', destination + '/transport.json', env, upload=True)
            aws_copy(destination + '/transport.json', stage / 'readback.json', env)
            transport_sha = digest(stage / 'transport.json')
            require(digest(stage / 'readback.json') == transport_sha, 'remote_manifest_checksum_mismatch')
        return {'role': role, 'transport': 'ncp-kr-age-encrypted', 'remote': destination,
                'transport_sha256': transport_sha, 'ncp_remote_verified': True,
                'database_restore_executed': False, 'source_created_at': verified['source_created_at']}


def ncp_download(remote, role, target, credential, identity, transport_sha, max_age=3600):
    target = fresh_target(target)
    remote = remote_prefix(remote, role)
    require(isinstance(transport_sha, str) and SHA.fullmatch(transport_sha),
            'trusted_transport_sha256_required')
    with tempfile.TemporaryDirectory(prefix='.map-ncp-download-', dir=target.parent) as work:
        stage = Path(work)
        stage.chmod(0o700)
        with aws_credentials(credential, stage) as env:
            aws_copy(remote + '/transport.json', stage / 'transport.json', env)
            require(digest(stage / 'transport.json') == transport_sha, 'transport_trust_anchor_mismatch')
            meta = read_json(stage / 'transport.json')
            require(meta.get('schema') == 'map-ncp-age-transport-v1' and meta.get('role') == role and
                    meta.get('endpoint') == ENDPOINT and meta.get('encryption') == 'age-x25519',
                    'transport_contract_mismatch')
            age_seconds(meta.get('source_created_at'), max_age)
            require(type(meta.get('ciphertext_bytes')) is int and
                    0 < meta['ciphertext_bytes'] <= MAX_BACKUP_BYTES + 32 * 1024 ** 3 and
                    SHA.fullmatch(meta.get('ciphertext_sha256', '')) and
                    SHA.fullmatch(meta.get('manifest_sha256', '')), 'invalid_transport_payload')
            disk_guard(stage, 3 * meta['ciphertext_bytes'] + MAX_JSON)
            aws_copy(remote + '/backup.age', stage / 'backup.age', env)
        require(file_info(stage / 'backup.age').st_size == meta['ciphertext_bytes'] and
                digest(stage / 'backup.age') == meta['ciphertext_sha256'], 'download_checksum_mismatch')
        age_decrypt(stage / 'backup.age', stage / 'backup.zip', identity)
        unpack(stage / 'backup.zip', stage / 'snapshot')
        require(digest(stage / 'snapshot' / 'manifest.json') == meta['manifest_sha256'],
                'plaintext_manifest_checksum_mismatch')
        _, recovered = verify(stage / 'snapshot', role, max_age)
        require(recovered['source_created_at'] == meta['source_created_at'], 'transport_source_timestamp_mismatch')
        publish(stage / 'snapshot', target)
    result = verify(target, role, max_age)[1]
    result.update({'transport': 'ncp-kr-age-encrypted', 'ncp_remote_verified': True,
                   'database_restore_executed': False})
    return result


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('snapshot', 'verify', 'restore', 'rollback', 'upload', 'download'))
    parser.add_argument('--role', required=True, choices=ROLES)
    parser.add_argument('--contract', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--target', type=Path)
    parser.add_argument('--max-age-seconds', type=int, default=3600)
    parser.add_argument('--offhost-dir', type=Path)
    parser.add_argument('--remote')
    parser.add_argument('--credentials-file', help='0600 AWS INI file, or - for stdin')
    parser.add_argument('--age-recipient')
    parser.add_argument('--age-identity-file', type=Path)
    parser.add_argument('--transport-sha256')
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        if args.action == 'snapshot':
            require(args.contract is not None and args.output is not None, 'contract_and_output_required')
            result = snapshot(args.contract, args.role, args.output, args.max_age_seconds)
        elif args.action == 'verify':
            require(args.snapshot is not None, 'snapshot_required')
            result = verify(args.snapshot, args.role, args.max_age_seconds)[1]
        elif args.action in ('restore', 'rollback'):
            require(args.snapshot is not None and args.target is not None, 'snapshot_and_fresh_target_required')
            result = restore(args.snapshot, args.role, args.target, args.max_age_seconds)
        else:
            require(bool(args.offhost_dir) != bool(args.remote), 'one_explicit_transport_required')
            if args.offhost_dir:
                require(not any((args.credentials_file, args.age_recipient, args.age_identity_file,
                                 args.transport_sha256)), 'fixture_and_cloud_inputs_mixed')
                source = args.snapshot if args.action == 'upload' else args.offhost_dir
                target = args.offhost_dir if args.action == 'upload' else args.target
                require(source is not None and target is not None, 'transfer_source_and_target_required')
                result = fixture_transfer(source, args.role, target, args.max_age_seconds)
            else:
                require(args.credentials_file is not None, 'explicit_credentials_required')
                if args.action == 'upload':
                    require(args.snapshot is not None, 'snapshot_required')
                    result = ncp_upload(args.snapshot, args.role, args.remote, args.credentials_file,
                                        args.age_recipient, args.max_age_seconds)
                else:
                    require(args.target is not None and args.age_identity_file is not None,
                            'fresh_target_and_private_identity_required')
                    result = ncp_download(args.remote, args.role, args.target, args.credentials_file,
                        args.age_identity_file, args.transport_sha256, args.max_age_seconds)
        result.update({'success': True, 'action': args.action,
                       'scope': 'closed backup files; no database import or service cutover',
                       'elapsed_seconds': round(time.monotonic() - started, 3)})
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({'success': False, 'action': args.action,
                          'code': str(error) if isinstance(error, BackupError) else 'operation_failed',
                          'error_type': type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
