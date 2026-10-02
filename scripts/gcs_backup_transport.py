#!/usr/bin/env python3
"""Encrypted closed-backup transport to one private GCS bucket.

upload: verify the closed snapshot, ZIP it, encrypt it to an age X25519
recipient, write transport.json, then create
gs://<bucket>/prod/<kind>/<uuid32>/{backup.age,transport.json} with a
create-only precondition and read both back to compare SHA256. download: needs
the trusted transport SHA256 and a private age identity on an encrypted volume;
the identity is shredded when it ends.

There is no bucket, IAM, lifecycle or delete operation here. The caller only
needs object create and object read; the bucket lifecycle expires objects after
RETENTION_DAYS. Parallel composite upload stays off: it would need delete
permission for its temporary component objects.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid


def _module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


transfer = _module('gcs_transport_closed_backup', 'ncp-bootstrap-backup.py')
BackupError, require = transfer.BackupError, transfer.require
GCLOUD = '/usr/bin/gcloud'
ROLE = 'prod'
KINDS = ('pg', 'redis', 'admin')
PREFIX = 'prod'
SCHEMA = 'map-gcs-age-transport-v1'
TRANSPORT = 'gcs-age-encrypted'
# The bucket lifecycle deletes objects at this age; the public privacy notice
# states the same number of days, so both are pinned by tests.
RETENTION_DAYS = 7
BUCKET = re.compile(r'[a-z0-9][a-z0-9_.-]{1,61}[a-z0-9]')
RECIPIENT = re.compile(r'age1[023456789acdefghjklmnpqrstuvwxyz]{58}')
SYSTEM_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}


def bucket_name(value):
    require(isinstance(value, str) and BUCKET.fullmatch(value) and '..' not in value, 'invalid_gcs_bucket')
    return value


def remote_root(bucket, kind):
    require(kind in KINDS, 'invalid_backup_kind')
    return 'gs://' + bucket_name(bucket) + '/' + PREFIX + '/' + kind


def parse_remote(remote):
    match = isinstance(remote, str) and re.fullmatch(
        r'gs://([^/]+)/' + PREFIX + '/(' + '|'.join(KINDS) + r')/[a-f0-9]{32}', remote)
    require(match, 'invalid_remote_prefix')
    return bucket_name(match[1]), match[2]


def gcloud_env():
    # Only the runtime home/config pass through. No inherited credential or
    # property override can redirect the copy to another account or mode.
    # Snap command links resolve to /usr/bin/snap, not to a path under /snap/.
    real = os.path.realpath(GCLOUD)
    require(os.path.isfile(real) and not real.startswith('/snap/') and real != '/usr/bin/snap',
            'apt_gcloud_required')
    env = {**SYSTEM_ENV, 'CLOUDSDK_CORE_DISABLE_PROMPTS': '1',
           'CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED': 'False'}
    env.update({key: os.environ[key] for key in ('HOME', 'CLOUDSDK_CONFIG') if key in os.environ})
    return env


def gcloud_copy(source, target, *, create=False):
    argv = [GCLOUD, 'storage', 'cp', str(source), str(target)]
    if create:
        # Generation 0 means "only if no live object has this name".
        argv.insert(3, '--if-generation-match=0')
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, env=gcloud_env(), timeout=3600)
    if result.returncode:
        # Child output is never relayed; only a well-known HTTP status becomes a code.
        # ponytail: best effort on gcloud's message text; unknown text keeps the generic code.
        status = re.search(rb'(?:HTTPError |not found: )(\d{3})', result.stderr)
        codes = {b'412': 'gcs_object_already_exists', b'403': 'gcs_permission_denied',
                 b'404': 'gcs_object_not_found'}
        raise BackupError(codes.get(status[1] if status else b'', 'external_command_failed'))


def run_system(argv):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, env=SYSTEM_ENV, timeout=60)
    require(result.returncode == 0, 'external_command_failed')
    return result.stdout.decode()


def encrypted_volume(directory):
    # Same LUKS2 evidence that host enrollment requires for the data mount.
    row = json.loads(run_system(['findmnt', '--json', '--target', str(directory),
                                 '--output', 'SOURCE']))['filesystems'][0]
    require(row['source'].startswith('/dev/mapper/') and
            re.search(r'type:\s+LUKS2', run_system(['cryptsetup', 'status', row['source']])),
            'encrypted_identity_volume_required')


def private_identity(path):
    path = transfer.clean_path(path)
    transfer.file_info(path, private=True)
    parent = path.parent.stat()
    require(parent.st_uid == os.geteuid() and stat.S_IMODE(parent.st_mode) == 0o700,
            'private_identity_directory_required')
    encrypted_volume(path.parent)
    return path


def shred(path):
    # Overwrite is best effort on virtual disks; removal must always happen.
    try:
        subprocess.run(['shred', '--remove', str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=SYSTEM_ENV, timeout=300)
    except (OSError, subprocess.SubprocessError):
        pass
    if os.path.lexists(path):
        os.unlink(path)


def gcs_upload(source, bucket, kind, recipient, max_age=3600):
    source = transfer.clean_path(source)
    manifest, verified = transfer.verify(source, ROLE, max_age)
    require(isinstance(recipient, str) and RECIPIENT.fullmatch(recipient), 'age_x25519_recipient_required')
    # Unique keys are create-only; no existing object is targeted or replaced.
    destination = remote_root(bucket, kind) + '/' + uuid.uuid4().hex
    # Plaintext ZIP stays next to the snapshot on the encrypted data volume.
    with tempfile.TemporaryDirectory(prefix='map-gcs-encrypted-', dir=source.parent) as work:
        stage = Path(work).resolve()
        stage.chmod(0o700)
        transfer.disk_guard(stage, 4 * (sum(item['bytes'] for item in manifest['files']) + transfer.MAX_JSON))
        transfer.pack(source, stage / 'backup.zip', ROLE, max_age)
        transfer.age_encrypt(stage / 'backup.zip', stage / 'backup.age', recipient)
        ciphertext = stage / 'backup.age'
        transport = {'schema': SCHEMA, 'role': ROLE, 'source_created_at': verified['source_created_at'],
                     'endpoint': 'gs://' + bucket, 'encryption': 'age-x25519',
                     'manifest_sha256': verified['manifest_sha256'],
                     'ciphertext_sha256': transfer.digest(ciphertext), 'ciphertext_bytes': ciphertext.stat().st_size}
        transfer.write_json(stage / 'transport.json', transport)
        gcloud_copy(ciphertext, destination + '/backup.age', create=True)
        gcloud_copy(destination + '/backup.age', stage / 'readback.age')
        require(transfer.digest(stage / 'readback.age') == transport['ciphertext_sha256'], 'remote_checksum_mismatch')
        # transport.json is published last; its SHA256 is the restore trust anchor.
        gcloud_copy(stage / 'transport.json', destination + '/transport.json', create=True)
        gcloud_copy(destination + '/transport.json', stage / 'readback.json')
        transport_sha = transfer.digest(stage / 'transport.json')
        require(transfer.digest(stage / 'readback.json') == transport_sha, 'remote_manifest_checksum_mismatch')
    return {'role': ROLE, 'transport': TRANSPORT, 'remote': destination, 'transport_sha256': transport_sha,
            'gcs_remote_verified': True, 'database_restore_executed': False,
            'source_created_at': verified['source_created_at']}


def gcs_download(remote, target, identity, transport_sha, max_age=RETENTION_DAYS * 86400):
    # The default freshness limit admits every backup the bucket still keeps.
    # Once the identity passes its own checks, every later outcome shreds it,
    # including input errors; a rejected identity file is left untouched.
    identity = private_identity(identity)
    try:
        target = transfer.fresh_target(target)
        bucket, _ = parse_remote(remote)
        require(isinstance(transport_sha, str) and transfer.SHA.fullmatch(transport_sha),
                'trusted_transport_sha256_required')
        with tempfile.TemporaryDirectory(prefix='.map-gcs-download-', dir=target.parent) as work:
            stage = Path(work)
            stage.chmod(0o700)
            gcloud_copy(remote + '/transport.json', stage / 'transport.json')
            require(transfer.digest(stage / 'transport.json') == transport_sha, 'transport_trust_anchor_mismatch')
            meta = transfer.read_json(stage / 'transport.json')
            require(meta.get('schema') == SCHEMA and meta.get('role') == ROLE and
                    meta.get('endpoint') == 'gs://' + bucket and meta.get('encryption') == 'age-x25519',
                    'transport_contract_mismatch')
            transfer.age_seconds(meta.get('source_created_at'), max_age)
            require(type(meta.get('ciphertext_bytes')) is int and
                    0 < meta['ciphertext_bytes'] <= transfer.MAX_BACKUP_BYTES + 32 * 1024 ** 3 and
                    transfer.SHA.fullmatch(meta.get('ciphertext_sha256', '')) and
                    transfer.SHA.fullmatch(meta.get('manifest_sha256', '')), 'invalid_transport_payload')
            transfer.disk_guard(stage, 3 * meta['ciphertext_bytes'] + transfer.MAX_JSON)
            gcloud_copy(remote + '/backup.age', stage / 'backup.age')
            require(transfer.file_info(stage / 'backup.age').st_size == meta['ciphertext_bytes'] and
                    transfer.digest(stage / 'backup.age') == meta['ciphertext_sha256'], 'download_checksum_mismatch')
            transfer.age_decrypt(stage / 'backup.age', stage / 'backup.zip', identity)
            transfer.unpack(stage / 'backup.zip', stage / 'snapshot')
            require(transfer.digest(stage / 'snapshot' / 'manifest.json') == meta['manifest_sha256'],
                    'plaintext_manifest_checksum_mismatch')
            _, recovered = transfer.verify(stage / 'snapshot', ROLE, max_age)
            require(recovered['source_created_at'] == meta['source_created_at'], 'transport_source_timestamp_mismatch')
            transfer.publish(stage / 'snapshot', target)
    finally:
        shred(identity)
    result = transfer.verify(target, ROLE, max_age)[1]
    result.update({'transport': TRANSPORT, 'gcs_remote_verified': True, 'database_restore_executed': False})
    return result


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('upload', 'download'))
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--bucket')
    parser.add_argument('--kind', choices=KINDS)
    parser.add_argument('--age-recipient')
    parser.add_argument('--remote')
    parser.add_argument('--target', type=Path)
    parser.add_argument('--age-identity-file', type=Path)
    parser.add_argument('--transport-sha256')
    parser.add_argument('--max-age-seconds', type=int,
                        help='freshness limit; default 3600 for upload, the bucket retention for download')
    args = parser.parse_args(argv)
    limit = {} if args.max_age_seconds is None else {'max_age': args.max_age_seconds}
    started = time.monotonic()
    try:
        if args.action == 'upload':
            require(args.snapshot and args.bucket and args.kind and args.age_recipient, 'upload_inputs_required')
            result = gcs_upload(args.snapshot, args.bucket, args.kind, args.age_recipient, **limit)
        else:
            require(args.remote and args.target and args.age_identity_file, 'download_inputs_required')
            result = gcs_download(args.remote, args.target, args.age_identity_file, args.transport_sha256, **limit)
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
