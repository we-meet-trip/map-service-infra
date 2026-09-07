"""Hosted synthetic PG exporter fixture. Parent Sandbox owns all cleanup.

Interface: check(existing_sandbox, exact_candidate_config_digest) -> JSON checks.
Requires acad675-or-later Sandbox.create_network/networks/owned network cleanup;
the parent dispatcher must call this helper for postgres-exporter.
No serving DB, existing volume, provider API, or credential input is accepted.
The official PG17.11 helper is a test dependency, not a new release approval.

Contracts:
https://www.postgresql.org/docs/17/predefined-roles.html
https://www.postgresql.org/docs/17/auth-password.html
https://github.com/prometheus-community/postgres_exporter/blob/867fbcac31cd18c143e244190ea9168cca069827/collector/pg_stat_database.go
"""
import json
import math
import os
import re
import sys
import time
import uuid
from urllib.parse import urlsplit

PG_IMAGE = 'postgres@sha256:7bade6d532592ca8ce7ee32def7399dad2607c4ea5583839fc4352a095a11ea6'
PG_CONFIG = 'sha256:a2ea0e68c465e0acf4c3672471b22b6b62972bb341e6f31544c855d85ba43745'
PG_VERSION_NUM = 170011
DATABASE = 'map_fixture'
OWNER = 'fixture_owner'
MONITOR = 'map_metrics'
PG_ALIAS = 'map-fixture-pg'
DATA_PATH = '/var/lib/postgresql/data'


class FixtureError(ValueError):
    """Stable error codes only; passwords and raw command errors stay private."""


def require(ok, reason):
    if not ok:
        raise FixtureError(reason)


def run(s, args, phase, **kwargs):
    try:
        return s.run(args, **kwargs)
    except Exception:
        raise FixtureError('pg_exporter_command_failed:' + phase) from None


def parsed(raw, phase):
    try:
        return json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        raise FixtureError('pg_exporter_invalid_json:' + phase) from None


def origin_only(origin):
    try:
        url = urlsplit(origin)
        valid = (url.scheme == 'http' and url.hostname == '127.0.0.1' and
                 url.port is not None and 0 < url.port < 65536 and
                 not url.username and not url.password and not url.path and
                 not url.query and not url.fragment)
    except (ValueError, TypeError):
        valid = False
    require(valid, 'pg_exporter_loopback_origin_required')


def owned_network(s, network):
    expected = 'map-infra-' + s.token + '-pg-exporter-db'
    require(network == expected and network in s.networks, 'pg_exporter_foreign_network')
    rows = parsed(run(s, ['docker', 'network', 'inspect', network], 'network-inspect'), 'network')
    require(isinstance(rows, list) and len(rows) == 1, 'pg_exporter_network_inspect')
    meta = rows[0]
    require(meta.get('Name') == network and meta.get('Internal') is True and
            meta.get('Labels', {}).get('map.infra.fixture') == s.token,
            'pg_exporter_network_not_owned_internal')


def owned_container(s, name, network, image_id, volume=None):
    require(name in s.containers and name.startswith('map-infra-' + s.token + '-'),
            'pg_exporter_foreign_container')
    rows = parsed(run(s, ['docker', 'inspect', name], 'container-inspect'), 'container')
    require(isinstance(rows, list) and len(rows) == 1, 'pg_exporter_container_inspect')
    meta = rows[0]
    require(meta.get('Name') == '/' + name and meta.get('Image') == image_id and
            meta.get('Config', {}).get('Labels', {}).get('map.infra.fixture') == s.token,
            'pg_exporter_container_identity')
    require(set(meta.get('NetworkSettings', {}).get('Networks', {})) == {network},
            'pg_exporter_container_foreign_network')
    mounts = meta.get('Mounts', [])
    if volume:
        require(volume in s.volumes and len(mounts) == 1 and
                mounts[0].get('Type') == 'volume' and mounts[0].get('Name') == volume and
                mounts[0].get('Destination') == DATA_PATH, 'pg_exporter_foreign_volume')
        rows = parsed(run(s, ['docker', 'volume', 'inspect', volume], 'volume-inspect'), 'volume')
        require(isinstance(rows, list) and len(rows) == 1 and rows[0].get('Name') == volume and
                rows[0].get('Labels', {}).get('map.infra.fixture') == s.token,
                'pg_exporter_volume_not_owned')
    else:
        require(not mounts, 'pg_exporter_unexpected_mount')


def psql(s, container, sql, phase, password=None):
    args = ['docker', 'exec', '-i']
    if password is not None:
        args += ['-e', 'PGPASSWORD=' + password, '-e', 'PGCONNECT_TIMEOUT=3']
    args += [container, 'psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-d', DATABASE]
    args += ['-U', OWNER] if password is None else ['-h', '127.0.0.1', '-p', '5432', '-U', MONITOR]
    return run(s, args, phase, input=sql.encode(), timeout=30)


