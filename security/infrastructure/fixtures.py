"""Isolated synthetic compatibility checks; never accepts production volumes."""
import base64
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.request
import uuid


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


class NoFixtureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('fixture_http_redirect_refused')


class Sandbox:
    def __init__(self, output):
        self.output = output
        self.token = uuid.uuid4().hex[:12]
        self.containers = []
        self.volumes = []
        self.networks = []
        self.origins = {}

    def run(self, args, **kwargs):
        p = subprocess.run(args, capture_output=True, timeout=kwargs.pop('timeout', 120), **kwargs)
        require(p.returncode == 0, 'fixture_command_failed:' + Path(args[0]).name)
        return p.stdout

    def create_network(self, label):
        require(re.fullmatch(r'[a-z0-9-]+', label), 'fixture_network_label')
        name = 'map-infra-' + self.token + '-' + label
        self.run(['docker', 'network', 'create', '--internal', '--label',
                  'map.infra.fixture=' + self.token, name])
        self.networks.append(name)
        return name

    def create(self, image, label, port, data_path=None, seed=None, extra=(), command=()):
        name = 'map-infra-' + self.token + '-' + label
        args = ['docker', 'create', '--name', name, '--label', 'map.infra.fixture=' + self.token,
                '--memory', '768m', '--cpus', '1', '-p', '127.0.0.1::' + str(port)]
        if data_path:
            volume = name + '-data'
            self.run(['docker', 'volume', 'create', '--label', 'map.infra.fixture=' + self.token, volume])
            self.volumes.append(volume)
            args += ['-v', volume + ':' + data_path]
        self.run(args + list(extra) + [image] + list(command))
        self.containers.append(name)
        if seed:
            require(seed.is_file(), 'cold_backup_archive_required')
            self.run(['docker', 'cp', '-a', '-', name + ':' + data_path], input=seed.read_bytes())
        self.run(['docker', 'start', name])
        return name, self.origin(name, port)

    def resolve_origin(self, name, port):
        require(name in self.containers, 'fixture_container_not_registered')
        require(type(port) is int and 0 < port < 65536, 'fixture_port_invalid')
        info = json.loads(self.run(['docker', 'inspect', name]))[0]
        require(info.get('Name') == '/' + name and
                (info.get('Config', {}).get('Labels') or {}).get('map.infra.fixture') == self.token,
                'fixture_container_owner_mismatch')
        require(info.get('State', {}).get('Running') is True, 'fixture_container_not_running')
        key = str(port) + '/tcp'
        requested = (info.get('HostConfig', {}).get('PortBindings') or {}).get(key)
        require(isinstance(requested, list) and len(requested) == 1 and
                requested[0].get('HostIp') == '127.0.0.1', 'fixture_must_bind_loopback')
        settings = info.get('NetworkSettings') or {}
        networks = settings.get('Networks') or {}
        owned = set(networks).intersection(self.networks)
        proof = {'container': name, 'container_id': info['Id'], 'container_port': port}
        if owned:
            # Docker 28 does not publish ports on an internal-only network. The
            # hosted Linux runner can reach its bridge IP without enabling egress.
            require(len(owned) == 1 and set(networks) == owned, 'fixture_network_must_be_internal_only')
            actual_bindings = (settings.get('Ports') or {}).get(key)
            require(actual_bindings in (None, []) or
                    (isinstance(actual_bindings, list) and len(actual_bindings) == 1 and
                     actual_bindings[0].get('HostIp') == '127.0.0.1' and
                     re.fullmatch(r'[0-9]{1,5}', actual_bindings[0].get('HostPort', '')) and
                     0 < int(actual_bindings[0]['HostPort']) < 65536),
                    'fixture_internal_unexpected_publication')
            network = next(iter(owned))
            meta = json.loads(self.run(['docker', 'network', 'inspect', network]))[0]
            endpoint = networks[network]
            require(meta.get('Name') == network and meta.get('Internal') is True and
                    meta.get('Driver') == 'bridge' and
                    (meta.get('Labels') or {}).get('map.infra.fixture') == self.token and
                    meta.get('Id') == endpoint.get('NetworkID'), 'fixture_internal_network_owner_mismatch')
            member = (meta.get('Containers') or {}).get(info['Id'], {})
            address = ipaddress.IPv4Address(endpoint.get('IPAddress', ''))
            require(any(address in ipaddress.IPv4Network(cidr) for cidr in
                        ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')),
                    'fixture_internal_address_invalid')
            require(member.get('Name') == name and
                    ipaddress.IPv4Interface(member.get('IPv4Address', '')).ip == address,
                    'fixture_internal_endpoint_mismatch')
            proof.update(mode='owned_internal_bridge', network=network, network_id=meta['Id'])
            return 'http://' + str(address) + ':' + str(port), proof
        require(set(networks) == {'bridge'}, 'fixture_unowned_network_refused')
        bindings = (settings.get('Ports') or {}).get(key)
        require(isinstance(bindings, list) and len(bindings) == 1, 'fixture_loopback_binding_missing')
        binding = bindings[0]
        require(binding.get('HostIp') == '127.0.0.1' and
                re.fullmatch(r'[0-9]{1,5}', binding.get('HostPort', '')) and
                0 < int(binding['HostPort']) < 65536, 'fixture_loopback_binding_invalid')
        proof['mode'] = 'owned_loopback_binding'
        return 'http://127.0.0.1:' + binding['HostPort'], proof

    def origin(self, name, port):
        origin, proof = self.resolve_origin(name, port)
        self.origins[origin] = proof
        return origin

    def owns_origin(self, origin):
        proof = self.origins.get(origin)
        if not proof:
            return False
        try:
            current, current_proof = self.resolve_origin(proof['container'], proof['container_port'])
            return current == origin and current_proof == proof
        except (ValueError, KeyError, TypeError):
            return False

    def request(self, origin, path, payload=None, credentials=None):
        require(self.owns_origin(origin), 'fixture_http_origin_not_owned')
        require(isinstance(path, str) and path.startswith('/') and not path.startswith('//')
                and '#' not in path, 'fixture_http_path_invalid')
        headers = {}
        if credentials:
            headers['Authorization'] = 'Basic ' + base64.b64encode(credentials.encode()).decode()
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            payload = json.dumps(payload).encode()
        req = urllib.request.Request(origin + path, data=payload, headers=headers)
        # Never forward synthetic credentials via environment proxies or redirects.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoFixtureRedirect())
        with opener.open(req, timeout=5) as response:
            return response.read()

    def diagnostics(self):
        states = []
        for name in self.containers:
            try:
                meta = json.loads(self.run(['docker', 'inspect', name]))[0]
                require((meta.get('Config', {}).get('Labels') or {}).get('map.infra.fixture') == self.token,
                        'fixture_diagnostic_owner_mismatch')
                state = meta.get('State') or {}
                states.append({'container': name, 'image_id': meta.get('Image'),
                               'state': {key: state.get(key) for key in
                                         ('Status', 'Running', 'ExitCode', 'OOMKilled', 'StartedAt', 'FinishedAt')}})
            except Exception as error:
                states.append({'container': name, 'diagnostic_error_type': type(error).__name__})
        return states

    def wait(self, origin, path):
        for _ in range(90):
            try:
                return self.request(origin, path)
            except (OSError, ValueError):
                time.sleep(1)
        raise ValueError('synthetic_http_readiness_timeout')

    def stop_copy(self, name, data_path, label):
        self.run(['docker', 'stop', '--time', '30', name])
        backup = self.output / (label + '.tar')
        # Preserve numeric owners in the tar stream; never extract/chown the retained backup.
        backup.write_bytes(self.run(['docker', 'cp', name + ':' + data_path + '/.', '-']))
        with tarfile.open(backup, 'r:*') as archive:
            require(any(member.isfile() for member in archive), 'cold_backup_empty')
        return backup

    def clean(self):
        for name in reversed(self.containers):
            meta = json.loads(self.run(['docker', 'inspect', name]))[0]
            require(meta['Config']['Labels'].get('map.infra.fixture') == self.token, 'container_owner_mismatch')
            self.run(['docker', 'rm', '-f', '-v', name])
        for name in reversed(self.volumes):
            meta = json.loads(self.run(['docker', 'volume', 'inspect', name]))[0]
            require(meta['Labels'].get('map.infra.fixture') == self.token, 'volume_owner_mismatch')
            self.run(['docker', 'volume', 'rm', name])
        for name in reversed(self.networks):
            meta = json.loads(self.run(['docker', 'network', 'inspect', name]))[0]
            require(meta['Labels'].get('map.infra.fixture') == self.token, 'network_owner_mismatch')
            self.run(['docker', 'network', 'rm', name])


def tree_hashes(path):
    if path.is_file():
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob('*')) if p.is_file()}


