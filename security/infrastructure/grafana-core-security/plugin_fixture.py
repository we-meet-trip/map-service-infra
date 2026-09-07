"""Check signed catalog registration and a real, isolated Prometheus backend query.

Requires the parent's owned Sandbox/Prometheus network. Creates only the fixed
synthetic data source inside that disposable Grafana database; subsequent calls
verify/reuse it. No container, network, provider, or credential setup occurs here.

Schemas checked against Grafana 13.2.1 commit 56cd3e9288d8255fecebe5d05b48d191f50674b5:
pkg/api/{plugins.go,dtos/plugins.go,datasources.go}. Prometheus query properties
come from grafana/grafana-prometheus-datasource commit
2e10053dd7d940f9eb91701df5122c8e348a0d18, pkg/promlib/models/query.go.
"""
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse


PLUGIN_IDS = tuple(json.loads(Path(__file__).with_name('pins.json').read_text())[
    'preserved_catalog_plugin_ids'])
DATASOURCE_UID = 'map-synthetic-plugin-prom'
PROMETHEUS_URL = 'http://map-fixture-prom:9090'


def require(value, code):
    if not value:
        raise ValueError(code)


def plugin_inventory(rows):
    require(isinstance(rows, list), 'grafana_plugin_list_not_array')
    inventory = []
    for plugin_id in PLUGIN_IDS:
        matches = [row for row in rows if isinstance(row, dict)
                   and row.get('id') == plugin_id]
        require(len(matches) == 1, 'grafana_plugin_registration:' + plugin_id)
        row = matches[0]
        require(row.get('type') == 'datasource', 'grafana_plugin_type:' + plugin_id)
        require(row.get('signature') == 'valid', 'grafana_plugin_signature:' + plugin_id)
        # `enabled` defaults false for data sources without an app setting. It is
        # not a backend registration indicator in Grafana's PluginListItem DTO.
        inventory.append({'id': plugin_id, 'signature': row['signature'],
                          'signatureType': row.get('signatureType'),
                          'signatureOrg': row.get('signatureOrg'),
                          'version': (row.get('info') or {}).get('version')})
    return inventory


def verify_datasource(data, prometheus_url):
    require(isinstance(data, dict) and data.get('uid') == DATASOURCE_UID,
            'grafana_fixture_datasource_identity')
    require(data.get('type') == 'prometheus' and data.get('access') == 'proxy'
            and data.get('url') == prometheus_url,
            'grafana_fixture_datasource_target')
    require(not data.get('basicAuth') and not data.get('withCredentials')
            and not any((data.get('secureJsonFields') or {}).values()),
            'grafana_fixture_datasource_credentials')


def verify_vector_response(response):
    require(isinstance(response, dict) and isinstance(response.get('results'), dict)
            and set(response['results']) == {'A'}, 'grafana_query_result_identity')
    result = response['results']['A']
    require(isinstance(result, dict) and not result.get('error')
            and not result.get('errorSource') and result.get('status') in (None, 200),
            'grafana_prometheus_backend_query_failed')
    frames = result.get('frames')
    require(isinstance(frames, list) and bool(frames), 'grafana_prometheus_frames_missing')
    samples = []
    for frame in frames:
        require(isinstance(frame, dict), 'grafana_query_frame_schema')
        fields = (frame.get('schema') or {}).get('fields')
        columns = (frame.get('data') or {}).get('values')
        require(isinstance(fields, list) and isinstance(columns, list)
                and len(fields) == len(columns), 'grafana_query_frame_columns')
        for field, values in zip(fields, columns):
            require(isinstance(field, dict) and isinstance(values, list),
                    'grafana_query_frame_field')
            if field.get('type') == 'number':
                samples.extend(values)
    require(len(samples) == 1 and type(samples[0]) in (int, float)
            and samples[0] == 1, 'grafana_prometheus_vector_value')
    return {'numeric_samples': 1, 'expected_value': 1, 'actual_value': samples[0]}


def check(s, grafana_origin, credentials, prometheus_url=PROMETHEUS_URL):
    """Return checks only after all 13 signatures and backend health/query pass."""
    origin = urllib.parse.urlsplit(grafana_origin)
    require(origin.scheme == 'http' and origin.hostname == '127.0.0.1'
            and origin.port is not None and 0 < origin.port < 65536
            and not origin.username and not origin.password
            and not origin.path and not origin.query and not origin.fragment,
            'grafana_fixture_loopback_origin_required')
    require(prometheus_url == PROMETHEUS_URL, 'owned_prometheus_alias_required')
    require(re.fullmatch(r'[a-f0-9]{12}', getattr(s, 'token', '')),
            'owned_sandbox_required')
    require(isinstance(credentials, str) and credentials.startswith('fixture-admin:')
            and len(credentials) > len('fixture-admin:'), 'synthetic_admin_required')

    def request(path, payload=None):
        try:
            body = s.request(grafana_origin, path, payload=payload, credentials=credentials)
        except urllib.error.HTTPError as error:
            raise ValueError('grafana_plugin_fixture_http_' + str(error.code)) from None
        require(isinstance(body, (bytes, str)) and len(body) <= 2 * 1024 * 1024,
                'grafana_fixture_bounded_json_required')
        return json.loads(body)

    inventory = plugin_inventory(request('/api/plugins'))
    sources = request('/api/datasources')
    require(isinstance(sources, list), 'grafana_datasource_list_not_array')
    existing = [row for row in sources if isinstance(row, dict)
                and row.get('uid') == DATASOURCE_UID]
    require(len(existing) <= 1, 'grafana_fixture_datasource_duplicate')
    created = not existing
    if existing:
        verify_datasource(existing[0], prometheus_url)
    else:
        request('/api/datasources', {'uid': DATASOURCE_UID,
                'name': 'MAP synthetic signed plugin preservation', 'type': 'prometheus',
                'access': 'proxy', 'url': prometheus_url, 'isDefault': False,
                'basicAuth': False, 'withCredentials': False,
                'jsonData': {'httpMethod': 'POST', 'timeInterval': '1s'}})
    verify_datasource(request('/api/datasources/uid/' + DATASOURCE_UID), prometheus_url)
    health = request('/api/datasources/uid/' + DATASOURCE_UID + '/health')
    require(isinstance(health, dict) and health.get('status') == 'OK',
            'grafana_prometheus_backend_health_failed')
    now_ms = int(time.time() * 1000)
    query = {'from': str(now_ms - 60000), 'to': str(now_ms), 'queries': [{
        'refId': 'A', 'datasource': {'type': 'prometheus', 'uid': DATASOURCE_UID},
        'expr': 'vector(1)', 'instant': True, 'range': False, 'format': 'table',
        'intervalMs': 1000, 'maxDataPoints': 1}]}
    sample = verify_vector_response(request('/api/ds/query', query))
    return {'status': 'PASS', 'all_13_catalog_plugins_registered': True,
            'all_13_catalog_signatures_valid': True, 'catalog_plugins': inventory,
            'prometheus_datasource_created': created, 'prometheus_datasource_uid': DATASOURCE_UID,
            'prometheus_backend_health': True, 'prometheus_backend_query': True,
            'expression': 'vector(1)', 'query_sample': sample,
            'external_provider_target_configured': False, 'production_credentials_used': False}
