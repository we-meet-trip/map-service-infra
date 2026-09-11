#!/usr/bin/env python3
"""Provision PostGIS and Hub/Agent on this receiver's new map_prod only.

Original service SQL and immutable image migrators are consumed from exact clean
source checkouts. No application, Admin role, exporter, or public ingress starts.
An uncertain phase cannot be automatically replayed; real data is never deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    try:
        if ROOT != Path('/opt/map-service-infra') or os.geteuid() != 0:
            raise RuntimeError()
        paths = [ROOT, *ROOT.parents]
        for directory, dirs, files in os.walk(ROOT, followlinks=False):
            paths.extend(Path(directory) / name for name in dirs + files)
        for path in paths:
            item = path.lstat()
            if (item.st_uid != 0 or item.st_mode & 0o022 or
                    not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode)) or
                    stat.S_ISREG(item.st_mode) and item.st_nlink != 1):
                raise RuntimeError()
    except Exception:
        print(json.dumps({'status': 'HOLD', 'public_serving': 'HOLD', 'error_code': 'root_source_checkout_untrusted'}))
        raise SystemExit(1)
spec = importlib.util.spec_from_file_location('ncp_database_receiver', ROOT / 'scripts/ncp-production-receiver.py')
receiver = importlib.util.module_from_spec(spec); spec.loader.exec_module(receiver)
require = receiver.require
DATA = receiver.DATA
STATE = receiver.STATE
SOURCES = {'hub': Path('/opt/map-service-hub'), 'agent': Path('/opt/map-service-agent')}
SECRET_NAMES = ('HUB_DATABASE_PASSWORD', 'HUB_MIGRATION_PASSWORD',
                'AGENT_DATABASE_PASSWORD', 'AGENT_MIGRATION_PASSWORD')
SCRAM_HOST_GUARD = """SELECT NOT EXISTS(SELECT 1 FROM pg_hba_file_rules WHERE error IS NOT NULL)
 AND NOT EXISTS(SELECT 1 FROM pg_hba_file_rules WHERE type LIKE 'host%' AND auth_method<>'scram-sha-256'
 AND NOT ((address='127.0.0.1' AND netmask='255.255.255.255')
       OR (address='::1' AND netmask='ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff')));"""

# The returned value is a catalog digest, never a User row or credential.
FINGERPRINT_SQL = """
SELECT encode(sha256(convert_to(jsonb_build_object(
 'schema',(SELECT jsonb_build_array(nspowner,nspacl) FROM pg_namespace WHERE nspname='user_service'),
 'relations',(SELECT jsonb_agg(jsonb_build_array(c.relname,c.relkind,c.relowner,c.relacl) ORDER BY c.relname)
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='user_service'),
 'routines',(SELECT jsonb_agg(jsonb_build_array(p.oid,p.proowner,p.proacl) ORDER BY p.oid)
   FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='user_service'),
 'roles',(SELECT jsonb_agg(jsonb_build_array(rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,
       rolreplication,rolbypassrls,rolinherit,rolvaliduntil,rolconfig,
       has_database_privilege(oid,current_database(),'CONNECT'),
       has_database_privilege(oid,current_database(),'CREATE'),has_database_privilege(oid,current_database(),'TEMP'),
       has_schema_privilege(oid,'public','USAGE'),has_schema_privilege(oid,'public','CREATE')) ORDER BY rolname)
   FROM pg_roles WHERE rolname IN ('map_user_bootstrap','map_user_owner','map_user_migrator','map_user_runtime')),
 'credential_verifiers',(SELECT jsonb_agg(jsonb_build_array(rolname,md5(rolpassword)) ORDER BY rolname)
   FROM pg_authid WHERE rolname IN ('map_user_bootstrap','map_user_owner','map_user_migrator','map_user_runtime')),
 'memberships',(SELECT jsonb_agg(jsonb_build_array(roleid,member,admin_option,inherit_option,set_option) ORDER BY roleid,member)
   FROM pg_auth_members WHERE member IN (SELECT oid FROM pg_roles WHERE rolname IN ('map_user_bootstrap','map_user_owner','map_user_migrator','map_user_runtime'))
      OR roleid IN (SELECT oid FROM pg_roles WHERE rolname IN ('map_user_bootstrap','map_user_owner','map_user_migrator','map_user_runtime'))),
 'defaults',(SELECT jsonb_agg(jsonb_build_array(defaclrole,defaclnamespace,defaclobjtype,defaclacl) ORDER BY oid)
   FROM pg_default_acl WHERE defaclrole IN (SELECT oid FROM pg_roles WHERE rolname IN ('map_user_bootstrap','map_user_owner','map_user_migrator','map_user_runtime'))),
 'history',(SELECT jsonb_agg(to_jsonb(h) ORDER BY installed_rank) FROM user_service.flyway_schema_history h)
)::text,'UTF8')),'hex');
"""

EMPTY_USER_SQL = """
DO $empty_user$
DECLARE item record; has_rows boolean;
BEGIN
 FOR item IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='user_service' AND c.relkind IN ('r','p') AND c.relname<>'flyway_schema_history'
 LOOP
  EXECUTE format('SELECT EXISTS(SELECT 1 FROM user_service.%I LIMIT 1)',item.relname) INTO has_rows;
  IF has_rows THEN RAISE EXCEPTION 'new installation has User rows'; END IF;
 END LOOP;