def prometheus(s, old, new):
    config = s.output / 'prometheus.yml'
    config.write_text('global:\n  scrape_interval: 1s\nscrape_configs:\n  - job_name: synthetic\n    static_configs:\n      - targets: ["127.0.0.1:9090"]\n')
    config.chmod(0o644)
    options = ('-v', str(config) + ':/etc/prometheus/prometheus.yml:ro')
    command = ('--config.file=/etc/prometheus/prometheus.yml', '--storage.tsdb.path=/prometheus')
    before, url = s.create(old, 'prom-old', 9090, '/prometheus', extra=options, command=command)
    s.wait(url, '/-/ready')
    query = '/api/v1/query?query=prometheus_build_info'
    for _ in range(30):
        result = json.loads(s.request(url, query))['data']['result']
        if result:
            break
        time.sleep(1)
    require(bool(result), 'baseline_tsdb_sample_missing')
    timestamp = str(float(result[0]['value'][0]))
    fixed_query = query + '&time=' + timestamp
    expected = json.loads(s.request(url, fixed_query))['data']['result']
    backup = s.stop_copy(before, '/prometheus', 'prom-pre-upgrade')
    hashes = tree_hashes(backup)
    with tarfile.open(backup, 'r:*') as archive:
        require(any('/wal/' in '/' + m.name for m in archive if m.isfile()), 'prometheus_wal_backup_missing')
    candidate_started = time.time()
    forward, url = s.create(new, 'prom-new', 9090, '/prometheus', backup, options, command)
    s.wait(url, '/-/ready')
    require(json.loads(s.request(url, fixed_query))['data']['result'] == expected, 'prometheus_historical_sample_changed')
    written = []
    for _ in range(30):
        samples = json.loads(s.request(url, '/api/v1/query?query=timestamp%28prometheus_build_info%29'))['data']['result']
        written = [float(sample['value'][1]) for sample in samples if float(sample['value'][1]) >= candidate_started]
        if written: break
        time.sleep(1)
    require(bool(written), 'candidate_new_tsdb_sample_not_written')
    new_query = query + '&time=' + str(max(written) + 0.01)
    new_expected = json.loads(s.request(url, new_query))['data']['result']
    require(bool(new_expected), 'candidate_written_sample_not_queryable')
    s.run(['docker', 'restart', forward]); url = s.origin(forward, 9090); s.wait(url, '/-/ready')
    require(json.loads(s.request(url, new_query))['data']['result'] == new_expected, 'candidate_new_sample_restart_lost')
    upgraded = s.stop_copy(forward, '/prometheus', 'prom-post-upgrade')
    rollback, url = s.create(old, 'prom-rollback', 9090, '/prometheus', backup, options, command)
    s.wait(url, '/-/ready')
    require(json.loads(s.request(url, fixed_query))['data']['result'] == expected, 'prometheus_backup_rollback_failed')
    require(tree_hashes(backup) == hashes, 'retained_prometheus_backup_mutated')
    # Opening a candidate-written TSDB with the old binary is checked separately.
    outcome = {'pre_upgrade_backup_restore': True, 'forward_historical_query': True,
               'pre_backup_unchanged': True, 'candidate_new_sample_and_restart': True,
               'candidate_sample_timestamp': max(written), 'post_upgrade_data_direct_downgrade': 'NOT_TESTED'}
    try:
        direct, url = s.create(old, 'prom-direct-old', 9090, '/prometheus', upgraded, options, command)
        s.wait(url, '/-/ready')
        require(json.loads(s.request(url, fixed_query))['data']['result'] == expected, 'prometheus_direct_downgrade_query_failed')
        require(json.loads(s.request(url, new_query))['data']['result'] == new_expected, 'prometheus_candidate_sample_old_read_failed')
        outcome['post_upgrade_data_direct_downgrade'] = 'PASS_SYNTHETIC'
    except Exception:
        outcome['post_upgrade_data_direct_downgrade'] = 'FAIL_USE_PRE_UPGRADE_BACKUP'
    outcome['pre_upgrade_backup_sha256'] = hashes
    return outcome


