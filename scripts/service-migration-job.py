#!/usr/bin/env python3
"""Run one service image's standalone migrator with only its own DB credentials.

Receives rendered Compose JSON on stdin; never prints it. Provisioning roles is a
separate operation. A private, internal network joins only PostgreSQL and this
bounded, disposable job. Existing DB containers and volumes are never recreated.

Each service contributes an image pattern, the credential names it accepts, the
command its image runs and the single completion line it prints. Everything else
below is shared, so a service cannot quietly widen the isolation rules.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import urllib.parse
import uuid

ID = re.compile(r'[a-f0-9]{64}')
LABEL = 'kr.mapservice.job'
GATEWAY_OPTION = 'com.docker.network.bridge.gateway_mode_ipv4'
DATABASE = re.compile(r'[a-z_][a-z0-9_]{0,62}')
IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]{0,62}')
ENV = {'PATH': '/usr/bin:/bin:/usr/local/bin', 'DOCKER_HOST': 'unix:///var/run/docker.sock',
       'DOCKER_CONFIG': '/var/empty'}


class JobError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise JobError(code)


def image_pattern(service):
    return re.compile(r'ghcr\.io/we-meet-trip/map-service-' + service + r'@sha256:[a-f0-9]{64}')


def dsn_parts(value, schemes, code):
    """Split a connection URI into the parts the target contract compares."""
    require(isinstance(value, str) and value, code)
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme in schemes and parsed.hostname and parsed.username
            and parsed.password and parsed.path.startswith('/'), code)
    database = parsed.path[1:]
    require(DATABASE.fullmatch(database) and not parsed.query and not parsed.fragment, code)
    return {'host': parsed.hostname, 'port': parsed.port or 5432,
            'database': database, 'user': urllib.parse.unquote(parsed.username),
            'password': urllib.parse.unquote(parsed.password)}


def user_contract(service, environment, credentials):
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
            and environment.get('JAVA_TOOL_OPTIONS', '-XX:MaxRAMPercentage=70') == '-XX:MaxRAMPercentage=70',
            'serving_database_configuration_override')
    require('POSTGRES_PASSWORD' not in environment and 'POSTGRES_USER' not in environment,
            'serving_has_migration_credentials')
    require(environment.get('USER_DATABASE_USER') == 'map_user_runtime'
            and bool(environment.get('USER_DATABASE_PASSWORD')), 'user_runtime_credentials_missing')
    database = environment.get('POSTGRES_DB', '')
    require(isinstance(database, str) and DATABASE.fullmatch(database), 'database_name_invalid')
    require(credentials['USER_MIGRATION_URL'] ==
            f'jdbc:postgresql://postgres:5432/{database}?currentSchema=user_service',
            'migration_database_target_mismatch')
    require(credentials['USER_MIGRATION_PASSWORD'] != environment['USER_DATABASE_PASSWORD'],
            'migration_runtime_credentials_shared')
    return {}


def hub_contract(service, environment, credentials):
    runtime = dsn_parts(environment.get('HUB_DATABASE_URL'),
                        ('postgresql+psycopg', 'postgresql+psycopg_async', 'postgresql+asyncpg',
                         'postgresql'), 'hub_runtime_dsn_invalid')
    require(runtime['user'] == 'map_hub_runtime', 'hub_runtime_role_mismatch')
    require(runtime['host'] == 'postgres' and runtime['port'] == 5432,
            'serving_database_target_mismatch')
    migration = dsn_parts(credentials['HUB_MIGRATION_DATABASE_URL'],
                          ('postgresql+psycopg', 'postgresql'), 'hub_migration_dsn_invalid')
    require(migration['user'] == service['login'], 'migration_credentials_contract')
    require((migration['host'], migration['port'], migration['database'])
            == (runtime['host'], runtime['port'], runtime['database']),
            'migration_database_target_mismatch')
    require(migration['password'] != runtime['password'], 'migration_runtime_credentials_shared')
    return {}


def agent_contract(service, environment, credentials):
    require(environment.get('POSTGRES_HOST', 'postgres') == 'postgres'
            and str(environment.get('POSTGRES_PORT', '5432')) == '5432',
            'serving_database_target_mismatch')
    require(environment.get('POSTGRES_USER') == 'map_agent_runtime'
            and bool(environment.get('POSTGRES_PASSWORD')), 'agent_runtime_credentials_missing')
    database = environment.get('POSTGRES_DB', '')
    require(isinstance(database, str) and DATABASE.fullmatch(database), 'database_name_invalid')
    schema = environment.get('LANGGRAPH_SCHEMA', 'langgraph')
    require(isinstance(schema, str) and IDENTIFIER.fullmatch(schema), 'agent_schema_invalid')
    migration = dsn_parts(credentials['AGENT_CHECKPOINT_MIGRATION_DSN'],
                          ('postgresql', 'postgres'), 'agent_migration_dsn_invalid')
    require(migration['user'] == service['login'], 'migration_credentials_contract')
    require((migration['host'], migration['port'], migration['database'])
            == ('postgres', 5432, database), 'migration_database_target_mismatch')
    require(migration['password'] != environment['POSTGRES_PASSWORD'],
            'migration_runtime_credentials_shared')
    # The migrator resolves the same schema the serving container reads.
    return {'LANGGRAPH_SCHEMA': schema}


def json_completion(operation, lines):
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if operation == 'check-config' and record == {'status': 'configuration_valid',
                                                      'database_connected': False}:
            records.append(record)
        elif isinstance(record, dict) and set(record) == {'status', 'operation', 'migrations_executed'} \
                and record['status'] == 'complete' and record['operation'] == operation \
                and type(record['migrations_executed']) is int and record['migrations_executed'] >= 0:
            records.append(record)
    return records


def line_completion(expected):
    def matcher(operation, lines):
        return [{'status': 'complete', 'operation': operation}
                for line in lines if line.strip() == expected]
    return matcher


SERVICES = {
    'user': {
        'kind': 'user-migration-v1',
        'prefix': 'USER_MIGRATION_',
        'keys': ('USER_MIGRATION_URL', 'USER_MIGRATION_USERNAME', 'USER_MIGRATION_PASSWORD'),
        'login': 'map_user_migrator',
        'login_key': 'USER_MIGRATION_USERNAME',
        'operations': ('check-config', 'validate', 'migrate'),
        'seconds': 300,
        'contract': user_contract,
        'completion': json_completion,
        'command': lambda operation: [
            'java', '-Xmx256m', '-Dloader.main=map.migration.UserMigrationApplication',
            '-cp', '/app/app.jar', 'org.springframework.boot.loader.launch.PropertiesLauncher',
            operation],
    },
    'hub': {
        'kind': 'hub-migration-v1',
        'prefix': 'HUB_MIGRATION_',
        'keys': ('HUB_MIGRATION_DATABASE_URL',),
        'login': 'map_hub_migrator',
        'login_key': None,
        'operations': ('migrate',),
        'seconds': 900,
        'contract': hub_contract,
        'completion': line_completion('Hub migration completed'),
        'command': lambda operation: ['python', '-m', 'app.db.migrate'],
    },
    'agent': {
        'kind': 'agent-migration-v1',
        'prefix': 'AGENT_CHECKPOINT_MIGRATION_',
        'keys': ('AGENT_CHECKPOINT_MIGRATION_DSN',),
        'login': 'map_agent_migrator',
        'login_key': None,
        'operations': ('migrate',),
        'seconds': 900,
        'contract': agent_contract,
        'completion': line_completion('Agent checkpoint migration completed'),
        'command': lambda operation: ['python', '-m', 'app.checkpoint_migrate'],
    },
}


def service_contract(name):
    require(name in SERVICES, 'migration_service_unknown')
    return {**SERVICES[name], 'name': name, 'image': image_pattern(name),
            'suffix': name + '-migration'}


def command(args, timeout=30):
    try:
        result = subprocess.run(['docker', *args], env=ENV, capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise JobError('docker_command_unavailable') from None
    require(result.returncode == 0, 'docker_command_failed')
    return result.stdout.strip()


def read_credentials(service, path):
    keys = set(service['keys'])
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
        require(key in keys and key not in values and value and not any(ord(c) < 32 for c in value),
                'migration_credentials_invalid')
        values[key] = value
    require(set(values) == keys, 'migration_credentials_contract')
    if service['login_key']:
        require(values[service['login_key']] == service['login'], 'migration_credentials_contract')
    return values


def contract(service, config, credentials):
    project = config.get('name')
    require(project in ('map-test', 'map-service', 'map-prod'), 'migration_project_invalid')
    production = config.get('x-map-production')
    if project == 'map-prod':
        require(isinstance(production, dict) and set(production) == {
            'environment', 'database', 'postgres_container_id', 'postgres_image_id'},
            'production_database_pin_required')
        require(production['environment'] == 'prod' and production['database'] == 'map_prod'
                and isinstance(production['postgres_container_id'], str)
                and ID.fullmatch(production['postgres_container_id'])
                and isinstance(production['postgres_image_id'], str)
                and re.fullmatch(r'sha256:[a-f0-9]{64}', production['postgres_image_id']),
                'production_database_identity_invalid')
    else:
        require(production is None, 'production_pin_on_nonproduction_project')
    try:
        entry = config['services'][service['name']]
        environment = entry['environment']
        image = entry['image']
    except (KeyError, TypeError):
        raise JobError('serving_compose_contract_missing') from None
    require(isinstance(image, str) and service['image'].fullmatch(image), 'serving_image_not_pinned')
    require(isinstance(environment, dict), 'serving_environment_invalid')
    # A serving container never carries the migrator's own credential names,
    # including any variant that shares their reserved prefix.
    require(not any(key.startswith(service['prefix']) for key in environment),
            'serving_has_migration_credentials')
    require(not entry.get('command') and not entry.get('entrypoint'),
            'serving_database_configuration_override')
    extra = service['contract'](service, environment, credentials)
    if production is not None:
        database = (urllib.parse.urlsplit(environment['HUB_DATABASE_URL']).path.removeprefix('/')
                    if service['name'] == 'hub' else environment.get('POSTGRES_DB'))
        require(database == production['database'], 'production_database_target_mismatch')
    return project, image, extra


def container_state(cid):
    require(ID.fullmatch(cid), 'container_id_invalid')
    result = json.loads(command(['inspect', '--format',
        '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},'
        '"exit":{{json .State.ExitCode}},"oom":{{json .State.OOMKilled}},'
        '"started":{{json .State.StartedAt}},"restarts":{{json .RestartCount}}}', cid]))
    require(result['id'] == cid, 'container_identity_changed')
    return result


def prepare_network(service, project, production=None):
    ids = command(['ps', '-q', '--no-trunc', '--filter', f'label=com.docker.compose.project={project}',
                   '--filter', 'label=com.docker.compose.service=postgres']).splitlines()
    require(len(ids) == 1 and ID.fullmatch(ids[0]), 'one_running_postgres_required')
    pgid = ids[0]
    pg_before = container_state(pgid)
    if project == 'map-prod':
        require(production is not None and pgid == production['postgres_container_id']
                and pg_before['image'] == production['postgres_image_id'] and pg_before['running'] is True,
                'production_postgres_identity_changed')
        labels = json.loads(command(['inspect', '--format',
            '{"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
            '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}', pgid]))
        require(labels == {'project': 'map-prod', 'service': 'postgres'},
                'production_postgres_scope_changed')
    else:
        require(production is None, 'production_pin_on_nonproduction_network')
    name = project + '-' + service['suffix']
    existing = command(['network', 'ls', '--filter', f'name=^{name}$', '--format', '{{.Name}}']).splitlines()
    require(existing in ([], [name]), 'migration_network_ambiguous')
    if not existing:
        command(['network', 'create', '--internal', '--ipv6=false',
                 '--opt', f'{GATEWAY_OPTION}=isolated', '--label', f'{LABEL}={service["kind"]}',
                 '--label', f'kr.mapservice.project={project}', name])
    network = json.loads(command(['network', 'inspect', name]))[0]
    require(network['Internal'] is True and network['Driver'] == 'bridge'
            and network.get('Labels', {}).get(LABEL) == service['kind']
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


def remove_private_job(service, directory, project):
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and info.st_mode & 0o777 == 0o700
            and re.fullmatch(re.escape(project + '-' + service['suffix']) + r'-[a-f0-9]{32}',
                             directory.name),
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
            and service['image'].fullmatch(record['image']), 'orphan_job_marker_invalid')
    secret = directory / 'migration.env'
    if secret.exists() or secret.is_symlink():
        private_file(secret)
        secret.unlink()
    marker.unlink()
    directory.rmdir()


def no_previous_job(service, project, scratch):
    ids = command(['ps', '-aq', '--no-trunc', '--filter', f'label={LABEL}={service["kind"]}',
                   '--filter', f'label=kr.mapservice.project={project}']).splitlines()
    for cid in ids:
        require(ID.fullmatch(cid), 'job_identity_invalid')
        # Never stop a previously running migration or start another beside it.
        require(not container_state(cid)['running'], 'previous_migration_still_running')
    for cid in ids:
        command(['rm', cid])  # Only our already exited/created jobs; no volumes.
    # A create response lost before start leaves no process to time out. Recover
    # its durable marker and private env copy after rejecting all running jobs.
    for directory in scratch.glob(project + '-' + service['suffix'] + '-*'):
        remove_private_job(service, directory, project)


def prepare_private_job(service, scratch, project, image):
    name = project + '-' + service['suffix'] + '-' + uuid.uuid4().hex
    directory = scratch / name
    directory.mkdir(mode=0o700)
    with (directory / 'job.json').open('x') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({'schema': 1, 'project': project, 'name': name, 'image': image}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    # Credentials are not created until the ownership marker is durable.
    return directory


def create_args(service, name, project, image, network, env_file, operation):
    require(operation in service['operations'], 'migration_operation_invalid')
    return ['create', '--pull=never', '--name', name, '--label', f'{LABEL}={service["kind"]}',
            '--label', f'kr.mapservice.project={project}', '--network', network,
            '--restart=no', '--init', '--no-healthcheck', '--read-only',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=67108864', '--user', '10001',
            '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
            '--memory=384m', '--cpus=0.5', '--pids-limit=128', '--log-driver=none',
            '--env-file', str(env_file), '--entrypoint', '/usr/bin/timeout', image,
            '--signal=TERM', '--kill-after=10s', f'{service["seconds"]}s',
            *service['command'](operation)]


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


def run_job(service, config, credentials, operation, scratch):
    project, image, extra = contract(service, config, credentials)
    # Resolve locally before any DB network mutation; never pull in this job.
    image_id = command(['image', 'inspect', '--format', '{{.Id}}', image])
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', image_id), 'serving_image_identity_invalid')
    no_previous_job(service, project, scratch)
    if project == 'map-prod':
        network, pgid, pg_before = prepare_network(service, project, config['x-map-production'])
    else:
        network, pgid, pg_before = prepare_network(service, project)
    cid = None
    outcome = None
    cleanup_ok = False
    create_attempted = False
    temporary = prepare_private_job(service, scratch, project, image)
    try:
        env_path = temporary / 'migration.env'
        with env_path.open('x') as stream:
            os.fchmod(stream.fileno(), 0o600)
            for key in sorted({**credentials, **extra}):
                stream.write(key + '=' + {**credentials, **extra}[key] + '\n')
        try:
            create_attempted = True
            cid = command(create_args(service, temporary.name, project, image, network,
                                      env_path, operation))
            require(ID.fullmatch(cid), 'created_job_id_invalid')
            env_path.unlink()  # Docker has consumed the file; no host copy during the job.
            try:
                result = subprocess.run(['docker', 'start', '-a', cid], env=ENV,
                                        capture_output=True, text=True,
                                        timeout=service['seconds'] + 30)
            except subprocess.TimeoutExpired:
                raise JobError('migration_host_deadline_exceeded') from None
            actual = container_state(cid)
            require(actual['image'] == image_id and not actual['running'] and not actual['oom'],
                    'migration_job_not_completed')
            require(result.returncode == 0 and actual['exit'] == 0, 'migration_process_failed')
            records = service['completion'](operation, result.stdout.splitlines())
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
            remove_private_job(service, temporary, project)
        # Unknown Docker create outcome retains the durable private marker for
        # the next locked attempt. Never report cleanup success in this case.
    require(cleanup_ok and container_state(pgid) == pg_before,
            'migration_cleanup_or_postgres_preservation_failed')
    network_after = json.loads(command(['network', 'inspect', network]))[0]
    require(set(network_after.get('Containers') or {}) == {pgid}, 'migration_network_not_database_only')
    return {'status': 'PASS', 'at': datetime.now(timezone.utc).isoformat(), 'project': project,
            'service': service['name'], 'image': image, 'image_id': image_id,
            'operation': operation, 'result': outcome,
            'database_container_preserved': True, 'temporary_job_removed': True,
            'network_database_only_after_job': True,
            'inside_container_deadline_seconds': service['seconds'],
            'credential_names': sorted(service['keys']), 'raw_job_output_suppressed': True}


@contextmanager
def job_lock(service, scratch):
    fd = os.open(scratch / (service['suffix'] + '.lock'),
                 os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and info.st_mode & 0o777 == 0o600 and info.st_nlink == 1, 'unsafe_migration_lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JobError('another_migration_launcher_active') from None
        yield


@contextmanager
def production_lock(service, config, scratch, credentials, receipt):
    """Direct CLI invocations share the receiver/backup writer lock on NCP."""
    require(config.get('name') == 'map-prod', 'production_project_required')
    state = Path('/srv/map-prod/deploy')
    require(scratch == state / 'migrations' and receipt.parent == state / 'receipts'
            and credentials == Path('/srv/map-prod/secrets') / (service['name'] + '-migration.env'),
            'production_migration_paths_required')
    for path in (scratch, receipt.parent, credentials.parent):
        for parent in (path, *path.parents):
            meta = parent.lstat()
            require(stat.S_ISDIR(meta.st_mode) and meta.st_uid == 0 and not meta.st_mode & 0o022,
                    'production_migration_path_unsafe')
    with job_lock({'suffix': 'deploy'}, state):
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, choices=sorted(SERVICES))
    parser.add_argument('--credentials', required=True, type=Path)
    parser.add_argument('--operation', choices=('check-config', 'validate', 'migrate'),
                        default='migrate')
    parser.add_argument('--scratch', type=Path, default=Path('/var/lib/map-deploy'))
    parser.add_argument('--receipt', required=True, type=Path)
    args = parser.parse_args()
    can_write_receipt = False
    try:
        service = service_contract(args.service)
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
        credentials = read_credentials(service, args.credentials)
        shared = (production_lock(service, config, args.scratch, args.credentials, args.receipt)
                  if config.get('name') == 'map-prod' else nullcontext())
        with shared, job_lock(service, args.scratch), bounded_signals():
            result = run_job(service, config, credentials, args.operation, args.scratch)
    except Exception as error:
        result = {'status': 'FAIL', 'service': args.service,
                  'error_code': str(error) if isinstance(error, JobError) else 'migration_job_failed',
                  'raw_job_output_suppressed': True}
    # Receipt contains only a fixed schema, image identities and bounded codes.
    if can_write_receipt and not args.receipt.exists() and not args.receipt.is_symlink():
        try:
            with args.receipt.open('x') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(result, indent=2) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            result = {'status': 'FAIL', 'error_code': 'migration_receipt_write_failed'}
    print(json.dumps(result))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
