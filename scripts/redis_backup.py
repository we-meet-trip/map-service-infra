#!/usr/bin/env python3
"""Consistent Redis RDB backups, off-host checksums, and isolated restore checks.

Never scans/prints keys or values, removes old backups, or mounts a serving volume
in a restore container. The exact source image must already exist locally.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

import pg_backup

ROOT = Path(__file__).resolve().parent.parent
LABEL = 'map.acceptance.redis-restore'
IMAGE = re.compile(r'sha256:[a-f0-9]{64}')


class BackupError(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise BackupError(code)


def run(args, *, data=None, timeout=30):
    p = subprocess.run(args, input=data, capture_output=True, text=True, timeout=timeout)
    require(p.returncode == 0, 'command_failed')
    return p.stdout.strip()


def parse_info(raw):
    return dict(line.split(':', 1) for line in raw.splitlines() if ':' in line and not line.startswith('#'))


def keyspace(raw):
    result = {}
    for db, value in parse_info(raw).items():
        require(re.fullmatch(r'db[0-9]{1,5}', db), 'unexpected_keyspace_field')
        fields = dict(piece.split('=', 1) for piece in value.split(','))
        keys, expires = int(fields['keys']), int(fields['expires'])
        require(0 <= expires <= keys, 'invalid_keyspace_counts')
        result[db] = {'keys': keys, 'expires': expires}
    return result


class Redis:
    def __init__(self, container):
        self.container = container

    def call(self, *args):
        # Credentials remain in the container environment, absent from argv/logs.
        return run(['docker', 'exec', self.container, 'sh', '-c',
                    'if [ -n "${REDIS_PASSWORD:-}" ]; then export REDISCLI_AUTH="$REDIS_PASSWORD"; else unset REDISCLI_AUTH; fi; exec redis-cli --raw -e "$@"', 'sh', *args])

    def info(self, section):
        return parse_info(self.call('INFO', section))


def source_container(environment):
    env = ROOT / ('.env.test' if environment == 'test' else '.env')
    require(env.is_file() and not env.is_symlink(), 'explicit_environment_required')
    cmd = ['docker', 'compose', '--env-file', str(env), '-f', str(ROOT / 'docker-compose.yml')]
    if environment == 'test':
        cmd += ['-f', str(ROOT / 'docker-compose.test.yml')]
    identity = run(cmd + ['ps', '-q', 'redis'])
    require(re.fullmatch('[a-f0-9]{64}', identity), 'one_running_redis_required')
    item = json.loads(run(['docker', 'inspect', identity]))[0]
    labels = item['Config'].get('Labels') or {}
    require(labels.get('com.docker.compose.service') == 'redis' and
            labels.get('com.docker.compose.project') == ('map-test' if environment == 'test' else 'map'),
            'redis_environment_mismatch')
    require(item['State']['Running'] and IMAGE.fullmatch(item['Image']), 'redis_identity_required')
    return item


def resource_guard(item, memory, persistence, available, disk_free):
    used, rss = int(memory['used_memory']), int(memory['used_memory_rss'])
    require(used > 0 and rss > 0, 'memory_measurement_required')
    require(used <= 16 * 1024**2, 'review_restore_capacity_before_large_backup')
    require(persistence.get('rdb_bgsave_in_progress') == '0' and
            persistence.get('aof_rewrite_in_progress') == '0', 'persistence_busy')
    require(persistence.get('aof_enabled') != '1' or persistence.get('aof_last_write_status') == 'ok', 'aof_write_unhealthy')
    reserve = max(64 * 1024**2, 2 * max(used, rss))
    limit = item['HostConfig']['Memory']
    require(not limit or rss + reserve < limit, 'redis_fork_memory_limit')
    require(available >= reserve + 512 * 1024**2, 'host_fork_memory_low')
    require(disk_free >= max(256 * 1024**2, 4 * used), 'snapshot_disk_low')
    return {'used_memory_bytes': used, 'rss_bytes': rss, 'fork_reserve_bytes': reserve,
            'host_available_bytes': available, 'disk_free_bytes': disk_free,
            'container_memory_limit_bytes': limit}


def wait_snapshot(redis, before, timeout=60):
    deadline = time.monotonic() + timeout
    # LASTSAVE has one-second precision; start in a later server clock second.
    while int(redis.call('TIME').splitlines()[0]) <= before:
        require(time.monotonic() < deadline, 'server_clock_not_advancing')
        time.sleep(.2)
    require(redis.call('BGSAVE') == 'Background saving started', 'bgsave_not_started')
    while time.monotonic() < deadline:
        state = redis.info('persistence')
        if state['rdb_bgsave_in_progress'] == '0':
            require(state['rdb_last_bgsave_status'] == 'ok', 'bgsave_failed')
            if int(state['rdb_last_save_time']) > before:
                return state
        time.sleep(.2)
    raise BackupError('bgsave_timeout')


def snapshot_file(redis, output, max_bytes):
    # Redis atomically renames a completed RDB. Reading its open file descriptor
    # keeps one consistent generation even if the automatic saver runs again.
    require(not output.exists() and not output.is_symlink(), 'snapshot_output_exists')
    directory = redis.call('CONFIG', 'GET', 'dir').splitlines()
    name = redis.call('CONFIG', 'GET', 'dbfilename').splitlines()
    require(len(directory) == 2 and directory[0] == 'dir' and directory[1] == '/data', 'unexpected_redis_data_directory')
    require(len(name) == 2 and name[0] == 'dbfilename' and re.fullmatch(r'[A-Za-z0-9_.-]+\.rdb', name[1]), 'unexpected_rdb_filename')
    partial = output.with_suffix('.part')
    process = subprocess.Popen(['docker', 'exec', redis.container, 'cat', '/data/' + name[1]],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        with partial.open('xb') as target:
            total = 0
            for block in iter(lambda: process.stdout.read(1024**2), b''):
                total += len(block)
                require(total <= max_bytes, 'snapshot_size_limit')
                target.write(block)
        process.stdout.close()
        require(process.wait(timeout=30) == 0, 'snapshot_copy_failed')
        with partial.open('rb') as source:
            require(re.fullmatch(b'REDIS[0-9]{4}', source.read(9)), 'invalid_rdb_header')
        partial.replace(output)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        partial.unlink(missing_ok=True)  # Only this attempt's unique partial.


def restore_counts(path, image, platform, *, replica=False, databases=16):
    require(IMAGE.fullmatch(image), 'immutable_restore_image_required')
    require(platform in ('linux/amd64', 'linux/arm64'), 'explicit_restore_platform_required')
    require(type(databases) is int and 1 <= databases <= 1024, 'bounded_database_count_required')
    name = 'map-redis-restore-' + uuid.uuid4().hex
    started = time.monotonic()
    created = False
    try:
        # /data is new tmpfs, not the image's anonymous volume or any host path.
        script = 'while [ ! -f /data/ready ]; do sleep 0.1; done; redis-check-rdb /data/dump.rdb >/tmp/check.log 2>&1 && exec redis-server --bind 127.0.0.1 --save "" --appendonly no --dir /data --dbfilename dump.rdb --logfile /tmp/redis.log'
        script += ' --databases ' + str(databases)
        if replica:
            # Disconnected loopback replica preserves expired entries while
            # loading, enabling reproducible per-DB counts of immutable RDB bytes.
            script += ' --replicaof 127.0.0.1 1'
        run(['docker', 'run', '-d', '-i', '--name', name, '--label', LABEL + '=true',
             '--pull', 'never', '--platform', platform, '--network', 'none', '--read-only', '--memory', '128m',
             '--memory-swap', '128m', '--cpus', '0.5', '--pids-limit', '64',
             '--security-opt', 'no-new-privileges', '--tmpfs', '/data:rw,size=67108864,mode=0700',
             '--tmpfs', '/tmp:rw,size=16777216', '--entrypoint', 'sh', image, '-c', script])
        created = True
        with path.open('rb') as stream:
            p = subprocess.run(['docker', 'exec', '-i', name, 'sh', '-c',
                                'cat > /data/dump.rdb && touch /data/ready'], stdin=stream,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        require(p.returncode == 0, 'restore_transfer_failed')
        redis = Redis(name)
        for _ in range(100):
            try:
                if redis.call('PING') == 'PONG':
                    break
            except BackupError:
                pass
            time.sleep(.1)
        else:
            raise BackupError('isolated_restore_timeout')
        server = redis.info('server')
        info = redis.info('replication')
        require(info.get('role') == ('slave' if replica else 'master'), 'restore_role_mismatch')
        counts = keyspace(redis.call('INFO', 'keyspace'))
        return {'keyspace': counts, 'redis_version': server['redis_version'],
                'elapsed_seconds': round(time.monotonic() - started, 3), 'rdb_check': 'PASS'}
    finally:
        if created:
            own = json.loads(run(['docker', 'inspect', name]))[0]
            require(own['Config']['Labels'].get(LABEL) == 'true', 'restore_cleanup_owner_mismatch')
            run(['docker', 'stop', '--time', '5', name])
            run(['docker', 'rm', name])  # Only the UUID fixture; no existing volumes.


def compare_counts(snapshot, restored):
    require(set(restored) <= set(snapshot), 'unexpected_restored_database')
    for db, original in snapshot.items():
        current = restored.get(db, {'keys': 0, 'expires': 0})
        require(current['keys'] <= original['keys'] and current['expires'] <= original['expires'] and
                current['keys'] - current['expires'] == original['keys'] - original['expires'],
                'restored_key_count_mismatch')


def backup(environment, *, item=None):
    os.umask(0o077)
    remote = os.environ['REDIS_BACKUP_REMOTE']
    require(remote.startswith('gs://') and remote.rstrip('/').endswith('/' + environment + '/redis-v1'),
            'separate_environment_gcs_redis_prefix_required')
    item = item or source_container(environment)
    redis = Redis(item['Id'])
    platform = run(['docker', 'image', 'inspect', '--format', '{{.Os}}/{{.Architecture}}', item['Image']])
    database_config = redis.call('CONFIG', 'GET', 'databases').splitlines()
    require(len(database_config) == 2 and database_config[0] == 'databases', 'database_count_required')
    databases = int(database_config[1])
    require(1 <= databases <= 1024, 'bounded_database_count_required')
    directory = Path(os.environ['REDIS_BACKUP_DIR'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(directory.is_dir() and not directory.is_symlink(), 'private_backup_directory_required')
    directory.chmod(0o700)
    memory, persistence = redis.info('memory'), redis.info('persistence')
    host_memory = {line.split(':')[0]: int(line.split()[1]) * 1024
                   for line in run(['docker', 'exec', redis.container, 'cat', '/proc/meminfo']).splitlines()}
    resources = resource_guard(item, memory, persistence, host_memory['MemAvailable'], shutil.disk_usage(directory).free)
    server = redis.info('server')
    before_keys = keyspace(redis.call('INFO', 'keyspace'))
    saved = wait_snapshot(redis, int(persistence['rdb_last_save_time']))
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    stem = f'map-redis-{environment}-{stamp}-{uuid.uuid4().hex}'
    rdb, manifest = directory / (stem + '.rdb'), directory / (stem + '.manifest.json')
    snapshot_file(redis, rdb, max(16 * 1024**2, 4 * resources['used_memory_bytes']))
    snapshot = restore_counts(rdb, item['Image'], platform, replica=True, databases=databases)
    restored = restore_counts(rdb, item['Image'], platform, databases=databases)
    compare_counts(snapshot['keyspace'], restored['keyspace'])
    require(server['redis_version'] == snapshot['redis_version'] == restored['redis_version'], 'restore_version_mismatch')
    meta = {'schema_version': 1, 'kind': 'redis-rdb', 'environment': environment,
            'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'snapshot_at': dt.datetime.fromtimestamp(int(saved['rdb_last_save_time']), dt.timezone.utc).isoformat(),
            'image_id': item['Image'], 'image_platform': platform, 'redis_version': server['redis_version'],
            'database_count': databases,
            'resources': resources, 'observed_source_keyspace_before': before_keys,
            'snapshot_keyspace': snapshot['keyspace'], 'snapshot_count_method': 'isolated_disconnected_replica_no_expiration',
            'primary_restore': restored, 'snapshot_count_restore_seconds': snapshot['elapsed_seconds'],
            'bgsave_seconds': int(saved['rdb_last_bgsave_time_sec']),
            'fork_usec_after': int(redis.info('stats')['latest_fork_usec']),
            'cow_bytes_after': int(saved['rdb_last_cow_size']),
            'files': [{'name': rdb.name, 'bytes': rdb.stat().st_size, 'sha256': pg_backup.checksum(rdb)}],
            'expiry_policy': 'absolute TTL preserved; expired keys omitted during normal primary restore'}
    # The manifest is published only after the RDB's remote bytes hash matches.
    partial = manifest.with_suffix('.part')
    with partial.open('x') as stream:
        stream.write(json.dumps(meta, indent=2) + '\n')
    pg_backup.upload([rdb], remote)
    partial.replace(manifest)
    pg_backup.upload([manifest], remote)
    print(json.dumps({'backup': 'complete', 'environment': environment, 'remote_verified': True,
                      'snapshot_at': meta['snapshot_at'], 'rdb_bytes': rdb.stat().st_size,
                      'snapshot_keyspace': snapshot['keyspace'], 'restore': 'PASS'}))
    return manifest


def main():
    def cancelled(signum, frame):
        raise BackupError('operation_cancelled')
    signal.signal(signal.SIGTERM, cancelled)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['backup', 'restore-check'])
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument('--test', action='store_true')
    choice.add_argument('--prod', action='store_true')
    p.add_argument('--manifest', type=Path)
    args = p.parse_args()
    environment = 'test' if args.test else 'prod'
    if args.action == 'backup':
        backup(environment)
    else:
        require(args.manifest is not None and args.manifest.is_file() and not args.manifest.is_symlink(), 'manifest_required')
        meta = json.loads(args.manifest.read_text())
        require(meta.get('schema_version') == 1 and meta.get('kind') == 'redis-rdb' and meta.get('environment') == environment, 'manifest_environment_mismatch')
        require(len(meta['files']) == 1, 'one_rdb_required')
        entry = meta['files'][0]
        require(re.fullmatch(r'map-redis-(test|prod)-[A-Za-z0-9]+-[a-f0-9]{32}\.rdb', entry['name']), 'invalid_rdb_name')
        path = args.manifest.parent / entry['name']
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == entry['bytes'] and pg_backup.checksum(path) == entry['sha256'], 'rdb_checksum_mismatch')
        snapshot = restore_counts(path, meta['image_id'], meta['image_platform'], replica=True, databases=meta['database_count'])
        require(snapshot['keyspace'] == meta['snapshot_keyspace'], 'snapshot_key_count_mismatch')
        restored = restore_counts(path, meta['image_id'], meta['image_platform'], databases=meta['database_count'])
        require(snapshot['redis_version'] == restored['redis_version'] == meta['redis_version'], 'restore_version_mismatch')
        compare_counts(meta['snapshot_keyspace'], restored['keyspace'])
        print(json.dumps({'restore': 'PASS', 'environment': environment, 'snapshot_keyspace': snapshot['keyspace'], 'primary_keyspace': restored['keyspace'], 'elapsed_seconds': snapshot['elapsed_seconds'] + restored['elapsed_seconds']}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__,
                          'code': str(error) if isinstance(error, BackupError) else 'operation_failed'}), file=sys.stderr)
        sys.exit(1)