def grafana(s, old, new):
    spec = importlib.util.spec_from_file_location('grafana_plugin_fixture',
        Path(__file__).with_name('grafana-core-security') / 'plugin_fixture.py')
    plugins = importlib.util.module_from_spec(spec); spec.loader.exec_module(plugins)
    network = s.create_network('grafana-plugin')
    prometheus_image = 'prom/prometheus@sha256:f6639335d34a77d9d9db382b92eeb7fc00934be8eae81dbc03b31cfe90411a94'
    s.run(['docker', 'pull', '--platform', 'linux/amd64', prometheus_image], timeout=600)
    prom, prom_origin = s.create(prometheus_image, 'grafana-prom', 9090, '/prometheus',
        extra=('--network', network, '--network-alias', 'map-fixture-prom'))
    s.wait(prom_origin, '/-/ready')
    password = uuid.uuid4().hex
    credentials = 'fixture-admin:' + password
    options = ('--network', network, '-e', 'GF_SECURITY_ADMIN_USER=fixture-admin', '-e', 'GF_SECURITY_ADMIN_PASSWORD=' + password,
               '-e', 'GF_AUTH_ANONYMOUS_ENABLED=false', '-e', 'GF_ANALYTICS_REPORTING_ENABLED=false',
               '-e', 'GF_ANALYTICS_CHECK_FOR_UPDATES=false', '-e', 'GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false',
               '-e', 'GF_PLUGINS_PREINSTALL_DISABLED=true')
    before, url = s.create(old, 'grafana-old', 3000, '/var/lib/grafana', extra=options)
    s.wait(url, '/api/health')
    body = {'dashboard': {'id': None, 'uid': 'map-synthetic-preserve', 'title': 'MAP synthetic preservation',
                         'tags': ['synthetic'], 'timezone': 'utc', 'schemaVersion': 39, 'version': 0, 'panels': []},
            'overwrite': False}
    saved = json.loads(s.request(url, '/api/dashboards/db', body, credentials))
    require(saved.get('status') == 'success', 'grafana_seed_not_saved')
    endpoint = '/api/dashboards/uid/map-synthetic-preserve'
    expected = json.loads(s.request(url, endpoint, credentials=credentials))['dashboard']
    def assert_dashboard(origin):
        actual = json.loads(s.request(origin, endpoint, credentials=credentials))['dashboard']
        for key in ('uid', 'title', 'tags', 'panels'):
            require(actual[key] == expected[key], 'grafana_dashboard_field_changed:' + key)
    backup = s.stop_copy(before, '/var/lib/grafana', 'grafana-pre-upgrade')
    hashes = tree_hashes(backup)
    with tarfile.open(backup, 'r:*') as archive:
        require(any(Path(m.name).name == 'grafana.db' for m in archive if m.isfile()), 'grafana_sqlite_backup_missing')
    forward, url = s.create(new, 'grafana-new', 3000, '/var/lib/grafana', backup, options)
    s.wait(url, '/api/health'); assert_dashboard(url)
    plugin_forward = plugins.check(s, url, credentials)
    s.run(['docker', 'restart', forward]); url = s.origin(forward, 3000); s.wait(url, '/api/health'); assert_dashboard(url)
    plugin_restart = plugins.check(s, url, credentials)
    require(plugin_forward['prometheus_datasource_created'] and not plugin_restart['prometheus_datasource_created'],
            'grafana_plugin_datasource_restart_persistence')
    upgraded = s.stop_copy(forward, '/var/lib/grafana', 'grafana-post-upgrade')
    rollback, url = s.create(old, 'grafana-rollback', 3000, '/var/lib/grafana', backup, options)
    s.wait(url, '/api/health'); assert_dashboard(url)
    require(tree_hashes(backup) == hashes, 'retained_grafana_backup_mutated')
    outcome = {'pre_upgrade_backup_restore': True, 'forward_dashboard_and_admin_auth': True,
               'plugins_forward': plugin_forward, 'plugins_restart': plugin_restart,
               'prometheus_fixture_image': prometheus_image, 'fixture_network_internal': True,
               'candidate_restart_persistence': True, 'pre_backup_unchanged': True,
               'post_upgrade_data_direct_downgrade': 'NOT_TESTED'}
    try:
        direct, url = s.create(old, 'grafana-direct-old', 3000, '/var/lib/grafana', upgraded, options)
        s.wait(url, '/api/health'); assert_dashboard(url)
        outcome['post_upgrade_data_direct_downgrade'] = 'PASS_SYNTHETIC'
    except Exception:
        outcome['post_upgrade_data_direct_downgrade'] = 'FAIL_USE_PRE_UPGRADE_BACKUP'
    outcome['pre_upgrade_backup_sha256'] = hashes
    return outcome


