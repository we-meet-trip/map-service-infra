#!/usr/bin/env python3
"""Run the exact User image's standalone migrator with only its own DB credentials.

Receives rendered Compose JSON on stdin; never prints it. Provisioning roles is a
separate operation. A private, internal network joins only PostgreSQL and this
bounded, disposable job. Existing DB containers and volumes are never recreated.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import uuid

IMAGE = re.compile(r'ghcr\.io/we-meet-trip/map-service-user@sha256:[a-f0-9]{64}')
ID = re.compile(r'[a-f0-9]{64}')
KEYS = {'USER_MIGRATION_URL', 'USER_MIGRATION_USERNAME', 'USER_MIGRATION_PASSWORD'}
LABEL = 'kr.mapservice.job'
KIND = 'user-migration-v1'
GATEWAY_OPTION = 'com.docker.network.bridge.gateway_mode_ipv4'
MAX_SECONDS = 300
ENV = {'PATH': '/usr/bin:/bin:/usr/local/bin', 'DOCKER_HOST': 'unix:///var/run/docker.sock',
       'DOCKER_CONFIG': '/var/empty'}

class JobError(Exception):
    pass

def require(condition, code):
    if not condition:
        raise JobError(code)

def command(args, timeout=30):
    try:
        result = subprocess.run(['docker', *args], env=ENV, capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise JobError('docker_command_unavailable') from None
    require(result.returncode == 0, 'docker_command_failed')
    return result.stdout.strip()

def read_credentials(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and info.st_mode & 0o777 == 0o600 and info.st_nlink == 1,
                    'unsafe_migration_credentials')
            raw = stream.read(16385)
        require(len(raw) <= 16384, 'migration_credentials_too_large')
        rows = raw.decode('utf-8').splitlines()
    except (OSError, UnicodeError):
        raise JobError('migration_credentials_unreadable') from None
    values = {}
    for line in rows:
        require('=' in line and '\x00' not in line, 'migration_credentials_invalid')
        key, value = line.split('=', 1)
        require(key in KEYS and key not in values and value and not any(ord(c) < 32 for c in value),
                'migration_credentials_invalid')
        values[key] = value
    require(set(values) == KEYS and values['USER_MIGRATION_USERNAME'] == 'map_user_migrator',
            'migration_credentials_contract')
    return values

def contract(config, credentials):
    project = config.get('name')
    require(project in ('map-test', 'map-service'), 'migration_project_invalid')
    try:
        service = config['services']['user']
        environment = service['environment']
        image = service['image']
    except (KeyError, TypeError):
        raise JobError('user_compose_contract_missing') from None
    require(isinstance(image, str) and IMAGE.fullmatch(image), 'user_image_not_pinned')
    require(isinstance(environment, dict), 'user_environment_invalid')
    require(environment.get('POSTGRES_HOST', 'postgres') == 'postgres'
            and str(environment.get('POSTGRES_PORT', '5432')) == '5432',
            'serving_database_target_mismatch')
    # Spring/JVM arguments can otherwise override the reviewed datasource target.
    # Spring binds the environment loosely: spring.datasource.url,
    # SPRING.DATASOURCE.URL and spring_datasource_url all reach the same
    # property, so the names are folded to one spelling before the check.
    require(not any(k.upper().replace('.', '_').replace('-', '_')
                     .startswith(('SPRING_', 'JAVA_', 'JDK_', '_JAVA_', 'LOADER_'))
                    for k in environment if k != 'JAVA_TOOL_OPTIONS')
            and environment.get('JAVA_TOOL_OPTIONS', '-XX:MaxRAMPercentage=70') == '-XX:MaxRAMPercentage=70'
            and not service.get('command') and not service.get('entrypoint'),
            'serving_database_configuration_override')
    require('POSTGRES_PASSWORD' not in environment and 'POSTGRES_USER' not in environment
            and not any(k.startswith('USER_MIGRATION_') for k in environment),
            'serving_has_migration_credentials')
    require(environment.get('USER_DATABASE_USER') == 'map_user_runtime'
            and bool(environment.get('USER_DATABASE_PASSWORD')), 'user_runtime_credentials_missing')
    database = environment.get('POSTGRES_DB', '')
    require(isinstance(database, str) and re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', database),
            'database_name_invalid')
    require(credentials['USER_MIGRATION_URL'] ==
            f'jdbc:postgresql://postgres:5432/{database}?currentSchema=user_service',
            'migration_database_target_mismatch')
    require(credentials['USER_MIGRATION_PASSWORD'] != environment['USER_DATABASE_PASSWORD'],
            'migration_runtime_credentials_shared')
    return project, image

def container_state(cid):
    require(ID.fullmatch(cid), 'container_id_invalid')
    result = json.loads(command(['inspect', '--format',
        '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},'
        '"exit":{{json .State.ExitCode}},"oom":{{json .State.OOMKilled}},'
        '"started":{{json .State.StartedAt}},"restarts":{{json .RestartCount}}}', cid]))
    require(result['id'] == cid, 'container_identity_changed')
    return result

def prepare_network(project):
    ids = command(['ps', '-q', '--no-trunc', '--filter', f'label=com.docker.compose.project={project}',
                   '--filter', 'label=com.docker.compose.service=postgres']).splitlines()
    require(len(ids) == 1 and ID.fullmatch(ids[0]), 'one_running_postgres_required')
    pgid = ids[0]
    pg_before = container_state(pgid)
    name = project + '-user-migration'
    existing = command(['network', 'ls', '--filter', f'name=^{name}$', '--format', '{{.Name}}']).splitlines()
    require(existing in ([], [name]), 'migration_network_ambiguous')
    if not existing:
        command(['network', 'create', '--internal', '--ipv6=false',
                 '--opt', f'{GATEWAY_OPTION}=isolated', '--label', f'{LABEL}={KIND}',
                 '--label', f'kr.mapservice.project={project}', name])
    network = json.loads(command(['network', 'inspect', name]))[0]
    require(network['Internal'] is True and network['Driver'] == 'bridge'
            and network.get('Labels', {}).get(LABEL) == KIND
            and network.get('Labels', {}).get('kr.mapservice.project') == project,
            'migration_network_not_owned')
    # Internal bridge alone still exposes host services through its gateway.
    require(network.get('EnableIPv6') is False
            and network.get('Options', {}).get(GATEWAY_OPTION) == 'isolated',
            'migration_network_host_gateway_not_isolated')
    endpoints = network.get('Containers') or {}
    require(set(endpoints).issubset({pgid}), 'migration_network_has_foreign_endpoint')
    if pgid not in endpoints:
        command(['network', 'connect', '--alias', 'postgres', name, pgid])
    require(container_state(pgid) == pg_before, 'postgres_changed_during_network_attachment')
    return name, pgid, pg_before

def private_file(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and info.st_mode & 0o777 == 0o600 and info.st_nlink == 1,
            'unsafe_orphan_job_file')

def remove_private_job(directory, project):
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and info.st_mode & 0o777 == 0o700
            and re.fullmatch(re.escape(project) + r'-user-migration-[a-f0-9]{32}', directory.name),
            'unsafe_orphan_job_directory')
    require({p.name for p in directory.iterdir()}.issubset({'job.json', 'migration.env'}),
            'orphan_job_has_unknown_files')
    marker = directory / 'job.json'
    require(marker.exists(), 'orphan_job_marker_missing')
    private_file(marker)
    require(marker.stat().st_size <= 4096, 'orphan_job_marker_invalid')
    record = json.loads(marker.read_text())
    require(isinstance(record, dict) and set(record) == {'schema', 'project', 'name', 'image'}
            and record['schema'] == 1 and record['project'] == project
            and record['name'] == directory.name and isinstance(record['image'], str)
            and IMAGE.fullmatch(record['image']), 'orphan_job_marker_invalid')
    secret = directory / 'migration.env'
    if secret.exists() or secret.is_symlink():
        private_file(secret)
        secret.unlink()
    marker.unlink()
    directory.rmdir()

def no_previous_job(project, scratch):
    ids = command(['ps', '-aq', '--no-trunc', '--filter', f'label={LABEL}={KIND}',
                   '--filter', f'label=kr.mapservice.project={project}']).splitlines()
    for cid in ids:
        require(ID.fullmatch(cid), 'job_identity_invalid')
        # Never stop a previously running migration or start another beside it.
        require(not container_state(cid)['running'], 'previous_migration_still_running')
    for cid in ids:
        command(['rm', cid])  # Only our already exited/created jobs; no volumes.
    # A create response lost before start leaves no process to time out. Recover
    # its durable marker and private env copy after rejecting all running jobs.
    for directory in scratch.glob(project + '-user-migration-*'):
        remove_private_job(directory, project)

def prepare_private_job(scratch, project, image):
    name = project + '-user-migration-' + uuid.uuid4().hex
    directory = scratch / name
    directory.mkdir(mode=0o700)
    with (directory / 'job.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({'schema': 1, 'project': project, 'name': name, 'image': image}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    # Credentials are not created until the ownership marker is durable.
    return directory

def create_args(name, project, image, network, env_file, operation):
    require(operation in ('check-config', 'validate', 'migrate'), 'migration_operation_invalid')
    return ['create', '--pull=never', '--name', name, '--label', f'{LABEL}={KIND}',
            '--label', f'kr.mapservice.project={project}', '--network', network,
            '--restart=no', '--init', '--no-healthcheck', '--read-only',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=67108864', '--user', '10001',
            '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
            '--memory=384m', '--cpus=0.5', '--pids-limit=128', '--log-driver=none',
            '--env-file', str(env_file), '--entrypoint', '/usr/bin/timeout', image,
            '--signal=TERM', '--kill-after=10s', f'{MAX_SECONDS}s', 'java',
            '-Xmx256m', '-Dloader.main=map.migration.UserMigrationApplication',
            '-cp', '/app/app.jar', 'org.springframework.boot.loader.launch.PropertiesLauncher', operation]

@contextmanager
def bounded_signals():
    prior = {}
    def interrupted(signum, frame):
        raise JobError('migration_interrupted')
    for sig in (signal.SIGTERM, signal.SIGINT):
        prior[sig] = signal.signal(sig, interrupted)
    try:
        yield
    finally:
        for sig, handler in prior.items():
            signal.signal(sig, handler)

def run_job(config, credentials, operation, scratch):
    project, image = contract(config, credentials)
    # Resolve locally before any DB network mutation; never pull in this job.
    image_id = command(['image', 'inspect', '--format', '{{.Id}}', image])
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', image_id), 'user_image_identity_invalid')
    no_previous_job(project, scratch)
    network, pgid, pg_before = prepare_network(project)
    cid = None
    outcome = None
    cleanup_ok = False
    create_attempted = False
    temporary = prepare_private_job(scratch, project, image)
    try:
        env_path = temporary / 'migration.env'
        with env_path.open('x') as stream:
            os.fchmod(stream.fileno(), 0o600)
            for key in sorted(KEYS):
                stream.write(key + '=' + credentials[key] + '\n')
        try:
            create_attempted = True
            cid = command(create_args(temporary.name, project, image, network, env_path, operation))
            require(ID.fullmatch(cid), 'created_job_id_invalid')
            env_path.unlink()  # Docker has consumed the file; no host copy during the job.
            try:
                result = subprocess.run(['docker', 'start', '-a', cid], env=ENV,
                                        capture_output=True, text=True, timeout=MAX_SECONDS + 30)
            except subprocess.TimeoutExpired:
                raise JobError('migration_host_deadline_exceeded') from None
            actual = container_state(cid)
            require(actual['image'] == image_id and not actual['running'] and not actual['oom'],
                    'migration_job_not_completed')
            require(result.returncode == 0 and actual['exit'] == 0, 'migration_process_failed')
            records = []
            for line in result.stdout.splitlines():
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if operation == 'check-config' and record == {'status':'configuration_valid','database_connected':False}:
                    records.append(record)
                elif isinstance(record, dict) and set(record) == {'status','operation','migrations_executed'} \
                        and record['status'] == 'complete' and record['operation'] == operation \
                        and type(record['migrations_executed']) is int and record['migrations_executed'] >= 0:
                    records.append(record)
            require(len(records) == 1, 'migration_completion_receipt_missing')
            outcome = records[0]
        finally:
            if cid is not None and ID.fullmatch(cid):
                actual = container_state(cid)
                if actual['running']:
                    command(['stop', '--time', '10', cid], timeout=30)
                command(['rm', cid])
                cleanup_ok = True
    finally:
        if cleanup_ok or not create_attempted:
            remove_private_job(temporary, project)
        # Unknown Docker create outcome retains the durable private marker for
        # the next locked attempt. Never report cleanup success in this case.
    require(cleanup_ok and container_state(pgid) == pg_before, 'migration_cleanup_or_postgres_preservation_failed')
    network_after = json.loads(command(['network', 'inspect', network]))[0]
    require(set(network_after.get('Containers') or {}) == {pgid}, 'migration_network_not_database_only')
    return {'status':'PASS','at':datetime.now(timezone.utc).isoformat(), 'project':project,
            'image':image,'image_id':image_id,'operation':operation,'result':outcome,
            'database_container_preserved':True,'temporary_job_removed':True,
            'network_database_only_after_job':True,'inside_container_deadline_seconds':MAX_SECONDS,
            'credential_names':sorted(KEYS),'raw_job_output_suppressed':True}

@contextmanager
def job_lock(scratch):
    fd = os.open(scratch / 'user-migration.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and info.st_mode & 0o777 == 0o600 and info.st_nlink == 1, 'unsafe_migration_lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JobError('another_migration_launcher_active') from None
        yield

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', required=True, type=Path)
    parser.add_argument('--operation', choices=('check-config','validate','migrate'), default='migrate')
    parser.add_argument('--scratch', type=Path, default=Path('/var/lib/map-deploy'))
    parser.add_argument('--receipt', required=True, type=Path)
    args = parser.parse_args()
    can_write_receipt = False
    try:
        require(os.geteuid() == 0, 'root_deployment_operator_required')
        for path in (args.scratch, args.receipt.parent):
            info = path.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                    'unsafe_migration_state_directory')
        require(not args.receipt.exists() and not args.receipt.is_symlink(), 'receipt_already_exists')
        can_write_receipt = True
        import sys
        raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
        require(len(raw) <= 4 * 1024 * 1024, 'compose_input_too_large')
        config = json.loads(raw)
        credentials = read_credentials(args.credentials)
        with job_lock(args.scratch), bounded_signals():
            result = run_job(config, credentials, args.operation, args.scratch)
    except Exception as error:
        result = {'status':'FAIL','error_code':str(error) if isinstance(error, JobError)
                  else 'migration_job_failed','raw_job_output_suppressed':True}
    # Receipt contains only a fixed schema, image identities and bounded codes.
    if can_write_receipt and not args.receipt.exists() and not args.receipt.is_symlink():
        try:
            with args.receipt.open('x') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(result, indent=2) + '\n')
                stream.flush(); os.fsync(stream.fileno())
        except OSError:
            result = {'status':'FAIL','error_code':'migration_receipt_write_failed'}
    print(json.dumps(result))
    return 0 if result['status'] == 'PASS' else 1

if __name__ == '__main__':
    raise SystemExit(main())
