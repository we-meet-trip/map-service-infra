"""Private timer configuration, shared deployment/backup locks, safe job status."""
from contextlib import ExitStack
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

CONFIG = Path('/etc/map-deploy/backup.env')
STATE = Path('/var/lib/map-deploy')
REPO = Path('/home/mapadmin26/map-service-infra')
LIB = Path('/usr/local/lib/map-deploy')
ALLOWED = {'BACKUP_DIR', 'BACKUP_REMOTE', 'BACKUP_S3_ENDPOINT', 'BACKUP_GCP_CREDENTIALS_FILE', 'BACKUP_REQUIRE_REMOTE'}
CHILD_CODES = {'persistence_busy', 'aof_write_unhealthy', 'redis_fork_memory_limit',
               'host_fork_memory_low', 'snapshot_disk_low', 'review_restore_capacity_before_large_backup',
               'bgsave_not_started', 'bgsave_failed', 'bgsave_timeout', 'snapshot_copy_failed',
               'snapshot_size_limit', 'isolated_restore_timeout', 'restored_key_count_mismatch',
               'restore_version_mismatch', 'operation_cancelled', 'operation_failed', 'command_failed'}


LOCK_WAIT_SECONDS = 150


def acquire(lock, seconds):
    """짧게 잡았다 놓기를 되풀이하는 이웃과 겨루려면 한 번 시도로는 모자란다.
    배포처럼 오래 쥐는 쪽에는 양보하도록 기다리는 시간에 상한을 둔다."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)


def configuration():
    metadata = CONFIG.stat()
    if CONFIG.is_symlink() or os.geteuid() != 0 or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError('private_root_configuration_required')
    values = {}
    for line in CONFIG.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in ALLOWED or key in values:
            raise ValueError('invalid_backup_configuration')
        values[key] = value
    if not values.get('BACKUP_REMOTE') or not values.get('BACKUP_DIR'):
        raise ValueError('remote_and_local_destination_required')
    return values


def write_status(kind, status):
    target = STATE / ('backup-status.json' if kind == 'pg' else 'redis-backup-status.json')
    try:
        previous = json.loads(target.read_text()) if target.exists() else {}
        if not isinstance(previous, dict):
            previous = {}
    except (ValueError, OSError):
        previous = {}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    result = {'last_attempt_at': now, 'success': status['success'],
              'last_success_at': now if status['success'] else previous.get('last_success_at'), **status}
    if kind == 'redis' and 'snapshot_at' not in result and previous.get('snapshot_at'):
        result['snapshot_at'] = previous['snapshot_at']
    freshness = result.get('snapshot_at') if kind == 'redis' else result.get('last_success_at')
    try:
        last = dt.datetime.fromisoformat(freshness) if isinstance(freshness, str) else None
        if last is not None and last.tzinfo is None:
            last = None
    except ValueError:
        last = None
    if last is not None:
        result['rpo_data_age_seconds'] = max(0, int((dt.datetime.now(dt.timezone.utc) - last).total_seconds()))
        result['rpo_1h_overdue'] = result['rpo_data_age_seconds'] > 3600
    else:
        result['rpo_1h_overdue'] = True
    pending = target.with_suffix('.new')
    pending.write_text(json.dumps(result) + '\n')
    pending.replace(target)
    print(json.dumps(result))
    return bool(result.get('rpo_1h_overdue'))


def entry(kind):
    os.umask(0o077)
    if kind not in ('pg', 'redis'):
        raise ValueError('invalid_backup_kind')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = configuration()
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
    env.update(values, BACKUP_REQUIRE_REMOTE='1')
    if kind == 'redis':
        if not values['BACKUP_REMOTE'].startswith('gs://'):
            raise ValueError('existing_gcs_destination_required')
        env['REDIS_BACKUP_REMOTE'] = values['BACKUP_REMOTE'].rstrip('/') + '/redis-v1'
        env['REDIS_BACKUP_DIR'] = str(Path(values['BACKUP_DIR']) / 'redis-v1')
    # Same acquisition order for both timers. The receiver itself already holds
    # deploy.lock and runs its PG backup directly, avoiding recursive acquisition.
    #
    # Waiting matters: the public supervisor takes the same deployment lock on
    # every one of its short cycles, so an attempt that gives up immediately
    # loses to it again and again and the copy silently ages past its target.
    # A deployment holds the lock far longer than this wait, so a real one still
    # defers. Deferring while the copy is already older than its target is not
    # reported as a completed run.
    with ExitStack() as stack:
        for filename in ('deploy.lock', 'backup.lock'):
            lock = stack.enter_context((STATE / filename).open('a'))
            if not acquire(lock, LOCK_WAIT_SECONDS):
                overdue = write_status(kind, {'success': False, 'deferred': True, 'code': 'LOCK_BUSY'})
                return 1 if overdue else 0
        module = 'pg_backup' if kind == 'pg' else 'redis_backup'
        loader = 'from pathlib import Path; import ' + module + '; ' + module + '.ROOT=Path(' + repr(str(REPO)) + '); raise SystemExit(' + module + '.main())'
        process = subprocess.Popen([sys.executable, '-c', loader, 'backup', '--test'], cwd=LIB,
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            out, errors = process.communicate(timeout=540)
            status = {'success': process.returncode == 0, 'deferred': False,
                      'code': 'COMPLETE' if process.returncode == 0 else 'BACKUP_FAILED'}
            if kind == 'redis' and process.returncode == 0:
                evidence = json.loads(out)
                if evidence.get('remote_verified') is not True or evidence.get('restore') != 'PASS':
                    status.update(success=False, code='INCOMPLETE_EVIDENCE')
                else:
                    status['snapshot_at'] = evidence['snapshot_at']
                    status['remote_verified'] = True
            elif kind == 'redis':
                try:
                    failure = json.loads(errors)
                    if isinstance(failure, dict) and failure.get('code') in CHILD_CODES:
                        status['reason'] = failure['code']
                except (ValueError, TypeError):
                    pass
            write_status(kind, status)
            return 0 if status['success'] else 1
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            write_status(kind, {'success': False, 'deferred': False, 'code': 'BACKUP_TIMEOUT'})
            return 1


def safe_entry(kind):
    try:
        return entry(kind)
    except Exception as error:
        status = {'success': False, 'deferred': False, 'code': 'BACKUP_RUNNER_FAILED',
                  'error_type': type(error).__name__}
        try:
            write_status(kind, status)
        except Exception:
            print(json.dumps(status), file=sys.stderr)
        return 1
