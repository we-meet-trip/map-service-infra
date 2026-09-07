#!/usr/bin/env python3
"""Local synthetic Redis RDB/TTL restore acceptance; no remote service or upload."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid
from unittest.mock import patch

import redis_backup as backup


def main():
    image = backup.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'redis:7-alpine'])
    token = uuid.uuid4().hex
    name = 'map-redis-backup-fixture-' + token
    own = False
    with tempfile.TemporaryDirectory(prefix='map-redis-synthetic-') as temporary:
        folder = Path(temporary)
        received = folder / 'verified-copy'
        received.mkdir(mode=0o700)
        try:
            backup.run(['docker', 'run', '-d', '--name', name, '--label', 'map.acceptance.redis-backup=' + token,
                        '--pull', 'never', '--network', 'none', '--memory', '128m', '--memory-swap', '128m',
                        '--cpus', '0.25', '--pids-limit', '64', '--read-only', '--tmpfs', '/data:rw,size=67108864',
                        image, 'redis-server', '--appendonly', 'yes', '--save', '', '--dir', '/data', '--databases', '32'])
            own = True
            redis = backup.Redis(name)
            for _ in range(100):
                try:
                    if redis.call('PING') == 'PONG': break
                except backup.BackupError: pass
                time.sleep(.1)
            # This fresh isolated source contains synthetic fixtures only.
            redis.call('SET', 'fixture-string', 'fixture-value')
            redis.call('-n', '2', 'HSET', 'fixture-hash', 'field', 'value')
            redis.call('-n', '4', 'XADD', 'fixture-stream', '*', 'field', 'value')
            redis.call('-n', '19', 'SET', 'fixture-high-db', 'fixture-value')
            redis.call('-n', '6', 'SET', 'fixture-expiring', 'fixture-value', 'EX', '10')
            item = json.loads(backup.run(['docker', 'inspect', name]))[0]
            original_snapshot = backup.snapshot_file
            def delay_until_expired(*args):
                original_snapshot(*args)
                time.sleep(10.2)
            uploaded = []
            def local_upload(files, remote):
                for source in files:
                    destination = received / source.name
                    if destination.exists(): raise AssertionError('fixture collision')
                    shutil.copyfile(source, destination)
                    assert backup.pg_backup.checksum(source) == backup.pg_backup.checksum(destination)
                    uploaded.append(source.suffix)
            with patch.dict(os.environ, {'REDIS_BACKUP_DIR': str(folder / 'source'), 'REDIS_BACKUP_REMOTE': 'gs://synthetic.invalid/test/redis-v1'}), patch.object(backup.pg_backup, 'upload', side_effect=local_upload), patch.object(backup, 'snapshot_file', side_effect=delay_until_expired), contextlib.redirect_stdout(io.StringIO()):
                manifest = backup.backup('test', item=item)
            meta = json.loads(manifest.read_text())
            assert uploaded == ['.rdb', '.json']
            assert meta['snapshot_keyspace'].get('db6') == {'keys': 1, 'expires': 1}, json.dumps({'before': meta['observed_source_keyspace_before'], 'snapshot': meta['snapshot_keyspace'], 'primary': meta['primary_restore']['keyspace']})
            assert 'db6' not in meta['primary_restore']['keyspace']
            assert sum(v['keys'] for v in meta['snapshot_keyspace'].values()) == 5
            # A later restore reads the transferred bytes, not the source file.
            restored = backup.restore_counts(received / meta['files'][0]['name'], image, meta['image_platform'], replica=True, databases=meta['database_count'])
            assert restored['keyspace'] == meta['snapshot_keyspace']
            result = {'status': 'PASS', 'scope': 'local_new_synthetic_containers_only',
                      'source_image_id': image, 'platform': meta['image_platform'], 'redis_version': meta['redis_version'],
                      'database_count': meta['database_count'],
                      'snapshot_keyspace': meta['snapshot_keyspace'], 'primary_keyspace': meta['primary_restore']['keyspace'],
                      'expired_key_retained_in_replica_count': True, 'expired_key_absent_in_primary': True,
                      'copy_checksum_verified': True, 'restored_from_copied_bytes': True,
                      'remote_provider_calls': 0, 'bgsave_seconds': meta['bgsave_seconds'],
                      'restores_seconds': meta['primary_restore']['elapsed_seconds'] + meta['snapshot_count_restore_seconds'] + restored['elapsed_seconds']}
        finally:
            if own:
                item = json.loads(backup.run(['docker', 'inspect', name]))[0]
                assert item['Config']['Labels'].get('map.acceptance.redis-backup') == token
                backup.run(['docker', 'stop', '--time', '5', name])
                backup.run(['docker', 'rm', name])
        result['fixture_removed'] = True
        print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