def role_checks(admin, connected):
    require(isinstance(admin, dict) and isinstance(connected, dict), 'pg_exporter_role_evidence_missing')
    role = admin.get('role', {})
    require(admin.get('server_version_num') == PG_VERSION_NUM, 'pg_exporter_pg_version_mismatch')
    require(admin.get('password_encryption') == 'scram-sha-256' and admin.get('scram_stored') is True,
            'pg_exporter_scram_storage_required')
    methods = admin.get('host_auth_methods')
    require(isinstance(methods, list) and methods and all(x == 'scram-sha-256' for x in methods),
            'pg_exporter_scram_host_auth_required')
    require(role.get('rolname') == MONITOR and role.get('rolcanlogin') is True and
            role.get('rolinherit') is True, 'pg_exporter_monitor_login_role')
    require(all(role.get(flag) is False for flag in
                ('rolsuper', 'rolcreatedb', 'rolcreaterole', 'rolreplication', 'rolbypassrls')),
            'pg_exporter_privileged_role_rejected')
    require(admin.get('direct_memberships') == ['pg_monitor'], 'pg_exporter_role_membership_escape')
    require(connected.get('current_user') == MONITOR and connected.get('session_user') == MONITOR and
            connected.get('database') == DATABASE and connected.get('tcp') is True and
            connected.get('pg_monitor_member') is True, 'pg_exporter_monitor_tcp_identity')
    require(connected.get('public_schema_create_privilege') is False and
            connected.get('database_create_privilege') is False,
            'pg_exporter_monitor_ddl_privilege')


