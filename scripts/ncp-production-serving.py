#!/usr/bin/env python3
"""Start the NCP serving services after the independent database installation.

Never creates, replaces or migrates PostgreSQL. Public admission and reboot
resume use the same private, source-bound inputs; credentials are never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


receiver = module('ncp_serving_receiver', 'ncp-production-receiver.py')
DATA, STATE = receiver.DATA, receiver.STATE
CONFIG = DATA / 'secrets/production-serving.json'
ENV_FILE = DATA / 'secrets/production-serving.env'
PUBLIC = DATA / 'data/public'
PUBLIC_MANIFEST = DATA / 'data/public-manifest.json'
NETWORK = 'map-prod-net'
API = 'https://api.mapservice.app'
SITE = 'https://mapservice.app'
SERVICES = ('redis', 'osrm-foot', 'osrm-bicycle', 'hub', 'agent', 'user', 'yolo', 'proxy', 'edge')
PUBLIC_SERVICES = ('edge', 'proxy', 'user', 'yolo')
PRIVATE_SERVICES = tuple(item for item in SERVICES if item != 'edge')
STATE_NAME = 'serving-state.json'
require = receiver.require


def configuration(value):
    receiver.exact(value, 'schema_version environment runtime_env_sha256 public_manifest_sha256 gemini_key_sha256',
                   'serving_configuration_invalid')
    require(type(value['schema_version']) is int and value['schema_version'] == 1 and
            value['environment'] == 'prod', 'production_serving_required')
    for key in ('runtime_env_sha256', 'gemini_key_sha256'):
        require(isinstance(value[key], str) and receiver.HEX.fullmatch(value[key]), 'serving_input_hash_required')
    require(value['public_manifest_sha256'] is None or
            isinstance(value['public_manifest_sha256'], str) and receiver.HEX.fullmatch(value['public_manifest_sha256']),
            'public_input_hash_invalid')
    return value


def parse_environment(raw):
    """Literal one-line KEY=value inputs; no shell evaluation or interpolation."""
    require(len(raw) <= 131072, 'runtime_environment_too_large')
    result = {}
    for line in raw.decode('utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        require(separator and re.fullmatch(r'[A-Z][A-Z0-9_]*', key) and key not in result and
                not any(ord(c) < 32 for c in value) and value == value.strip() and
                not value.startswith(('"', "'")), 'runtime_environment_literal_required')
        require(not any(word in key for word in ('MIGRATION', 'BOOTSTRAP')) and
                key not in ('POSTGRES_PASSWORD', 'POSTGRES_USER', 'JWT_SECRET', 'LOCATION_MASTER_KEY',
                            'DOCKER_HOST', 'DOCKER_CONFIG', 'COMPOSE_FILE', 'COMPOSE_PROFILES'),
                'nonruntime_credential_or_control_input')
        result[key] = value
    return result


def validate_environment(env, config, passwords):
    fixed = {'APP_ENV': 'prod', 'POSTGRES_DB': 'map_prod', 'POSTGRES_HOST': 'postgres',
             'POSTGRES_PORT': '5432', 'USER_DATABASE_USER': 'map_user_runtime',
             'AGENT_DATABASE_USER': 'map_agent_runtime', 'REDIS_HOST': 'redis', 'REDIS_PORT': '6379',
             'LANGGRAPH_SCHEMA': 'langgraph', 'KAKAO_PUBLIC_ORIGIN': API,
             'KAKAO_OAUTH_REDIRECT_URI': API + '/api/v1/auth/kakao/callback',
             'KAKAO_APP_CALLBACK_SCHEME': 'mapauth://kakao', 'CHAT_INVITE_BASE_URL': SITE + '/invite/'}
    require(all(env.get(key) == value for key, value in fixed.items()), 'production_runtime_identity_mismatch')
    switches = {'AUTH_ENFORCED': 'true', 'TESTER_SEED_ENABLED': 'false', 'PLACES_STUB_MODE': 'false',
                'TRAINING_CAPTURE_ENABLED': 'false', 'TRAINING_EXPORT_ENABLED': 'false',
                'LOCATION_ENC_ENABLED': 'true', 'LOCATION_WIRE_ENABLED': 'true', 'APPLE_ENABLED': 'true'}
    require(all(env.get(key, '').lower() == value for key, value in switches.items()),
            'production_runtime_switch_mismatch')
    required = ('JWT_PRIVATE_KEY', 'JWT_PUBLIC_KEY', 'LOCATION_ENC_ACTIVE_KID', 'LOCATION_ENC_KEYS',
                'LOCATION_WIRE_KEY', 'CHECKPOINT_ENC_ACTIVE_KID', 'CHECKPOINT_ENC_KEYS',
                'INTERNAL_SERVICE_TOKEN', 'USER_ADMIN_INTERNAL_TOKEN', 'HUB_ADMIN_INTERNAL_TOKEN',
                'VISION_INTERNAL_TOKEN', 'GEMINI_API_KEY', 'GEMINI_MODEL', 'KMA_SERVICE_KEY',
                'KAKAO_REST_API_KEY', 'KAKAO_OAUTH_CLIENT_ID', 'KAKAO_OAUTH_CLIENT_SECRET',
                'APPLE_CLIENT_ID', 'APPLE_TEAM_ID', 'APPLE_KEY_ID', 'APPLE_PRIVATE_KEY_B64', 'EDGE_EMAIL')
    require(all(env.get(key) and not env[key].lower().startswith(('replace', 'required', 'changeme'))
                for key in required), 'production_runtime_input_missing')
    require(env['APPLE_CLIENT_ID'] == 'kr.mapservice.client', 'apple_production_identity_mismatch')
    require(env.get('VISION_GEMINI_API_KEY') == env['GEMINI_API_KEY'] and
            hashlib.sha256(env['GEMINI_API_KEY'].encode()).hexdigest() == config['gemini_key_sha256'],
            'verified_production_gemini_key_required')
    tokens = [env[key] for key in ('INTERNAL_SERVICE_TOKEN', 'USER_ADMIN_INTERNAL_TOKEN',
                                  'HUB_ADMIN_INTERNAL_TOKEN', 'VISION_INTERNAL_TOKEN')]
    require(len(set(tokens)) == len(tokens), 'production_token_reuse')
    require(all(env.get(name) == passwords[name] for name in
                ('USER_DATABASE_PASSWORD', 'AGENT_DATABASE_PASSWORD', 'REDIS_PASSWORD')),
            'runtime_database_secret_mismatch')
    hub = urllib.parse.urlsplit(env.get('HUB_DATABASE_URL', ''))
    require(hub.scheme == 'postgresql+psycopg' and hub.hostname == 'postgres' and hub.port == 5432 and
            hub.path == '/map_prod' and hub.username == 'map_hub_runtime' and
            urllib.parse.unquote(hub.password or '') == passwords['HUB_DATABASE_PASSWORD'] and
            not hub.query and not hub.fragment, 'hub_runtime_database_mismatch')
    redis = urllib.parse.urlsplit(env.get('REDIS_URL', ''))
    require(redis.scheme == 'redis' and redis.hostname == 'redis' and redis.port == 6379 and
            urllib.parse.unquote(redis.password or '') == passwords['REDIS_PASSWORD'] and
            redis.path in ('', '/0') and not redis.query and not redis.fragment, 'redis_runtime_secret_mismatch')
    require(set(env.get('CORS_ALLOWED_ORIGINS', '').split(',')) == {SITE}, 'production_cors_mismatch')
    internal = {'HUB_BASE_URL': 'http://proxy:8081/hub', 'AGENT_BASE_URL': 'http://proxy:8081/agent',
                'USER_SERVICE_BASE_URL': 'http://proxy:8081/user',
                'OSRM_FOOT_BASE_URL': 'http://osrm-foot:5000', 'OSRM_BICYCLE_BASE_URL': 'http://osrm-bicycle:5000'}
    require(all(env.get(key) == value for key, value in internal.items()), 'production_internal_route_mismatch')
    test_hosts = ('mapapptest.duckdns.org', 'mapcenter-b59ca.web.app', 'mapcenter-b59ca.firebaseapp.com')
    require(not any(host in value for value in env.values() for host in test_hosts), 'test_host_in_production')
    return env


def verify_public(config, *, root=PUBLIC, manifest_path=PUBLIC_MANIFEST):
    require(receiver.private.digest(manifest_path) == config['public_manifest_sha256'], 'public_manifest_pin_mismatch')
    manifest = receiver.private.read_json(manifest_path)
    require(manifest.get('schema_version') == 1 and manifest.get('status') == 'READY_FOR_PUBLICATION' and
            manifest.get('environment') == 'prod' and manifest.get('source_ref') == 'master' and
            re.fullmatch(r'[a-f0-9]{40}', manifest.get('source_sha', '')) and manifest.get('blockers') == [] and
            manifest.get('public_site_origin') == SITE and manifest.get('api_origin') == API and
            manifest.get('android_package') == 'kr.mapservice.client', 'release_ready_public_bundle_required')
    prefix = manifest.get('apple_app_id_prefix', '')
    require(isinstance(prefix, str) and re.fullmatch(r'[A-Z0-9]{10}', prefix), 'apple_app_prefix_required')
    receiver.private.clean_path(root)
    files = manifest.get('files')
    require(isinstance(files, dict) and 0 < len(files) <= 10000, 'public_file_inventory_required')
    actual = set()
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'public_symlink_forbidden')
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        require(path.is_file() and name in files and receiver.HEX.fullmatch(files[name]) and
                receiver.private.digest(path) == files[name], 'public_file_hash_mismatch')
        actual.add(name)
    require(actual == set(files), 'public_file_inventory_mismatch')
    required = {'index.html', 'app_config.json', 'invite-environment.json', 'invite/index.html',
                '.well-known/apple-app-site-association', '.well-known/assetlinks.json',
                *('legal/' + name + '.html' for name in ('privacy', 'terms', 'location-terms', 'support', 'delete-account'))}
    require(required <= actual, 'public_route_missing')
    app = receiver.private.read_json(root / 'app_config.json')
    invite = receiver.private.read_json(root / 'invite-environment.json')
    require(app.get('environment') == 'prod' and app.get('api_base_url') == API and
            invite.get('app_environment') == 'prod' and invite.get('api_allowed_origins') == [API] and
            invite.get('invite_origin') == SITE and invite.get('public_site_origin') == SITE and
            invite.get('app_config_url') == SITE + '/app_config.json' and
            invite.get('android_package') == 'kr.mapservice.client' and invite.get('invite_scheme') == 'mapservice',
            'public_native_environment_mismatch')
    aasa = receiver.private.read_json(root / '.well-known/apple-app-site-association')
    details = aasa.get('applinks', {}).get('details', [])
    require(any(item.get('appID') == prefix + '.kr.mapservice.client' and '/invite/*' in item.get('paths', [])
                for item in details), 'apple_association_mismatch')
    with receiver.private.open_read(root / '.well-known/assetlinks.json') as stream:
        raw = stream.read(1048577)
    require(len(raw) <= 1048576, 'android_association_size_limit')
    links = json.loads(raw)
    require(isinstance(links, list) and any(
        item.get('relation') == ['delegate_permission/common.handle_all_urls'] and
        item.get('target', {}).get('namespace') == 'android_app' and
        item['target'].get('package_name') == 'kr.mapservice.client' and
        item['target'].get('sha256_cert_fingerprints') and
        all(re.fullmatch(r'(?:[A-F0-9]{2}:){31}[A-F0-9]{2}', fingerprint)
            for fingerprint in item['target']['sha256_cert_fingerprints']) for item in links),
        'android_association_mismatch')
    return manifest


class Backend(receiver.Backend):
    def inputs(self, config):
        runtime = receiver.configuration(receiver.private.read_json(receiver.CONFIG, private=True))
        enrollment, contract, release = self.verify(runtime)
        require(release['source_ref'] == 'master', 'production_requires_master_release')
        request = receiver.private.read_json(STATE / 'bootstrap-request.json', private=True)
        proof = receiver.private.read_json(STATE / 'new-host-proof.json', private=True)
        expected = receiver.identity_request(runtime, enrollment, contract, release, proof, request['postgres']['container_id'])
        expected['created_at'] = request['created_at']
        require(request == expected, 'serving_database_request_mismatch')
        receiver.verify_bootstrap_receipt(receiver.private.read_json(STATE / 'bootstrap-receipt.json', private=True), request)
        completed = receiver.private.read_json(STATE / 'database-provision-complete.json', private=True)
        require(completed.get('status') == 'PRODUCTION_DATABASE_PREPARED' and
                completed.get('request_sha256') == receiver.canonical(request) and
                completed.get('user_catalog_preserved') is True and
                completed.get('hub_initial_database_create_revoked') is True and
                completed.get('runtime_probes') == {'hub': 'PASS', 'agent': 'PASS'}, 'database_completion_required')
        for name in ('hub', 'agent'):
            source = completed.get('sources', {}).get(name, {})
            require(source.get('source_sha') == release['services'][name]['source_sha'] and
                    source.get('image') == contract['images'][name]['image'], 'database_release_source_mismatch')
        passwords = {}
        for name in ('USER_DATABASE_PASSWORD', 'HUB_DATABASE_PASSWORD', 'AGENT_DATABASE_PASSWORD', 'REDIS_PASSWORD'):
            with receiver.private.open_read(DATA / 'secrets' / name, private=True) as stream:
                passwords[name] = stream.read(65537).decode().removesuffix('\n')
            require(passwords[name] and len(passwords[name]) <= 65536, 'runtime_secret_missing')
        with receiver.private.open_read(ENV_FILE, private=True) as stream:
            raw = stream.read(131073)
        require(hashlib.sha256(raw).hexdigest() == config['runtime_env_sha256'], 'runtime_env_pin_mismatch')
        environment = validate_environment(parse_environment(raw), config, passwords)
        image_ids = {name: self.docker(['image', 'inspect', '--format', '{{.Id}}', entry['image']])
                     for name, entry in contract['images'].items() if name in SERVICES}
        require(set(image_ids) == set(SERVICES) and all(receiver.IMAGE.fullmatch(value) for value in image_ids.values()),
                'serving_cached_images_missing')
        return {'runtime': runtime, 'request': request, 'contract': contract, 'environment': environment,
                'images': image_ids, 'binding': receiver.canonical({'request': request,
                    'runtime_env_sha256': config['runtime_env_sha256'], 'gemini_key_sha256': config['gemini_key_sha256']})}

    def public_inputs(self, config):
        require(config['public_manifest_sha256'] is not None, 'ready_public_manifest_pin_required')
        return verify_public(config)

    def pg(self, inputs, *, start=False):
        pin = inputs['request']['postgres']
        item = json.loads(self.docker(['inspect', pin['container_id']]))[0]
        labels = item['Config'].get('Labels') or {}
        require(item['Id'] == pin['container_id'] and item['Image'] == pin['image_id'] and
                labels.get('com.docker.compose.project') == 'map-prod' and
                labels.get('com.docker.compose.service') == 'postgres' and not item['HostConfig']['PortBindings'] and
                item['HostConfig']['RestartPolicy']['Name'] == 'no' and
                any(m['Type'] == 'bind' and m['Source'] == str(receiver.PGDATA) and
                    m['Destination'] == '/var/lib/postgresql/data' and m['RW'] is True for m in item['Mounts']),
                'serving_postgres_identity_changed')
        if start and not item['State']['Running']:
            self.docker(['start', pin['container_id']])
        if start:
            self.postgres(pin['container_id'], pin['image_id'])
        return pin['container_id']

    def prepare(self, inputs):
        self.pg(inputs, start=True)
        existing = self.docker(['network', 'ls', '--filter', 'name=^' + NETWORK + '$', '--format', '{{.Name}}'])
        if not existing:
            self.docker(['network', 'create', '--driver', 'bridge', '--label', 'kr.mapservice.project=map-prod', NETWORK])
        net = json.loads(self.docker(['network', 'inspect', NETWORK]))[0]
        require(net['Name'] == NETWORK and net['Driver'] == 'bridge' and not net['Internal'] and
                net.get('Labels', {}).get('kr.mapservice.project') == 'map-prod', 'serving_network_not_owned')
        if inputs['request']['postgres']['container_id'] not in (net.get('Containers') or {}):
            self.docker(['network', 'connect', '--alias', 'postgres', NETWORK, inputs['request']['postgres']['container_id']])
        for name in ('redis', 'caddy-data', 'caddy-config', 'proxy-upstreams'):
            receiver.private_dir(DATA / 'data' / name)
        require(not list((DATA / 'data/proxy-upstreams').iterdir()), 'proxy_override_must_be_empty')

    def compose(self, inputs, *args):
        env = {**inputs['environment'], **receiver.ENV, 'COMPOSE_PROJECT_NAME': 'map-prod',
               'MAP_ENVIRONMENT': 'prod', 'OSRM_NETWORK': NETWORK,
               'OSRM_MOUNT_ROOT': str(DATA / 'staging' / inputs['runtime']['release_name'] / 'runtime'),
               'OSRM_FOOT_PORT': '5000', 'OSRM_BICYCLE_PORT': '5001',
               'PROXY_UPSTREAMS_DIR': str(DATA / 'data/proxy-upstreams')}
        for name in SERVICES:
            env['PROD_' + name.upper().replace('-', '_') + '_IMAGE'] = inputs['contract']['images'][name]['image']
        command = ['docker', 'compose', '--project-directory', str(ROOT), '--env-file', '/dev/null',
                   '-f', str(ROOT / 'docker-compose.prod.yml'), *args]
        try:
            result = subprocess.run(command, env=env, cwd=ROOT, text=True, capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            raise receiver.ReceiverError('serving_compose_unavailable') from None
        require(result.returncode == 0, 'serving_compose_failed')
        return result.stdout

    def rendered(self, inputs):
        value = json.loads(self.compose(inputs, 'config', '--format', 'json'))
        require(value.get('name') == 'map-prod' and set(value['services']) == set(SERVICES), 'serving_service_allowlist')
        for name, entry in value['services'].items():
            require(entry.get('image') == inputs['contract']['images'][name]['image'] and
                    not entry.get('build') and entry.get('restart') == 'no', 'serving_image_or_restart_mismatch')
            require('postgres' not in entry.get('depends_on', {}), 'serving_must_not_manage_postgres')
            for port in entry.get('ports', []):
                require(name == 'edge' or port.get('host_ip') == '127.0.0.1', 'private_port_exposed')
        require(value['services']['agent']['environment']['GEMINI_API_KEY'] ==
                value['services']['yolo']['environment']['GEMINI_API_KEY'] == inputs['environment']['GEMINI_API_KEY'],
                'rendered_gemini_key_mismatch')
        inputs['rendered'] = value
        return value

    def up(self, inputs, names):
        # Redis keeps its exact identity for the installed backup source contract.
        if 'redis' in names:
            self.compose(inputs, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--no-recreate',
                         '--wait', '--wait-timeout', '180', 'redis')
        rest = [name for name in names if name != 'redis']
        if rest:
            self.compose(inputs, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                         '--wait', '--wait-timeout', '240', *rest)

    def inventory(self, inputs, names):
        result = {}
        for name in names:
            ids = self.docker(['ps', '-aq', '--no-trunc', '--filter', 'label=com.docker.compose.project=map-prod',
                               '--filter', 'label=com.docker.compose.service=' + name]).splitlines()
            require(len(ids) == 1 and receiver.HEX.fullmatch(ids[0]), 'serving_container_ambiguous')
            item = json.loads(self.docker(['inspect', '--format',
                '{"image":{{json .Image}},"running":{{json .State.Running}},"oom":{{json .State.OOMKilled}},'
                '"environment":{{json .Config.Env}}}', ids[0]]))
            require(item['image'] == inputs['images'][name] and item['running'] and not item['oom'],
                    'serving_container_unhealthy')
            actual_env = dict(value.split('=', 1) for value in item['environment'])
            expected_env = inputs['rendered']['services'][name].get('environment', {})
            require(all(actual_env.get(key) == value for key, value in expected_env.items() if value is not None),
                    'serving_container_environment_drift')
            result[name] = {'container_id': ids[0], 'image_id': item['image']}
        self.postgres(inputs['request']['postgres']['container_id'], inputs['request']['postgres']['image_id'])
        return result

    def stop(self, names=PUBLIC_SERVICES):
        # Admission can always be closed, even with missing/changed runtime keys,
        # public files or DB receipts. Labels scope this to this fixed project.
        for name in names:
            ids = self.docker(['ps', '-q', '--no-trunc', '--filter', 'label=com.docker.compose.project=map-prod',
                               '--filter', 'label=com.docker.compose.service=' + name]).splitlines()
            require(all(receiver.HEX.fullmatch(cid) for cid in ids), 'public_container_id_invalid')
            if ids:
                self.docker(['stop', '--time', '60', *ids], timeout=90)

    def private_ready(self):
        for port, path in ((8090, '/healthz/app'), (8000, '/health/ready'), (8001, '/health/ready'),
                           (8004, '/health'), (5000, '/nearest/v1/foot/126.9780,37.5665'),
                           (5001, '/nearest/v1/bicycle/126.9780,37.5665')):
            body, _ = self.http('http://127.0.0.1:' + str(port) + path, 200)
            if port in (5000, 5001):
                require(json.loads(body).get('code') == 'Ok', 'routing_readiness_failed')
            if port == 8090:
                require(json.loads(body).get('status') == 'UP', 'user_readiness_failed')
        self.http('http://127.0.0.1:8090/api/v1/users/me', 401)

    def http(self, url, expected):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            result = opener.open(url, timeout=15)
        except urllib.error.HTTPError as error:
            result = error
        except (OSError, urllib.error.URLError):
            raise receiver.ReceiverError('serving_http_unavailable') from None
        with result:
            require(result.code == expected, 'serving_http_status_mismatch')
            return result.read(1048576), dict(result.headers)

    def public_ready(self):
        # Caddy has no image healthcheck; allow initial ACME issuance to finish.
        deadline = time.monotonic() + 120
        while True:
            try:
                self.public_probe()
                return
            except receiver.ReceiverError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)

    def public_probe(self):
        body, _ = self.http(API + '/healthz/app', 200)
        require(json.loads(body).get('status') == 'UP', 'public_user_readiness_failed')
        self.http(API + '/api/v1/users/me', 401)
        self.http(API + '/actuator/env', 404)
        for path in ('/', '/legal/privacy.html', '/legal/terms.html', '/legal/location-terms.html',
                     '/legal/support.html', '/legal/delete-account.html', '/invite/readiness',
                     '/app_config.json', '/invite-environment.json', '/.well-known/assetlinks.json',
                     '/.well-known/apple-app-site-association'):
            body, headers = self.http(SITE + path, 200)
            require(body, 'empty_public_resource')
            if path.endswith('.json') or path.endswith('apple-app-site-association'):
                require(headers.get('Content-Type', '').split(';')[0] == 'application/json', 'public_json_content_type')
        self.http(SITE + '/legal/not-present', 404)


def execute(action, config=None, *, backend=None, state=STATE, keep_resume_intent=False):
    backend = backend or Backend()
    receiver.private_dir(state)
    with receiver.writer(state):
        path = state / STATE_NAME
        # Stop first: inability to decode a receipt must not leave the edge open.
        if action == 'stop-public':
            try:
                backend.stop()
            except Exception:
                receiver.host.atomic_json(path, {'status': 'PUBLIC_STOP_FAILED', 'public_serving': 'UNKNOWN',
                    'resume_public': False, 'updated_at': receiver.now()})
                raise receiver.ReceiverError('public_stop_failed') from None
        previous = receiver.private.read_json(path, private=True) if path.exists() else {}
        if action == 'stop-public':
            receiver.host.atomic_json(path, {**previous, 'public_serving': 'HOLD',
                                              'resume_public': keep_resume_intent and previous.get('resume_public', False),
                                              'status': 'PUBLIC_STOPPED', 'updated_at': receiver.now()})
            return {'status': 'PUBLIC_STOPPED', 'public_serving': 'HOLD'}
        if action == 'start-private':
            require(previous.get('public_serving') != 'OPEN', 'stop_public_before_private_start')
        inputs = None
        try:
            config = configuration(config if config is not None else receiver.private.read_json(CONFIG, private=True))
            inputs = backend.inputs(config)
            backend.pg(inputs)
            backend.rendered(inputs)
            if action == 'verify':
                return {'status': 'SERVING_INPUTS_VERIFIED',
                        'public_serving': previous.get('public_serving', 'HOLD')}
            if action == 'resume':
                require(previous.get('binding') == inputs['binding'] and previous.get('status') in
                        ('PRIVATE_READY', 'PUBLIC_READY', 'PUBLIC_STOPPED'), 'previous_serving_receipt_required')
                publish = previous.get('resume_public') is True
            else:
                publish = action == 'publish'
            if action == 'publish':
                require(previous.get('binding') == inputs['binding'] and previous.get('status') == 'PRIVATE_READY',
                        'private_readiness_receipt_required')
            if publish:
                backend.public_inputs(config)
                if action == 'resume':
                    require(previous.get('public_manifest_sha256') == config['public_manifest_sha256'],
                            'published_bundle_changed_before_resume')
            # Docker live-restore may retain the edge across a daemon restart.
            backend.stop(('edge',))
            backend.prepare(inputs)
            backend.up(inputs, PRIVATE_SERVICES)
            backend.private_ready()
            containers = backend.inventory(inputs, PRIVATE_SERVICES)
            record = {'status': 'PRIVATE_READY', 'binding': inputs['binding'], 'public_serving': 'HOLD',
                      'resume_public': False,
                      'containers': containers, 'updated_at': receiver.now()}
            receiver.host.atomic_json(path, record)
            if publish:
                backend.up(inputs, ('edge',))
                backend.public_ready()
                record.update(status='PUBLIC_READY', public_serving='OPEN', resume_public=True,
                              public_manifest_sha256=config['public_manifest_sha256'],
                              containers=backend.inventory(inputs, SERVICES), updated_at=receiver.now())
                receiver.host.atomic_json(path, record)
            return {'status': record['status'], 'public_serving': record['public_serving']}
        except Exception:
            if action != 'verify':
                try:
                    backend.stop()
                except Exception:
                    receiver.host.atomic_json(path, {'status': 'PUBLIC_STOP_FAILED', 'public_serving': 'UNKNOWN',
                        'resume_public': False, 'updated_at': receiver.now()})
                    raise receiver.ReceiverError('public_stop_failed') from None
                receiver.host.atomic_json(path, {'status': 'SERVING_FAILED',
                    'binding': inputs['binding'] if inputs else previous.get('binding'),
                    'public_serving': 'HOLD', 'resume_public': False, 'updated_at': receiver.now()})
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('verify', 'start-private', 'publish', 'stop-public', 'resume'))
    parser.add_argument('--keep-resume-intent', action='store_true',
                        help='stop-public only: preserve prior publication intent for systemd stop/start')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        require(os.geteuid() == 0, 'root_serving_runner_required')
        receiver.source_ownership(ROOT)
        require(not args.keep_resume_intent or args.action == 'stop-public', 'stop_only_resume_option')
        result = execute(args.action, keep_resume_intent=args.keep_resume_intent)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'ERROR', 'public_serving': 'UNKNOWN',
                          'error_code': str(error) if isinstance(error, receiver.ReceiverError) else 'serving_guard_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
