import contextlib
import datetime as dt
import threading
import time
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import redis_backup as backup
import backup_job as job


class RedisBackupTests(unittest.TestCase):
    def test_keyspace_reports_only_database_aggregate_counts(self):
        self.assertEqual(backup.keyspace('# Keyspace\r\ndb2:keys=5,expires=3,avg_ttl=1\r\n'), {'db2': {'keys': 5, 'expires': 3}})
        with self.assertRaises(backup.BackupError):
            backup.keyspace('unexpected:keys=1,expires=0')
        with self.assertRaises(backup.BackupError):
            backup.keyspace('db2:keys=1,expires=2')

    def test_only_expiring_keys_may_disappear_at_primary_restore(self):
        original = {'db0': {'keys': 4, 'expires': 2}, 'db6': {'keys': 1, 'expires': 1}}
        backup.compare_counts(original, {'db0': {'keys': 2, 'expires': 0}})
        for bad in ({'db0': {'keys': 1, 'expires': 0}}, {'db0': {'keys': 4, 'expires': 1}}, {'db9': {'keys': 1, 'expires': 0}}):
            with self.subTest(bad=bad), self.assertRaises(backup.BackupError):
                backup.compare_counts(original, bad)

    def test_fork_guard_rejects_busy_low_memory_and_unreviewed_growth(self):
        item = {'HostConfig': {'Memory': 128 * 1024**2}}
        mem = {'used_memory': 2 * 1024**2, 'used_memory_rss': 4 * 1024**2}
        persistence = {'rdb_bgsave_in_progress': '0', 'aof_rewrite_in_progress': '0', 'aof_enabled': '1', 'aof_last_write_status': 'ok'}
        backup.resource_guard(item, mem, persistence, 2 * 1024**3, 1024**3)
        bad_cases = [(item, mem, {**persistence, 'aof_rewrite_in_progress': '1'}, 2 * 1024**3, 1024**3),
                     (item, mem, persistence, 100 * 1024**2, 1024**3),
                     (item, mem, persistence, 2 * 1024**3, 100),
                     (item, {**mem, 'used_memory': 17 * 1024**2}, persistence, 2 * 1024**3, 1024**3)]
        for args in bad_cases:
            with self.assertRaises(backup.BackupError):
                backup.resource_guard(*args)

    def test_bgsave_must_complete_after_previous_generation(self):
        class Fake:
            def __init__(self): self.started = 0
            def call(self, *args):
                if args[0] == 'TIME': return '101\n0'
                self.started += 1
                return 'Background saving started'
            def info(self, section):
                return {'rdb_bgsave_in_progress': '0', 'rdb_last_bgsave_status': 'ok', 'rdb_last_save_time': '101'}
        fake = Fake()
        self.assertEqual(backup.wait_snapshot(fake, 100)['rdb_last_save_time'], '101')
        self.assertEqual(fake.started, 1)
        with patch.object(fake, 'info', return_value={'rdb_bgsave_in_progress': '0', 'rdb_last_bgsave_status': 'err'}):
            with self.assertRaisesRegex(backup.BackupError, 'bgsave_failed'):
                backup.wait_snapshot(fake, 100)

    def test_restore_never_accepts_mutable_image_or_unspecified_platform(self):
        with patch.object(backup, 'run') as run:
            with self.assertRaises(backup.BackupError):
                backup.restore_counts(Path('fixture.rdb'), 'redis:7-alpine', 'linux/amd64')
            with self.assertRaises(backup.BackupError):
                backup.restore_counts(Path('fixture.rdb'), 'sha256:' + 'a' * 64, 'other')
            run.assert_not_called()

    def test_destination_mismatch_is_rejected_before_any_source_call(self):
        with patch.dict(backup.os.environ, {'REDIS_BACKUP_REMOTE': 'gs://fixture/prod/redis-v1'}), patch.object(backup, 'source_container') as source:
            with self.assertRaisesRegex(backup.BackupError, 'separate_environment'):
                backup.backup('test')
            source.assert_not_called()

    def test_gcs_corruption_prevents_manifest_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / 'snapshot.rdb', Path(temporary) / 'snapshot.manifest.json']
            for path in paths: path.write_bytes(b'good')
            calls = []
            process = SimpleNamespace(stdout=io.BytesIO(b'evil'), wait=lambda: 0, poll=lambda: 0)
            with patch.object(backup.pg_backup, 'run', side_effect=lambda args, **kw: calls.append(args)), patch.object(backup.pg_backup.subprocess, 'Popen', return_value=process):
                with self.assertRaisesRegex(backup.pg_backup.BackupError, 'checksum mismatch'):
                    backup.pg_backup.upload(paths, 'gs://fixture/test/redis-v1')
            self.assertFalse(any('snapshot.manifest.json' in str(part) for call in calls for part in call))


