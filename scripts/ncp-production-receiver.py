#!/usr/bin/env python3
"""Prepare one brand-new NCP production database; never activate public serving.

Each phase owns /srv/map-prod/deploy/deploy.lock. Existing data, a prior attempt,
or an uncertain outcome is a HOLD, never an instruction to reset or retry.
The separate User bootstrap controller consumes the private request emitted by
prepare. GCP's six-service receiver/watchdog remains a separate implementation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def source_ownership(root, *, required_path=Path('/opt/map-service-infra'), uid=0):
    """Check filesystem trust before importing any code from the checkout."""
    if root != required_path or not (root / '.git').is_dir() or (root / '.git').is_symlink():
        raise RuntimeError('root_source_checkout_required')
    for path in (root, *root.parents):
        item = path.lstat()
        if not stat.S_ISDIR(item.st_mode) or item.st_uid != uid or item.st_mode & 0o022:
            raise RuntimeError('root_source_ownership_required')
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name; item = path.lstat()
            if (not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode)) or
                    item.st_uid != uid or item.st_mode & 0o022 or
                    stat.S_ISREG(item.st_mode) and item.st_nlink != 1):
                raise RuntimeError('root_source_ownership_required')
    for name in ('commondir', 'objects/info/alternates'):
        if (root / '.git' / name).exists():
            raise RuntimeError('standalone_source_checkout_required')


if __name__ == '__main__':
    try:
        source_ownership(ROOT)
    except Exception:
        print(json.dumps({'status': 'HOLD', 'error_code': 'root_source_checkout_untrusted', 'public_serving': 'HOLD'}))
        raise SystemExit(1)

sys.path.insert(0, str(ROOT / 'scripts'))
import release_manifest


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


host = module('production_receiver_host', 'ncp-bootstrap-host.py')
artifacts = module('production_receiver_artifacts', 'ncp-bootstrap-artifacts.py')
private = module('production_receiver_private', 'ncp-bootstrap-backup.py')
migration = module('production_receiver_migration', 'service-migration-job.py')
DATA = Path('/srv/map-prod')
STATE = DATA / 'deploy'
CONFIG = DATA / 'secrets/production-runtime.json'
ENROLLMENT = Path('/var/lib/map-bootstrap/enrollment.json')
HEX = re.compile(r'[a-f0-9]{64}')
IMAGE = re.compile(r'sha256:[a-f0-9]{64}')
NETWORK = 'map-prod-user-migration'
PGDATA = DATA / 'data/postgres'
SECRET_NAMES = ('POSTGRES_PASSWORD', 'USER_BOOTSTRAP_MARKER', 'USER_BOOTSTRAP_PASSWORD',
                'USER_DATABASE_PASSWORD', 'USER_MIGRATION_PASSWORD')
ENV = {'PATH': '/usr/bin:/bin:/usr/local/bin', 'HOME': '/var/empty',
       'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONFIG': '/var/empty'}


class ReceiverError(RuntimeError):
    pass


def require(ok, code):
    if not ok:
        raise ReceiverError(code)


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def exact(value, keys, code):
    require(isinstance(value, dict) and set(value) == set(keys.split()), code)


def configuration(value):
    exact(value, 'schema_version environment project topology release_name enrollment_sha256 '
          'security_approval_sha256 release_manifest_sha256 bootstrap_approval_sha256 '
          'postgres_image_id user_image_id', 'runtime_contract_invalid')
    require(type(value['schema_version']) is int and value['schema_version'] == 1 and
            value['environment'] == 'prod' and value['project'] == 'map-prod' and
            value['topology'] == host.CURRENT_TOPOLOGY, 'ncp_production_only')
    require(isinstance(value['release_name'], str) and
            re.fullmatch(r'[a-z][a-z0-9-]{1,63}', value['release_name']), 'release_name_invalid')
    for key in ('enrollment_sha256', 'security_approval_sha256', 'release_manifest_sha256',
                'bootstrap_approval_sha256'):
        require(isinstance(value[key], str) and HEX.fullmatch(value[key]), 'approval_pin_required')
    for key in ('postgres_image_id', 'user_image_id'):
        require(isinstance(value[key], str) and IMAGE.fullmatch(value[key]), 'local_image_pin_required')
    return value


def private_dir(path):
    private.clean_path(path, exists=False)
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
            stat.S_IMODE(info.st_mode) == 0o700, 'private_directory_required')
    return path


def write_once(path, value):
    require(not path.exists() and not path.is_symlink(), 'prior_attempt_requires_hold')
    host.write_once(path, (json.dumps(value, indent=2, sort_keys=True) + '\n').encode())


@contextmanager
def writer(state):
    # The same lock inode and nonblocking flock as the standalone migration CLI.
    with migration.job_lock({'suffix': 'deploy'}, state):
        yield


def bootstrap_approval(value, manifest):
    exact(value, 'schema_version status source_sha github_run_id artifact_sha256 '
          'bootstrap_postgres_verified reviewer', 'bootstrap_approval_invalid')
    require(type(value['schema_version']) is int and value['schema_version'] == 1 and
            value['status'] == 'approved' and value['bootstrap_postgres_verified'] is True and
            value['source_sha'] == manifest['services']['user']['source_sha'] and
            isinstance(value['github_run_id'], str) and re.fullmatch(r'[1-9][0-9]{0,19}', value['github_run_id']) and
            isinstance(value['artifact_sha256'], str) and HEX.fullmatch(value['artifact_sha256']) and
            isinstance(value['reviewer'], str) and 0 < len(value['reviewer']) <= 100,
            'bootstrap_ci_review_required')


def release_binding(contract, manifest):
    release_manifest.validate(manifest)  # Always exactly six source images.
    require(artifacts.PROD_REQUIRED <= set(contract['images']) <=
            artifacts.PROD_REQUIRED | artifacts.PROD_OPTIONAL, 'production_image_allowlist_required')
    for service in ('user', 'agent', 'hub', 'yolo'):
        entry = manifest['services'][service]
        require(contract['images'][service]['image'] == entry['image'] + '@' + entry['digest']
                and contract['images'][service]['source_commit'] == entry['source_sha'],
                'production_release_subset_mismatch')


def identity_request(config, enrollment, contract, manifest, proof, pgid):
    """Closed bridge to the independent one-use User bootstrap controller."""
    return {'schema_version': 1, 'environment': 'prod', 'project': 'map-prod', 'database': 'map_prod',
            'enrollment_sha256': config['enrollment_sha256'],
            'security_approval_sha256': config['security_approval_sha256'],
            'release_manifest_sha256': config['release_manifest_sha256'],
            'runtime_contract_sha256': canonical(config),
            'new_host_proof_sha256': canonical(proof),
            'user': {'image': contract['images']['user']['image'], 'image_id': config['user_image_id'],
                     'source_sha': manifest['services']['user']['source_sha']},
            'postgres': {'container_id': pgid, 'image_id': config['postgres_image_id'],
                         'data_path': str(PGDATA), 'data_uuid': enrollment['data_uuid'],
                         'operator': 'postgres', 'network': NETWORK},
            'marker_sha256': proof['marker_sha256'],
            'secret_paths': {name: str(DATA / 'secrets' / name) for name in SECRET_NAMES},
            'created_at': now()}


def verify_bootstrap_receipt(value, request):
    exact(value, 'schema_version status input_request_sha256 identities migrations_executed '
          'finalization_complete bootstrap_login_disabled bootstrap_sessions_zero '
          'runtime_and_migrator_roles_ready postgres_identity_preserved started_at completed_at',
          'bootstrap_receipt_schema_invalid')
    require(type(value['schema_version']) is int and value['schema_version'] == 1 and value['status'] == 'PASS'
            and value['input_request_sha256'] == canonical(request)
            and value['identities'] == {key: request[key] for key in (
                'environment', 'project', 'database', 'enrollment_sha256', 'security_approval_sha256',
                'release_manifest_sha256', 'runtime_contract_sha256', 'new_host_proof_sha256',
                'user', 'postgres', 'marker_sha256')}
            and type(value['migrations_executed']) is int and value['migrations_executed'] == 4,
            'bootstrap_receipt_identity_mismatch')
    for key in ('finalization_complete', 'bootstrap_login_disabled', 'bootstrap_sessions_zero',
                'runtime_and_migrator_roles_ready', 'postgres_identity_preserved'):
        require(value[key] is True, 'bootstrap_finalization_incomplete')
    start, end = private.utc(value['started_at']), private.utc(value['completed_at'])
    require(private.utc(request['created_at']) <= start <= end <= datetime.now(timezone.utc),
            'bootstrap_receipt_time_invalid')
    return value


class Backend:
    def command(self, args, *, payload=None, timeout=30):
        try:
            result = subprocess.run(args, input=payload, capture_output=True, text=True, env=ENV,
                                    timeout=timeout, cwd=ROOT)
        except (OSError, subprocess.TimeoutExpired):
            raise ReceiverError('receiver_command_unavailable') from None
        require(result.returncode == 0, 'receiver_command_failed')
        return result.stdout.strip()

    def docker(self, args, **kwargs):
        return self.command(['docker', *args], **kwargs)

    def verify(self, config):
        source_ownership(ROOT)
        enrollment = private.read_json(ENROLLMENT, private=True)
        host.validate(enrollment); host.require_current_topology(enrollment)
        require(canonical(enrollment) == config['enrollment_sha256'], 'enrollment_pin_mismatch')
        host.verify(enrollment)
        stage = DATA / 'staging' / config['release_name']
        artifacts.verify_installed(stage, 'prod', config['security_approval_sha256'])
        contract = artifacts.read_json(stage / 'contract.json')
        receiver = contract['receiver']
        require(receiver is not None and 'empty-host-ncp-v1' in receiver['capabilities'] and
                receiver['sha256'] == artifacts.sha256(Path(__file__)), 'receiver_artifact_mismatch')
        installation = DATA / 'installations' / config['release_name']
        cache = private.read_json(installation / 'cache-receipt.json', private=True)
        require(cache == {'status': 'images_cached', 'role': 'prod',
                         'security_approval_sha256': config['security_approval_sha256'],
                         'contract_sha256': artifacts.sha256(stage / 'contract.json'),
                         'images': contract['images'], 'receiver_executed': False, 'serving_changes': 0},
                'artifact_cache_receipt_mismatch')
        release_path = installation / 'release.json'
        require(artifacts.sha256(release_path) == config['release_manifest_sha256'], 'release_pin_mismatch')
        manifest = private.read_json(release_path, private=True)
        release_binding(contract, manifest)
        require(self.command(['git', 'rev-parse', '--show-toplevel']) == str(ROOT)
                and self.command(['git', 'rev-parse', 'HEAD']) == manifest['infra_sha'] == receiver['source_commit']
                and not self.command(['git', 'status', '--porcelain', '--untracked-files=no']),
                'receiver_source_checkout_mismatch')
        approval_path = installation / 'bootstrap-approval.json'
        require(artifacts.sha256(approval_path) == config['bootstrap_approval_sha256'], 'bootstrap_approval_pin_mismatch')
        bootstrap_approval(private.read_json(approval_path, private=True), manifest)
        for service in ('postgres', 'user'):
            image = contract['images'][service]['image']
            actual = json.loads(self.docker(['image', 'inspect', '--format',
                '{"id":{{json .Id}},"os":{{json .Os}},"arch":{{json .Architecture}},'
                '"digests":{{json .RepoDigests}}}', image]))
            require(actual['id'] == config[service + '_image_id'] and actual['os'] == 'linux' and
                    actual['arch'] == 'amd64' and image in actual['digests'], 'cached_image_drift')
        return enrollment, contract, manifest

    def empty(self):
        require(not self.docker(['ps', '-aq', '--no-trunc']) and not self.docker(['volume', 'ls', '-q']),
                'empty_docker_host_required')
        require(not self.docker(['network', 'ls', '--filter', 'name=^' + NETWORK + '$', '--format', '{{.Name}}']),
                'prior_database_network_requires_hold')
        private.clean_path(PGDATA, exists=False)
        require(not PGDATA.exists(), 'new_postgres_data_path_required')

    def secrets(self):
        result = {}
        for name in SECRET_NAMES:
            with private.open_read(DATA / 'secrets' / name, private=True) as stream:
                raw = stream.read(66)
            # Match the SQL controller's private quoting and Docker FILE contract.
            value = raw.decode('ascii').removesuffix('\n')
            require(re.fullmatch(r'[a-f0-9]{64}', value), 'independent_32_byte_hex_secrets_required')
            result[name] = value
        require(len(set(result.values())) == len(result), 'bootstrap_secret_reuse')
        return result

    def prepare_postgres(self, contract, config):
        # Only the new dedicated data directory is created. The database inside
        # it starts with postgres; PostGIS init never runs against map_prod.
        private_dir(DATA / 'data'); private_dir(PGDATA)
        self.docker(['network', 'create', '--internal', '--ipv6=false', '--driver', 'bridge',
                     '--opt', migration.GATEWAY_OPTION + '=isolated',
                     '--label', migration.LABEL + '=user-migration-v1',
                     '--label', 'kr.mapservice.project=map-prod', NETWORK])
        cid = self.docker(['create', '--pull=never', '--name', 'map-prod-postgres-1',
            '--label', 'com.docker.compose.project=map-prod', '--label', 'com.docker.compose.service=postgres',
            '--label', 'kr.mapservice.receiver=empty-host-ncp-v1',
            '--network', NETWORK, '--network-alias', 'postgres', '--restart=no', '--no-healthcheck',
            '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=67108864',
            '--tmpfs', '/var/run/postgresql:rw,noexec,nosuid,size=16777216',
            '--memory=2g', '--cpus=1', '--pids-limit=256', '--shm-size=256m', '--log-driver=none',
            '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=DAC_OVERRIDE', '--cap-add=FOWNER',
            '--cap-add=SETGID', '--cap-add=SETUID', '--security-opt=no-new-privileges:true',
            '--mount', 'type=bind,src=' + str(PGDATA) + ',dst=/var/lib/postgresql/data',
            '--mount', 'type=bind,src=' + str(DATA / 'secrets/POSTGRES_PASSWORD') + ',dst=/run/secrets/pg-password,readonly',
            '--env', 'POSTGRES_PASSWORD_FILE=/run/secrets/pg-password', '--env', 'POSTGRES_USER=postgres',
            '--env', 'POSTGRES_DB=postgres', contract['images']['postgres']['image']])
        require(HEX.fullmatch(cid), 'created_postgres_identity_invalid')
        # Preserve the exact identity before start; a lost create/start outcome
        # leaves the durable attempt and all data for operator quarantine.
        write_once(STATE / 'postgres-created.json', {'container_id': cid, 'image_id': config['postgres_image_id']})
        self.docker(['start', cid])
        return cid

    def postgres(self, cid, expected_image):
        actual = json.loads(self.docker(['inspect', '--format',
            '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},'
            '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
            '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
            '"ports":{{json .HostConfig.PortBindings}},"network_mode":{{json .HostConfig.NetworkMode}},'
            '"restart":{{json .HostConfig.RestartPolicy.Name}},"mounts":{{json .Mounts}}}', cid]))
        require(actual['id'] == cid and actual['image'] == expected_image and actual['running'] is True
                and actual['project'] == 'map-prod' and actual['service'] == 'postgres' and not actual['ports']
                and actual['network_mode'] == NETWORK and actual['restart'] == 'no', 'postgres_identity_drift')
        mounts = actual['mounts']
        require(any(item['Type'] == 'bind' and item['Source'] == str(PGDATA) and
                    item['Destination'] == '/var/lib/postgresql/data' and item['RW'] is True for item in mounts),
                'postgres_data_mount_drift')
        network = json.loads(self.docker(['network', 'inspect', NETWORK]))[0]
        require(network['Internal'] is True and network['Driver'] == 'bridge' and
                network['EnableIPv6'] is False and network['Options'].get(migration.GATEWAY_OPTION) == 'isolated'
                and network['Labels'] == {migration.LABEL: 'user-migration-v1', 'kr.mapservice.project': 'map-prod'}
                and set(network.get('Containers') or {}) == {cid}, 'postgres_network_drift')

    def create_database(self, cid, marker):
        deadline = time.monotonic() + 90
        while True:
            try:
                ready = self.docker(['exec', cid, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'postgres'])
                if ready:
                    break
            except ReceiverError:
                pass
            require(time.monotonic() < deadline, 'postgres_first_start_deadline')
            time.sleep(1)
        sql = ("\\set ON_ERROR_STOP on\nCREATE DATABASE map_prod TEMPLATE template0;\n"
               "COMMENT ON DATABASE map_prod IS 'map-user-bootstrap:v1:" + marker + "';\n")
        self.docker(['exec', '-i', cid, 'psql', '-X', '-U', 'postgres', '-d', 'postgres'], payload=sql)
        result = self.docker(['exec', cid, 'psql', '-XAt', '-U', 'postgres', '-d', 'map_prod', '-c',
            "SELECT current_database() || '|' || current_setting('server_version_num') || '|' || "
            "shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"])
        row = result.split('|')
        require(len(row) == 3 and row[0] == 'map_prod' and row[1].isdigit() and int(row[1]) >= 170000 and
                row[2] == 'map-user-bootstrap:v1:' + marker, 'new_database_identity_failed')


def prepare(config, *, backend=None, state=STATE):
    config = configuration(config); backend = backend or Backend()
    backend.verify(config)
    private_dir(state)
    with writer(state):
        enrollment, contract, manifest = backend.verify(config)
        for filename in ('first-install-attempt.json', 'new-host-proof.json', 'bootstrap-request.json'):
            require(not (state / filename).exists() and not (state / filename).is_symlink(),
                    'prior_attempt_requires_hold')
        backend.empty()
        secrets = backend.secrets()
        proof = {'schema_version': 1, 'environment': 'prod', 'project': 'map-prod',
                 'machine_id': enrollment['machine_id'], 'instance_id': enrollment['instance_id'],
                 'data_uuid': enrollment['data_uuid'], 'database': 'map_prod',
                 'empty_docker_containers': True, 'empty_docker_volumes': True, 'new_data_path': str(PGDATA),
                 'enrollment_sha256': config['enrollment_sha256'], 'runtime_contract_sha256': canonical(config),
                 'marker_sha256': hashlib.sha256(secrets['USER_BOOTSTRAP_MARKER'].encode()).hexdigest(),
                 'observed_at': now()}
        write_once(state / 'first-install-attempt.json', {'status': 'HOLD', 'started_at': now(),
                   'runtime_contract_sha256': canonical(config), 'automatic_retry_permitted': False})
        write_once(state / 'new-host-proof.json', proof)
        cid = backend.prepare_postgres(contract, config)
        backend.postgres(cid, config['postgres_image_id'])
        backend.create_database(cid, secrets['USER_BOOTSTRAP_MARKER'])
        backend.postgres(cid, config['postgres_image_id'])
        request = identity_request(config, enrollment, contract, manifest, proof, cid)
        write_once(state / 'bootstrap-request.json', request)
        return {'status': 'BOOTSTRAP_REQUIRED', 'request_sha256': canonical(request),
                'new_host_proof_sha256': canonical(proof), 'public_serving': 'HOLD', 'postgres_created': True}


def accept_bootstrap(config, *, backend=None, state=STATE):
    config = configuration(config); backend = backend or Backend()
    backend.verify(config)
    private_dir(state)
    with writer(state):
        enrollment, contract, manifest = backend.verify(config)
        request = private.read_json(state / 'bootstrap-request.json', private=True)
        proof = private.read_json(state / 'new-host-proof.json', private=True)
        expected = identity_request(config, enrollment, contract, manifest, proof, request['postgres']['container_id'])
        expected['created_at'] = request['created_at']
        require(request == expected, 'bootstrap_request_drift')
        receipt = private.read_json(state / 'bootstrap-receipt.json', private=True)
        verify_bootstrap_receipt(receipt, request)
        backend.postgres(request['postgres']['container_id'], config['postgres_image_id'])
        result = {'status': 'BOOTSTRAP_ACCEPTED', 'request_sha256': canonical(request),
                  'bootstrap_receipt_sha256': canonical(receipt), 'public_serving': 'HOLD',
                  'normal_service_migrations_required': ['user', 'hub', 'agent']}
        write_once(state / 'bootstrap-accepted.json', result)
        return result


def migrate_user(config, *, backend=None, state=STATE):
    """Run the unchanged normal migrator only after finalization acceptance."""
    config = configuration(config); backend = backend or Backend()
    backend.verify(config)
    private_dir(state)
    with writer(state):
        backend.verify(config)
        request = private.read_json(state / 'bootstrap-request.json', private=True)
        receipt = private.read_json(state / 'bootstrap-receipt.json', private=True)
        verify_bootstrap_receipt(receipt, request)
        accepted = private.read_json(state / 'bootstrap-accepted.json', private=True)
        require(request['runtime_contract_sha256'] == canonical(config) and
                accepted == {'status': 'BOOTSTRAP_ACCEPTED', 'request_sha256': canonical(request),
                             'bootstrap_receipt_sha256': canonical(receipt), 'public_serving': 'HOLD',
                             'normal_service_migrations_required': ['user', 'hub', 'agent']},
                'bootstrap_acceptance_required')
        backend.postgres(request['postgres']['container_id'], config['postgres_image_id'])
        with private.open_read(DATA / 'secrets/USER_DATABASE_PASSWORD', private=True) as stream:
            password = stream.read(66).decode('ascii').removesuffix('\n')
        require(re.fullmatch(r'[a-f0-9]{64}', password), 'runtime_secret_invalid')
        service = migration.service_contract('user')
        credentials = migration.read_credentials(service, DATA / 'secrets/user-migration.env')
        serving = {'name': 'map-prod', 'x-map-production': {
            'environment': 'prod', 'database': 'map_prod',
            'postgres_container_id': request['postgres']['container_id'],
            'postgres_image_id': config['postgres_image_id']}, 'services': {'user': {
                'image': request['user']['image'], 'environment': {
                    'USER_DATABASE_USER': 'map_user_runtime', 'USER_DATABASE_PASSWORD': password,
                    'POSTGRES_DB': 'map_prod', 'POSTGRES_HOST': 'postgres', 'POSTGRES_PORT': '5432'}}}}
        migration.contract(service, serving, credentials)
        scratch = private_dir(state / 'migrations'); receipts = private_dir(state / 'receipts')
        write_once(state / 'user-normal-migrations-attempt.json', {
            'status': 'HOLD', 'request_sha256': canonical(request), 'automatic_retry_permitted': False})
        with migration.job_lock(service, scratch), migration.bounded_signals():
            for operation in ('migrate', 'validate'):
                result = migration.run_job(service, serving, credentials, operation, scratch)
                require(result['image_id'] == request['user']['image_id'], 'user_migration_image_drift')
                write_once(receipts / ('user-normal-' + operation + '.json'), result)
        backend.postgres(request['postgres']['container_id'], config['postgres_image_id'])
        result = {'status': 'USER_NORMAL_MIGRATIONS_COMPLETE', 'request_sha256': canonical(request),
                  'public_serving': 'HOLD', 'remaining_service_migrations': ['hub', 'agent']}
        write_once(state / 'user-normal-migrations-complete.json', result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('verify', 'prepare', 'accept-bootstrap', 'migrate-user'))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        require(os.geteuid() == 0, 'root_receiver_required')
        config = configuration(private.read_json(CONFIG, private=True))
        if args.phase == 'verify':
            Backend().verify(config)
            result = {'status': 'RECEIVER_INPUTS_VERIFIED', 'public_serving': 'HOLD', 'mutations': 0}
        elif args.phase == 'prepare':
            result = prepare(config)
        elif args.phase == 'accept-bootstrap':
            result = accept_bootstrap(config)
        else:
            result = migrate_user(config)
        print(json.dumps(result, sort_keys=True)); return 0
    except Exception as error:
        print(json.dumps({'status': 'HOLD', 'public_serving': 'HOLD', 'automatic_retry_permitted': False,
                          'error_code': str(error) if isinstance(error, ReceiverError) else 'receiver_guard_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