def metrics_checks(raw):
    require(isinstance(raw, bytes) and len(raw) <= 4 * 1024 * 1024, 'pg_exporter_metrics_size')
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise FixtureError('pg_exporter_metrics_encoding') from None
    names = {'pg_up', 'pg_exporter_last_scrape_error', 'pg_stat_database_numbackends'}
    samples = {name: [] for name in names}
    metric = re.compile(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+([^\s]+)(?:\s+\d+)?\Z')
    label = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\[\\"n])*)"(?:,|$)')
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        match = metric.fullmatch(line)
        if not match:
            require(not any(line.startswith(name) for name in names), 'pg_exporter_metric_malformed')
            continue
        name, labels_raw, value_raw = match.groups()
        if name not in names:
            continue
        try:
            value = float(value_raw)
        except ValueError:
            raise FixtureError('pg_exporter_metric_value') from None
        require(math.isfinite(value), 'pg_exporter_metric_nonfinite')
        labels = {}
        pos = 0
        while labels_raw and pos < len(labels_raw):
            item = label.match(labels_raw, pos)
            require(item is not None, 'pg_exporter_metric_labels')
            require(item[1] not in labels, 'pg_exporter_metric_duplicate_label')
            labels[item[1]] = item[2]
            pos = item.end()
        samples[name].append((labels, value))
    require(samples['pg_up'] and all(value == 1 for _, value in samples['pg_up']),
            'pg_exporter_pg_up_not_one')
    require(samples['pg_exporter_last_scrape_error'] and
            all(value == 0 for _, value in samples['pg_exporter_last_scrape_error']),
            'pg_exporter_last_scrape_error_not_zero')
    database_samples = [value for labels, value in samples['pg_stat_database_numbackends']
                        if labels.get('datname') == DATABASE]
    require(len(database_samples) == 1 and database_samples[0] >= 1,
            'pg_exporter_database_stats_missing')
    return {'pg_up': 1, 'pg_exporter_last_scrape_error': 0,
            'pg_stat_database_numbackends': database_samples[0], 'database': DATABASE}


def check(s, candidate):
    require(os.environ.get('GITHUB_ACTIONS') == 'true' and
            os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted' and sys.platform == 'linux',
            'pg_exporter_remote_hosted_ci_only')
    require(isinstance(getattr(s, 'token', None), str) and re.fullmatch(r'[a-f0-9]{12}', s.token),
            'pg_exporter_sandbox_token')
    require(isinstance(candidate, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', candidate),
            'pg_exporter_exact_candidate_required')
    require(all(callable(getattr(s, name, None)) for name in ('create_network', 'create', 'run', 'wait', 'request'))
            and all(isinstance(getattr(s, name, None), list) for name in ('networks', 'containers', 'volumes')),
            'pg_exporter_sandbox_network_contract_missing')
    network = s.create_network('pg-exporter-db')
    owned_network(s, network)
    volume = 'map-infra-' + s.token + '-pg-exporter-db-data'
    existing = run(s, ['docker', 'volume', 'ls', '--filter', 'name=^' + volume + '$',
                       '--format', '{{.Name}}'], 'fresh-volume-check')
    require(not existing.strip(), 'pg_exporter_volume_already_exists')
    run(s, ['docker', 'pull', '--platform', 'linux/amd64', PG_IMAGE], 'pull-helper', timeout=600)
    owner_password, metrics_password = uuid.uuid4().hex, uuid.uuid4().hex
    options = ('--network', network, '--network-alias', PG_ALIAS,
               '-e', 'POSTGRES_DB=' + DATABASE, '-e', 'POSTGRES_USER=' + OWNER,
               '-e', 'POSTGRES_PASSWORD=' + owner_password,
               '-e', 'POSTGRES_HOST_AUTH_METHOD=scram-sha-256',
               '-e', 'POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=trust')
    pg, pg_origin = s.create(PG_IMAGE, 'pg-exporter-db', 5432, DATA_PATH, extra=options,
                             command=('postgres', '-c', 'password_encryption=scram-sha-256'))
    origin_only(pg_origin)
    owned_container(s, pg, network, PG_CONFIG, volume)
    for attempt in range(60):
        try:
            run(s, ['docker', 'exec', pg, 'pg_isready', '-h', '127.0.0.1', '-U', OWNER, '-d', DATABASE],
                'pg-readiness', timeout=5)
            break
        except FixtureError:
            if attempt == 59:
                raise FixtureError('pg_exporter_pg_readiness_timeout') from None
            time.sleep(1)
    bootstrap = f"""
SET password_encryption = 'scram-sha-256';
REVOKE ALL ON DATABASE {DATABASE} FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE ROLE {MONITOR} LOGIN PASSWORD '{metrics_password}' INHERIT
 NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT pg_monitor TO {MONITOR};
GRANT CONNECT ON DATABASE {DATABASE} TO {MONITOR};
CREATE TABLE public.map_fixture_sample(id integer PRIMARY KEY);
INSERT INTO public.map_fixture_sample VALUES (1);
"""
    psql(s, pg, bootstrap, 'bootstrap')
    admin_sql = f"""SELECT json_build_object(
'server_version_num',current_setting('server_version_num')::int,
'password_encryption',current_setting('password_encryption'),
'role',row_to_json(r),'scram_stored',a.rolpassword LIKE 'SCRAM-SHA-256$%',
'host_auth_methods',(SELECT json_agg(auth_method ORDER BY line_number)
 FROM pg_hba_file_rules WHERE type LIKE 'host%'),
'direct_memberships',(SELECT json_agg(parent.rolname ORDER BY parent.rolname)
 FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid WHERE m.member=r.oid))
FROM (SELECT oid,rolname,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin,rolinherit
 FROM pg_roles WHERE rolname='{MONITOR}') r JOIN pg_authid a ON a.oid=r.oid;
"""
    admin = parsed(psql(s, pg, admin_sql, 'role-audit'), 'role-audit')
    tcp_sql = """SELECT json_build_object('current_user',current_user,'session_user',session_user,
'database',current_database(),'tcp',inet_client_addr() IS NOT NULL,
'pg_monitor_member',pg_has_role(current_user,'pg_monitor','MEMBER'),
'public_schema_create_privilege',has_schema_privilege(current_user,'public','CREATE'),
'database_create_privilege',has_database_privilege(current_user,current_database(),'CREATE'));
"""
    connected = parsed(psql(s, pg, tcp_sql, 'monitor-tcp-auth', metrics_password), 'monitor-tcp-auth')
    role_checks(admin, connected)
    # Each block only succeeds after PostgreSQL returns insufficient_privilege.
    # Unexpected successful DDL raises an exception, rolling that DDL back.
    for ddl in ('CREATE TABLE public.map_fixture_forbidden(id integer)',
                'CREATE SCHEMA map_fixture_forbidden'):
        sql = "DO $fixture$ BEGIN BEGIN " + ddl + "; EXCEPTION WHEN SQLSTATE '42501' THEN RETURN; END; "
        sql += "RAISE EXCEPTION 'unexpected_ddl_success'; END $fixture$;"
        psql(s, pg, sql, 'ddl-denial', metrics_password)
    dsn = f'postgresql://{MONITOR}:{metrics_password}@{PG_ALIAS}:5432/{DATABASE}?sslmode=disable&connect_timeout=3'
    exporter, origin = s.create(candidate, 'pg-exporter-live', 9187,
        extra=('--network', network, '--read-only', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true', '-e', 'DATA_SOURCE_NAME=' + dsn))
    origin_only(origin)
    owned_container(s, exporter, network, candidate)
    try:
        first = s.wait(origin, '/metrics')
        first_metrics = metrics_checks(first)
        second_metrics = metrics_checks(s.request(origin, '/metrics'))
    except FixtureError:
        raise
    except Exception:
        raise FixtureError('pg_exporter_http_scrape_failed') from None
    return {
        'actual_postgresql_scrape': True, 'monitor_scram_tcp_authenticated': True,
        'monitor_privileges_checked': True, 'monitor_table_and_schema_ddl_denied_sqlstate_42501': True,
        'fixture_network_internal': True, 'fresh_owned_postgres_volume': True,
        'postgres_helper_image': PG_IMAGE, 'postgres_server_version_num': admin['server_version_num'],
        'candidate_runtime_image_id': candidate, 'monitor_role': MONITOR,
        'monitor_direct_memberships': admin['direct_memberships'],
        'first_scrape': first_metrics, 'second_scrape': second_metrics,
        'serving_database_credentials_used': False, 'production_data_used': False,
        'host_filesystem_or_docker_socket_mounted': False,
        'cleanup_owner': 'Parent Sandbox.clean; all created resources registered in its lists',
    }