class BackupTimerTests(unittest.TestCase):
    @contextlib.contextmanager
    def timer(self, root, wait=0.2):
        with patch.object(job, 'STATE', Path(root)), \
             patch.object(job, 'LOCK_WAIT_SECONDS', wait), \
             patch.object(job, 'configuration', return_value={'BACKUP_REMOTE': 'gs://fixture/test', 'BACKUP_DIR': '/tmp/fixture'}), \
             contextlib.redirect_stdout(io.StringIO()):
            yield

    def test_deployment_lock_defers_without_starting_child_and_preserves_success(self):
        with tempfile.TemporaryDirectory() as root, patch.object(job.subprocess, 'Popen') as child, self.timer(root):
            target = Path(root) / 'redis-backup-status.json'
            target.write_text(json.dumps({'last_success_at': '2026-09-01T00:00:00+00:00', 'snapshot_at': '2026-09-01T00:00:00+00:00'}))
            with (Path(root) / 'deploy.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # 이미 목표보다 오래된 사본을 두고 미룬 것은 끝난 실행이 아니다.
                self.assertEqual(job.entry('redis'), 1)
            child.assert_not_called()
            value = json.loads(target.read_text())
            self.assertEqual(value['code'], 'LOCK_BUSY')
            self.assertEqual(value['snapshot_at'], '2026-09-01T00:00:00+00:00')
            self.assertTrue(value['rpo_1h_overdue'])

    def test_fresh_copy_still_defers_quietly_while_a_deployment_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as root, patch.object(job.subprocess, 'Popen') as child, self.timer(root):
            target = Path(root) / 'redis-backup-status.json'
            fresh = dt.datetime.now(dt.timezone.utc).isoformat()
            target.write_text(json.dumps({'last_success_at': fresh, 'snapshot_at': fresh}))
            with (Path(root) / 'deploy.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(job.entry('redis'), 0)
            child.assert_not_called()
            self.assertFalse(json.loads(target.read_text())['rpo_1h_overdue'])

    def test_a_neighbour_that_keeps_retaking_the_lock_cannot_starve_the_copy(self):
        """공개 감독기는 짧은 주기마다 같은 배포 잠금을 다시 잡는다. 한 번
        시도하고 물러나면 그쪽이 계속 이겨 사본이 조용히 낡는다."""
        with tempfile.TemporaryDirectory() as root, patch.object(job.subprocess, 'Popen') as child, self.timer(root, wait=5):
            (Path(root) / 'redis-backup-status.json').write_text(
                json.dumps({'last_success_at': '2026-09-01T00:00:00+00:00', 'snapshot_at': '2026-09-01T00:00:00+00:00'}))
            released = threading.Event()

            def neighbour():
                for _ in range(3):
                    with (Path(root) / 'deploy.lock').open('a') as held:
                        fcntl.flock(held, fcntl.LOCK_EX)
                        time.sleep(0.2)
                    time.sleep(0.2)
                released.set()

            worker = threading.Thread(target=neighbour)
            worker.start()
            time.sleep(0.05)
            child.return_value = SimpleNamespace(
                pid=1, communicate=lambda timeout=None: ('{}', ''), returncode=0)
            job.entry('redis')
            worker.join(10)
            self.assertTrue(released.is_set())
            child.assert_called_once()

    def test_corrupt_status_does_not_prevent_next_success(self):
        with tempfile.TemporaryDirectory() as root, patch.object(job, 'STATE', Path(root)), contextlib.redirect_stdout(io.StringIO()):
            target = Path(root) / 'backup-status.json'
            target.write_text('broken')
            job.write_status('pg', {'success': True, 'code': 'COMPLETE'})
            self.assertTrue(json.loads(target.read_text())['success'])

    def test_child_timeout_terminates_its_group_and_records_failure(self):
        process = SimpleNamespace(pid=4242, communicate=None)
        with tempfile.TemporaryDirectory() as root, patch.object(job, 'STATE', Path(root)), patch.object(job, 'configuration', return_value={'BACKUP_REMOTE': 'gs://fixture/test', 'BACKUP_DIR': '/tmp/fixture'}), patch.object(job.subprocess, 'Popen', return_value=process), patch.object(job.os, 'killpg') as terminate, contextlib.redirect_stdout(io.StringIO()):
            from unittest.mock import Mock
            process.communicate = Mock(side_effect=[job.subprocess.TimeoutExpired('fixture', 540), ('', '')])
            self.assertEqual(job.entry('redis'), 1)
            terminate.assert_called_once_with(4242, job.signal.SIGTERM)
            self.assertEqual(json.loads((Path(root) / 'redis-backup-status.json').read_text())['code'], 'BACKUP_TIMEOUT')

    def test_configuration_never_sources_shell_and_rejects_duplicate_or_unknown_fields(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'backup.env'
            config.write_text('BACKUP_DIR=/tmp/fixture\nBACKUP_REMOTE=gs://fixture/test\n')
            config.chmod(0o600)
            # CI runs as an ordinary user; mock ownership, not parsing semantics.
            metadata = type('Stat', (), {'st_uid': 0, 'st_mode': 0o100600})()
            with patch.object(job, 'CONFIG', config), patch.object(job.os, 'geteuid', return_value=0), patch.object(Path, 'stat', return_value=metadata):
                self.assertEqual(job.configuration()['BACKUP_REMOTE'], 'gs://fixture/test')
                for bad in ('BACKUP_DIR=/x\nBACKUP_DIR=/y\n', 'TOKEN=secret\n'):
                    config.write_text(bad)
                    with self.assertRaises(ValueError): job.configuration()

    def test_redis_timer_is_staggered_and_process_group_is_bounded(self):
        root = Path(__file__).parents[1]
        timer = (root / 'deploy/map-test-redis-backup.timer').read_text()
        service = (root / 'deploy/map-test-redis-backup.service').read_text()
        self.assertIn('*:15,45:00', timer)
        self.assertIn('TimeoutStartSec=600', service)
        self.assertIn('KillMode=control-group', service)
