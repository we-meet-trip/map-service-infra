#!/usr/bin/env python3
"""Local GCP production backup writer to the private GCS backup bucket.

Same order as ncp-production-backup.py execute(), whose upload and receipt are
bound to the NCP transport and cannot be called: host check, deploy.lock then
backup.lock, pinned source, pending guard, closed helper output, closed
contract, snapshot, encrypted GCS upload, own receipt, plaintext cleanup and
status. Only this checkout's scripts are imported and none of them is changed.
No database, older backup, remote object, cloud resource or service is deleted.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import uuid

SCRIPTS = Path(__file__).resolve().parent
IMPORTS = ('gcs-production-backup.py', 'gcs_backup_transport.py', 'ncp-production-backup.py',
           'ncp-bootstrap-backup.py', 'ncp-bootstrap-host.py', 'pg_backup.py', 'redis_backup.py', 'backup_job.py')


def trusted_source(scripts):
    # A root timer must not import code that a non-root account could change.
    for path in (scripts, *scripts.parents):
        item = path.lstat()
        if not stat.S_ISDIR(item.st_mode) or item.st_uid != 0 or item.st_mode & 0o022:
            raise RuntimeError('root_source_ownership_required')
    for name in IMPORTS:
        item = (scripts / name).lstat()
        if not stat.S_ISREG(item.st_mode) or item.st_uid != 0 or item.st_mode & 0o022 or item.st_nlink != 1:
            raise RuntimeError('root_source_ownership_required')


if __name__ == '__main__':
    try:
        trusted_source(SCRIPTS)
    except Exception:
        print(json.dumps({'code': 'BACKUP_FAILED', 'success': False, 'reason': 'root_source_ownership_required'}),
              file=sys.stderr)
        print('MAP_BACKUP_RESULT=FAILED', flush=True)
        raise SystemExit(1)

sys.path.insert(0, str(SCRIPTS))
import backup_job
import gcs_backup_transport as transport


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


ncp = module('gcs_ncp_production_backup', 'ncp-production-backup.py')
transfer = ncp.transfer
DATA = ncp.DATA
CONFIG = Path('/etc/map-ops-gcp/backup-gcs.json')
KINDS = transport.KINDS
OWNER = 'map-prod-backup-gcs-v1'
RESULT = 'MAP_BACKUP_RESULT'
ADMIN_PROJECT, ADMIN_SERVICE = 'map-admin-prod', 'admin-control-db'
SOURCES = {'postgres': {'user', 'database'}, 'redis': set(), 'admin': {'user', 'database'}}


def require(ok, code):
    if not ok:
        raise transfer.BackupError(code)


def configuration(path=CONFIG):
    value = transfer.read_json(path, private=True)
    required = {'schema_version', 'enrollment_sha256', 'bucket', 'age_recipient', 'postgres', 'redis'}
    # The admin control DB pin is optional until that stack is installed.
    require(required <= set(value) <= required | {'admin'}, 'invalid_backup_configuration')
    require(type(value['schema_version']) is int and value['schema_version'] == 1, 'production_configuration_required')
    require(isinstance(value['enrollment_sha256'], str) and ncp.ID.fullmatch(value['enrollment_sha256']),
            'enrollment_pin_required')
    require(isinstance(value['bucket'], str) and transport.BUCKET.fullmatch(value['bucket']), 'invalid_gcs_bucket')
    require(isinstance(value['age_recipient'], str) and transport.RECIPIENT.fullmatch(value['age_recipient']),
            'age_recipient_required')
    for name, extra in SOURCES.items():
        if name not in value:
            continue
        entry = value[name]
        require(isinstance(entry, dict) and set(entry) == {'container_id', 'image_id'} | extra, 'invalid_source_contract')
        require(isinstance(entry['container_id'], str) and ncp.ID.fullmatch(entry['container_id']) and
                isinstance(entry['image_id'], str) and ncp.IMAGE.fullmatch(entry['image_id']),
                'exact_source_identity_required')
        require(all(isinstance(entry[key], str) and ncp.IDENTIFIER.fullmatch(entry[key]) for key in extra),
                'database_identifiers_required')
    ids = [value[name]['container_id'] for name in SOURCES if name in value]
    require(len(set(ids)) == len(ids), 'source_identity_reuse')
    require(value['postgres']['database'] == 'map_prod', 'production_database_required')
    return value


class Backend(ncp.Backend):
    """NCP host, source and collect checks; admin control DB and GCS upload added."""

    def source(self, config, kind):
        if kind != 'admin':
            return super().source(config, 'postgres' if kind == 'pg' else 'redis')
        pin = config['admin']
        fmt = ('{"Id":{{json .Id}},"Image":{{json .Image}},"State":{"Running":{{json .State.Running}}},'
               '"Config":{"Labels":{"com.docker.compose.project":{{json (index .Config.Labels "com.docker.compose.project")}},'
               '"com.docker.compose.service":{{json (index .Config.Labels "com.docker.compose.service")}}}}}')
        item = json.loads(ncp.redis.run(['docker', 'inspect', '--format', fmt, pin['container_id']]))
        labels = item['Config']['Labels']
        require(item['Id'] == pin['container_id'] and item['Image'] == pin['image_id'] and
                item['State']['Running'] is True and labels.get('com.docker.compose.project') == ADMIN_PROJECT and
                labels.get('com.docker.compose.service') == ADMIN_SERVICE, 'production_source_drift')
        return item

    def collect(self, kind, config, item, directory):
        if kind == 'admin':
            # The same closed pg_dump bundle, taken from the pinned control DB.
            return super().collect('pg', {'postgres': config['admin']}, item, directory)
        return super().collect(kind, config, item, directory)

    def upload(self, snapshot, config, kind):
        return transport.gcs_upload(snapshot, config['bucket'], kind, config['age_recipient'])


def write_status(state, kind, code, *, receipt=None):
    state = transfer.clean_path(state)
    metadata = state.stat()
    require(state.is_dir() and metadata.st_uid == os.geteuid() and
            stat.S_IMODE(metadata.st_mode) == 0o700, 'private_state_directory_required')
    path = state / (kind + '-backup-gcs-status.json')
    try:
        old = transfer.read_json(path, private=True) if path.exists() else {}
    except Exception:
        old = {}  # A damaged status file must not block the next backup.
    now = dt.datetime.now(dt.timezone.utc)
    value = {'code': code, 'success': receipt is not None, 'last_attempt_at': now.isoformat(),
             'last_success_at': now.isoformat() if receipt else old.get('last_success_at'),
             'snapshot_at': receipt['source_created_at'] if receipt else old.get('snapshot_at'),
             'transport_sha256': receipt['transport_sha256'] if receipt else old.get('transport_sha256'),
             'remote': receipt['remote'] if receipt else old.get('remote')}
    try:
        value['rpo_1h_overdue'] = (now - transfer.utc(value['snapshot_at'])).total_seconds() > 3600
    except Exception:
        value['rpo_1h_overdue'] = True
    ncp.host.atomic_json(path, value)
    return value


def retain_receipt(state, kind, job_id, receipt):
    directory = ncp.private_directory(state / 'backup-receipts-gcs')
    record = {'schema_version': 1, 'owner': OWNER, 'kind': kind, 'job_id': job_id, 'receipt': receipt}
    transfer.write_json(directory / (kind + '-' + job_id + '.json'), record)
    old = []
    for path in directory.glob(kind + '-*.json'):
        require(re.fullmatch(kind + r'-[a-f0-9]{32}\.json', path.name), 'unknown_backup_receipt_requires_review')
        item = transfer.read_json(path, private=True)
        require(set(item) == set(record) and item['schema_version'] == 1 and item['owner'] == OWNER and
                item['kind'] == kind and path.name == kind + '-' + item['job_id'] + '.json' and
                item['receipt'].get('gcs_remote_verified') is True, 'unknown_backup_receipt_requires_review')
        old.append((transfer.utc(item['receipt']['source_created_at']), path))
    # Two days at a 30-minute cadence; the bucket lifecycle owns remote expiry.
    for _, path in sorted(old, key=lambda item: (item[0], item[1].name))[:-96]:
        transfer.file_info(path, private=True)
        path.unlink()


def execute(kind, config, *, data=DATA, backend=None, lock_wait=backup_job.LOCK_WAIT_SECONDS):
    require(kind in KINDS, 'invalid_backup_kind')
    # An enabled admin timer without its pin fails loudly instead of skipping.
    require(kind != 'admin' or 'admin' in config, 'admin_source_not_configured')
    backend = backend or Backend()
    backend.verify_host(config)  # No state is created on an unenrolled host.
    state = ncp.private_directory(data / 'deploy')
    # Same acquisition order as the receiver and the NCP runner.
    with ExitStack() as stack:
        for filename in ('deploy.lock', 'backup.lock'):
            lock = stack.enter_context(ncp.lock_file(state / filename))
            if not backup_job.acquire(lock, lock_wait):
                return write_status(state, kind, 'LOCK_BUSY')
        backend.verify_host(config)
        item = backend.source(config, kind)
        # Separate from the NCP job directory and split per kind, so failed
        # jobs of one runner or kind never block another's pending guard.
        backups = ncp.private_directory(ncp.private_directory(data / 'backups-gcs') / kind)
        ncp.pending_guard(backups)
        job_id = uuid.uuid4().hex
        directory = ncp.private_directory(backups / (kind + '-' + job_id))
        # Marker owner matches what remove_successful_plaintext() accepts.
        transfer.write_json(directory / 'job.json', {'schema_version': 1, 'owner': 'map-prod-backup-v1',
                                                     'kind': kind, 'job_id': job_id})
        helper = backend.collect(kind, config, item, directory)
        contract = ncp.closed_contract('redis' if kind == 'redis' else 'pg', helper, item['Id'])
        snapshot = directory / 'closed'
        transfer.snapshot(contract, 'prod', snapshot)
        receipt = backend.upload(snapshot, config, kind)
        require(receipt.get('gcs_remote_verified') is True and receipt.get('transport') == transport.TRANSPORT and
                receipt.get('role') == 'prod' and ncp.ID.fullmatch(receipt.get('transport_sha256', '')),
                'remote_backup_evidence_incomplete')
        require(receipt.get('source_created_at') == transfer.read_json(contract)['created_at'] and
                re.fullmatch(re.escape(transport.remote_root(config['bucket'], kind) + '/') + r'[a-f0-9]{32}',
                             receipt.get('remote', '')), 'remote_backup_identity_mismatch')
        retain_receipt(state, kind, job_id, receipt)
        ncp.remove_successful_plaintext(directory, kind, job_id)
        return write_status(state, kind, 'COMPLETE', receipt=receipt)


def cancelled(signum, frame):
    raise transfer.BackupError('operation_cancelled')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=KINDS)
    args = parser.parse_args(argv)
    os.umask(0o077)
    # systemd's stop timeout sends SIGTERM; cleanup blocks and the result line still run.
    signal.signal(signal.SIGTERM, cancelled)
    try:
        require(os.geteuid() == 0, 'root_runner_required')
        result = execute(args.kind, configuration())
    except Exception as error:
        try:
            write_status(DATA / 'deploy', args.kind, 'BACKUP_FAILED')
        except Exception:
            pass
        known = isinstance(error, (transfer.BackupError, transport.BackupError))
        print(json.dumps({'code': 'BACKUP_FAILED', 'success': False,
                          'reason': str(error) if known else 'operation_failed'}), file=sys.stderr)
        print(RESULT + '=FAILED', flush=True)
        return 1
    # remote and transport_sha256 are the download trust anchor; the journal
    # keeps a copy outside the VM.
    print(json.dumps({key: result[key] for key in ('code', 'success', 'rpo_1h_overdue', 'remote', 'transport_sha256')}))
    if result['success']:
        print(RESULT + '=COMPLETE kind=' + args.kind, flush=True)
        return 0
    # A deferral while the last success is under an hour old is not a failure.
    if result['code'] == 'LOCK_BUSY' and not result['rpo_1h_overdue']:
        return 0
    print(RESULT + '=FAILED', flush=True)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