END
$empty_user$;
"""


def serving_config(request, images, secrets, service):
    if service == 'hub':
        environment = {'HUB_DATABASE_URL': 'postgresql+psycopg://map_hub_runtime:' +
                       secrets['HUB_DATABASE_PASSWORD'] + '@postgres:5432/map_prod'}
        credentials = {'HUB_MIGRATION_DATABASE_URL': 'postgresql+psycopg://map_hub_migrator:' +
                       secrets['HUB_MIGRATION_PASSWORD'] + '@postgres:5432/map_prod'}
    else:
        require(service == 'agent', 'production_service_invalid')
        environment = {'POSTGRES_USER': 'map_agent_runtime', 'POSTGRES_PASSWORD': secrets['AGENT_DATABASE_PASSWORD'],
                       'POSTGRES_HOST': 'postgres', 'POSTGRES_PORT': '5432', 'POSTGRES_DB': 'map_prod',
                       'LANGGRAPH_SCHEMA': 'langgraph'}
        credentials = {'AGENT_CHECKPOINT_MIGRATION_DSN': 'postgresql://map_agent_migrator:' +
                       secrets['AGENT_MIGRATION_PASSWORD'] + '@postgres:5432/map_prod'}
    return {'name': 'map-prod', 'x-map-production': {'environment': 'prod', 'database': 'map_prod',
                'postgres_container_id': request['postgres']['container_id'],
                'postgres_image_id': request['postgres']['image_id']},
            'services': {service: {'image': images[service], 'environment': environment}}}, credentials


class Backend(receiver.Backend):
    def verify_phase(self, config, state):
        enrollment, contract, manifest = self.verify(config)
        request = receiver.private.read_json(state / 'bootstrap-request.json', private=True)
        proof = receiver.private.read_json(state / 'new-host-proof.json', private=True)
        expected = receiver.identity_request(config, enrollment, contract, manifest, proof, request['postgres']['container_id'])
        expected['created_at'] = request['created_at']
        require(request == expected, 'database_phase_request_drift')
        receiver.verify_bootstrap_receipt(receiver.private.read_json(state / 'bootstrap-receipt.json', private=True), request)
        complete = receiver.private.read_json(state / 'user-normal-migrations-complete.json', private=True)
        require(complete == {'status': 'USER_NORMAL_MIGRATIONS_COMPLETE', 'request_sha256': receiver.canonical(request),
                            'public_serving': 'HOLD', 'remaining_service_migrations': ['hub', 'agent']},
                'normal_user_migrations_required')
        for operation in ('migrate', 'validate'):
            receipt = receiver.private.read_json(state / 'receipts' / ('user-normal-' + operation + '.json'), private=True)
            require(receipt['status'] == 'PASS' and receipt['project'] == 'map-prod' and receipt['service'] == 'user'
                    and receipt['image'] == request['user']['image'] and receipt['image_id'] == request['user']['image_id']
                    and receipt['operation'] == operation and receipt['database_container_preserved'] is True
                    and receipt['temporary_job_removed'] is True and receipt['network_database_only_after_job'] is True,
                    'normal_user_migration_receipt_invalid')
        self.postgres(request['postgres']['container_id'], request['postgres']['image_id'])
        require(self.docker(['ps', '-aq', '--no-trunc']).splitlines() == [request['postgres']['container_id']]
                and not self.docker(['volume', 'ls', '-q']), 'new_database_host_only')
        sql = {}; images = {}; source_receipts = {}
        for service, path in SOURCES.items():
            receiver.source_ownership(path, required_path=path)
            expected_sha = manifest['services'][service]['source_sha']
            git = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-C', str(path)]
            require(self.command(git + ['rev-parse', '--show-toplevel']) == str(path)
                    and self.command(git + ['rev-parse', 'HEAD']) == expected_sha
                    and not self.command(git + ['status', '--porcelain', '--untracked-files=no']),
                    'database_role_source_mismatch')
            source = path / 'docs/database-roles.sql'
            raw = receiver.host.regular(source)
            sql[service] = raw.decode('utf-8')
            images[service] = contract['images'][service]['image']
            image_id = self.docker(['image', 'inspect', '--format', '{{.Id}}', images[service]])
            require(receiver.IMAGE.fullmatch(image_id), 'database_service_image_missing')
            source_receipts[service] = {'source_sha': expected_sha, 'role_sql_sha256': hashlib.sha256(raw).hexdigest(),
                                        'image': images[service], 'image_id': image_id}
        return {'request': request, 'role_sql': sql, 'images': images, 'sources': source_receipts}

    def sql(self, request, text, *, password=None, role='postgres'):
        require(role in ('postgres', 'map_hub_runtime', 'map_agent_runtime'), 'database_operator_role_invalid')
        args = ['exec', '-i', request['postgres']['container_id']]
        prefix = "\\set ON_ERROR_STOP on\nSET standard_conforming_strings=on; SET statement_timeout='180s'; SET lock_timeout='10s';\n"
        if role == 'postgres':
            args += ['psql', '-XqAt', '-U', 'postgres', '-d', 'map_prod']
            payload = prefix + text
        else:
            require(isinstance(password, str) and receiver.HEX.fullmatch(password), 'database_secret_invalid')
            args += ['/bin/sh', '-c', 'IFS= read -r PGPASSWORD; export PGPASSWORD; '
                     'exec psql -XqAt -h postgres -U ' + role + ' -d map_prod']
            payload = password + '\n' + prefix + text
        return self.docker(args, payload=payload, timeout=200)

    def secrets(self):
        result = {}
        names = SECRET_NAMES + ('POSTGRES_PASSWORD', 'USER_DATABASE_PASSWORD', 'USER_MIGRATION_PASSWORD', 'USER_BOOTSTRAP_MARKER')
        for name in names:
            with receiver.private.open_read(DATA / 'secrets' / name, private=True) as stream:
                value = stream.read(66).decode('ascii').removesuffix('\n')
            require(receiver.HEX.fullmatch(value), 'independent_database_secrets_required')
            result[name] = value
        require(len(set(result.values())) == len(result), 'database_secret_reuse')
        return result

    def fresh_guard(self, request, secrets):
        require(hashlib.sha256(secrets['USER_BOOTSTRAP_MARKER'].encode()).hexdigest() == request['marker_sha256'],
                'database_marker_mismatch')
        sql = """SELECT current_database()='map_prod' AND current_user='postgres'
          AND current_setting('server_version_num')::integer>=170000
          AND shobj_description(oid,'pg_database')='map-user-bootstrap:v1:""" + secrets['USER_BOOTSTRAP_MARKER'] + """'
          AND NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN
             ('map_hub_owner','map_hub_migrator','map_hub_runtime','map_agent_owner','map_agent_migrator','map_agent_runtime'))
          AND NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('user_service','public','information_schema')
                         AND nspname NOT LIKE 'pg\\_%' ESCAPE '\\')
          AND NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname<>'plpgsql')
          AND NOT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid())
          FROM pg_database WHERE datname=current_database();"""
        require(self.sql(request, sql) == 't', 'fresh_post_user_database_required')
        require(self.sql(request, SCRAM_HOST_GUARD) == 't', 'scram_host_authentication_required')
        self.sql(request, EMPTY_USER_SQL)
        return self.fingerprint(request)

    def fingerprint(self, request):
        value = self.sql(request, FINGERPRINT_SQL)
        require(receiver.HEX.fullmatch(value), 'user_catalog_fingerprint_invalid')
        return value

    def extension(self, request):
        self.sql(request, 'CREATE EXTENSION postgis WITH SCHEMA public;')
        require(self.sql(request, "SELECT ST_IsValid(ST_GeomFromText('POINT(127 37)',4326)) AND "
                                  "ST_SRID(ST_GeomFromText('POINT(127 37)',4326))=4326;") == 't',
                'postgis_operator_probe_failed')

    def roles(self, request, sql):
        self.sql(request, sql)  # Original reviewed service SQL, without rewriting.

    def logins(self, request, secrets):
        # User's empty-host prepare revoked PUBLIC schema USAGE. Hub's owner
        # needs explicit USAGE for PostGIS types/functions during migrations;
        # public CREATE and every User privilege remain untouched.
        rows = ['SET password_encryption=\'scram-sha-256\';',
                'GRANT USAGE ON SCHEMA public TO map_hub_owner;']
        for service in ('hub', 'agent'):
            for role, key in (('runtime', service.upper() + '_DATABASE_PASSWORD'),
                              ('migrator', service.upper() + '_MIGRATION_PASSWORD')):
                rows.append('ALTER ROLE map_' + service + '_' + role + " LOGIN PASSWORD '" + secrets[key] + "';")
        self.sql(request, 'BEGIN;\n' + '\n'.join(rows) + '\nCOMMIT;')

    def hub_scope(self, request, enabled):
        # Only this already proven first install may temporarily satisfy the
        # unchanged 0001 CREATE SCHEMA permission check. Always revoke in finally.
        sql = ("BEGIN; DO $expiry$ BEGIN EXECUTE format('ALTER ROLE map_hub_migrator VALID UNTIL %L',"
               "clock_timestamp()+interval '20 minutes'); END; $expiry$; "
               "GRANT CREATE ON DATABASE map_prod TO map_hub_owner; COMMIT;" if enabled else
               "BEGIN; REVOKE CREATE ON DATABASE map_prod FROM map_hub_owner; "
               "ALTER ROLE map_hub_migrator VALID UNTIL 'infinity'; COMMIT;")
        self.sql(request, sql)
        expected = 't' if enabled else 'f'
        require(self.sql(request, "SELECT has_database_privilege('map_hub_owner','map_prod','CREATE');") == expected,
                'hub_initial_database_scope_failed')

    def migrate(self, inputs, secrets, service, state):
        config, credentials = serving_config(inputs['request'], inputs['images'], secrets, service)
        item = receiver.migration.service_contract(service)
        scratch = receiver.private_dir(state / 'migrations')
        with receiver.migration.job_lock(item, scratch):
            result = receiver.migration.run_job(item, config, credentials, 'migrate', scratch)
        require(result['image_id'] == inputs['sources'][service]['image_id'], 'migration_image_changed')
        return result

    def runtime_probe(self, request, secrets, service):
        schema = 'hub_data' if service == 'hub' else 'langgraph'
        history = 'alembic_version' if service == 'hub' else 'checkpoint_migrations'
        data = 'places' if service == 'hub' else 'checkpoints'
        role = 'map_' + service + '_runtime'; owner = 'map_' + service + '_owner'
        sql = ("SELECT current_user='" + role + "' AND session_user=current_user AND NOT rolsuper AND NOT rolcreatedb "
               "AND inet_server_addr() IS NOT NULL AND NOT (inet_server_addr()<<inet'127.0.0.0/8') "
               "AND inet_server_addr()<>inet'::1' "
               "AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls AND NOT rolinherit "
               "AND NOT has_database_privilege(current_database(),'CREATE,TEMP') "
               "AND NOT has_schema_privilege('" + schema + "','CREATE') AND NOT has_schema_privilege('public','CREATE') "
               "AND NOT pg_has_role(current_user,'" + owner + "','MEMBER') "
               "AND has_table_privilege('" + schema + "." + history + "','SELECT') "
               "AND NOT has_table_privilege('" + schema + "." + history + "','INSERT,UPDATE,DELETE') "
               + ''.join("AND has_table_privilege('" + schema + "." + data + "','" + action + "') "
                         for action in ('SELECT', 'INSERT', 'UPDATE', 'DELETE')) +
               "AND NOT has_table_privilege((SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
               "WHERE n.nspname='user_service' AND c.relname='users'),'SELECT,INSERT,UPDATE,DELETE') "
               "FROM pg_roles WHERE rolname=current_user;")
        require(self.sql(request, sql, password=secrets[service.upper() + '_DATABASE_PASSWORD'], role=role) == 't',
                'runtime_database_probe_failed')
        if service == 'hub':
            require(self.sql(request, "SELECT ST_DWithin(ST_SetSRID(ST_MakePoint(127,37),4326)::geography, "
                "ST_SetSRID(ST_MakePoint(127,37),4326)::geography,1);", password=secrets['HUB_DATABASE_PASSWORD'], role=role) == 't',
                'hub_postgis_runtime_probe_failed')

    def preserved(self, request, before):
        self.sql(request, EMPTY_USER_SQL)
        require(self.fingerprint(request) == before, 'user_contract_changed')
        require(self.sql(request, "SELECT NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN "
                    "('map_hub_owner','map_agent_owner','map_hub_runtime','map_agent_runtime','map_hub_migrator','map_agent_migrator') "
                    "AND has_database_privilege(oid,'map_prod','CREATE,TEMP'));") == 't', 'database_create_or_temp_not_revoked')
        self.postgres(request['postgres']['container_id'], request['postgres']['image_id'])


def execute(config, *, backend=None, state=STATE):
    config = receiver.configuration(config); backend = backend or Backend()
    backend.verify_phase(config, state)
    with receiver.writer(state), receiver.migration.bounded_signals():
        inputs = backend.verify_phase(config, state)
        request = inputs['request']; secrets = backend.secrets()
        attempt = state / 'database-provision-attempt.json'
        require(not attempt.exists() and not attempt.is_symlink(), 'prior_database_attempt_requires_hold')
        for service in ('hub', 'agent'):
            path = DATA / 'secrets' / (service + '-migration.env')
            require(not path.exists() and not path.is_symlink(), 'existing_migration_credentials_require_hold')
        before = backend.fresh_guard(request, secrets)
        receiver.write_once(attempt, {'status': 'HOLD', 'request_sha256': receiver.canonical(request),
                                     'sources': inputs['sources'], 'automatic_retry_permitted': False,
                                     'user_catalog_before': before, 'started_at': receiver.now()})
        backend.extension(request)
        for service in ('hub', 'agent'): backend.roles(request, inputs['role_sql'][service])
        backend.logins(request, secrets)
        receipts = receiver.private_dir(state / 'receipts')
        try:
            # Set the finalizer before granting; even a lost GRANT response runs
            # the exact-target revocation. A hard host kill still leaves HOLD.
            backend.hub_scope(request, True)
            first = backend.migrate(inputs, secrets, 'hub', state)
            receiver.write_once(receipts / 'hub-initial-migration.json', first)
        finally:
            backend.hub_scope(request, False)
        backend.roles(request, inputs['role_sql']['hub'])
        second = backend.migrate(inputs, secrets, 'hub', state)
        receiver.write_once(receipts / 'hub-normal-migration.json', second)
        backend.roles(request, inputs['role_sql']['hub'])
        agent = backend.migrate(inputs, secrets, 'agent', state)
        receiver.write_once(receipts / 'agent-normal-migration.json', agent)
        backend.roles(request, inputs['role_sql']['agent'])
        for service in ('hub', 'agent'): backend.runtime_probe(request, secrets, service)
        backend.preserved(request, before)
        for service in ('hub', 'agent'):
            _, credentials = serving_config(request, inputs['images'], secrets, service)
            receiver.host.write_once(DATA / 'secrets' / (service + '-migration.env'),
                                     ''.join(key + '=' + value + '\n' for key, value in credentials.items()).encode())
        result = {'status': 'PRODUCTION_DATABASE_PREPARED', 'request_sha256': receiver.canonical(request),
                  'sources': inputs['sources'], 'user_catalog_preserved': True,
                  'hub_initial_database_create_revoked': True, 'runtime_probes': {'hub': 'PASS', 'agent': 'PASS'},
                  'public_serving': 'HOLD', 'admin_exporter_roles_created': False, 'completed_at': receiver.now()}
        receiver.write_once(state / 'database-provision-complete.json', result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.parse_args(argv)
    os.umask(0o077)
    try:
        require(os.geteuid() == 0, 'root_database_operator_required')
        config = receiver.configuration(receiver.private.read_json(receiver.CONFIG, private=True))
        result = execute(config)
        print(json.dumps(result, sort_keys=True)); return 0
    except Exception as error:
        print(json.dumps({'status': 'HOLD', 'public_serving': 'HOLD', 'automatic_retry_permitted': False,
                          'error_code': str(error) if isinstance(error, receiver.ReceiverError) else 'database_provision_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