def smoke(s, service, image):
    if service == 'postgres-exporter':
        spec = importlib.util.spec_from_file_location('pg_exporter_fixture',
            Path(__file__).with_name('fixtures') / 'postgres_exporter.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module.check(s, image)
    if service == 'dns':
        data = s.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                      '--security-opt', 'no-new-privileges:true', image, '--version'])
        require(b'curl ' in data and b'https' in data, 'curl_tls_protocols_missing')
        return {'offline_binary_protocol_smoke': True, 'provider_dns_update_executed': False}
    ports = {'proxy': 80, 'node-exporter': 9100, 'redis-exporter': 9121}
    name, url = s.create(image, service, ports[service])
    path = '/' if service == 'proxy' else '/metrics'
    body = s.wait(url, path)
    marker = {'proxy': b'nginx', 'node-exporter': b'node_exporter_build_info',
              'redis-exporter': b'redis_exporter_build_info'}[service]
    require(marker.lower() in body.lower(), 'stateless_endpoint_content_missing')
    return {'isolated_http_smoke': True, 'serving_database_credentials_used': False,
            'host_filesystem_or_docker_socket_mounted': False}


def check(service, before, candidate, output):
    require(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
            and sys.platform == 'linux', 'remote_hosted_ci_only')
    require(re.fullmatch(r'[a-z0-9/_-]+@sha256:[a-f0-9]{64}', before), 'old_digest_required')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', candidate), 'candidate_runtime_image_identifier_required')
    if service == 'postgres':
        spec = importlib.util.spec_from_file_location('pg_fixture', Path(__file__).with_name('fixtures') / 'postgres_restore.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module.execute(before, candidate, output / 'compatibility')
    directory = output / 'compatibility'; directory.mkdir()
    s = Sandbox(directory)
    result = {'service': service, 'status': 'FAIL', 'old_image': before, 'candidate_runtime_image_id': candidate,
              'synthetic_only': True, 'production_data_or_credentials_used': False,
              'rollback_contract': 'Retain stopped pre-upgrade backup and exact old image; do not reuse mutated live data'}
    try:
        if service == 'prometheus': result['checks'] = prometheus(s, before, candidate)
        elif service == 'grafana': result['checks'] = grafana(s, before, candidate)
        else: result['checks'] = smoke(s, service, candidate)
        result['status'] = 'PASS'
    except Exception as error:
        result['failure_code'] = str(error) if isinstance(error, ValueError) else type(error).__name__
        result['failure_trace'] = [{'file': Path(frame.filename).name, 'function': frame.name,
                                    'line': frame.lineno} for frame in traceback.extract_tb(error.__traceback__)[-12:]]
        result['owned_container_states'] = s.diagnostics()
    finally:
        result['http_origin_proofs'] = s.origins
        try: s.clean(); result['cleanup'] = 'PASS'
        except Exception: result['cleanup'] = 'FAIL'; result['status'] = 'FAIL'
        (directory / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result
