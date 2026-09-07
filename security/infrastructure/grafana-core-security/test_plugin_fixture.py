import copy
import importlib.util
import json
from pathlib import Path
import unittest
import urllib.error


SPEC = importlib.util.spec_from_file_location('grafana_plugin_fixture',
                                             Path(__file__).with_name('plugin_fixture.py'))
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


class FakeSandbox:
    token = '0123456789ab'

    def __init__(self):
        self.plugins = [{'id': name, 'type': 'datasource', 'signature': 'valid',
                         'signatureType': 'grafana', 'signatureOrg': 'grafana',
                         'enabled': False, 'info': {'version': 'fixture'}}
                        for name in FIXTURE.PLUGIN_IDS]
        self.sources = []
        self.calls = []
        self.health = {'status': 'OK'}
        self.health_exception = None
        self.query = {'results': {'A': {'frames': [{
            'schema': {'refId': 'A', 'fields': [{'name': 'Time', 'type': 'time'},
                                              {'name': 'Value', 'type': 'number'}]},
            'data': {'values': [[1700000000000], [1]]}}]}}}

    def request(self, origin, path, payload=None, credentials=None):
        self.calls.append((path, payload))
        if path == '/api/plugins':
            return json.dumps(self.plugins).encode()
        if path == '/api/datasources':
            if payload is not None:
                self.sources.append(copy.deepcopy(payload))
                return b'{"message":"Datasource added"}'
            return json.dumps(self.sources).encode()
        if path.endswith('/health'):
            if self.health_exception:
                raise self.health_exception
            return json.dumps(self.health).encode()
        if path == '/api/datasources/uid/' + FIXTURE.DATASOURCE_UID:
            return json.dumps(self.sources[0]).encode()
        if path == '/api/ds/query':
            return json.dumps(self.query).encode()
        raise AssertionError(path)


class PluginFixtureTests(unittest.TestCase):
    def check(self, sandbox):
        return FIXTURE.check(sandbox, 'http://127.0.0.1:45678', 'fixture-admin:synthetic')

    def test_candidate_then_restart_reuses_datasource_and_reexecutes_backend(self):
        sandbox = FakeSandbox()
        first, second = self.check(sandbox), self.check(sandbox)
        self.assertTrue(first['prometheus_datasource_created'])
        self.assertFalse(second['prometheus_datasource_created'])
        self.assertEqual(len(sandbox.sources), 1)
        queries = [payload for path, payload in sandbox.calls if path == '/api/ds/query']
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0]['queries'][0]['expr'], 'vector(1)')
        self.assertEqual(queries[0]['queries'][0]['datasource']['uid'], FIXTURE.DATASOURCE_UID)

    def test_missing_duplicate_wrong_type_or_modified_signature_rejected_before_write(self):
        for mutation in ('missing', 'duplicate', 'modified', 'unsigned', 'internal', 'type'):
            sandbox = FakeSandbox()
            if mutation == 'missing':
                sandbox.plugins.pop()
            elif mutation == 'duplicate':
                sandbox.plugins.append(copy.deepcopy(sandbox.plugins[0]))
            elif mutation == 'type':
                sandbox.plugins[0]['type'] = 'panel'
            else:
                sandbox.plugins[0]['signature'] = mutation
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.check(sandbox)
            self.assertEqual(sandbox.sources, [])

    def test_backend_missing_or_error_cannot_pass_even_with_registered_valid_plugin(self):
        for query in ({'results': {'A': {'error': 'plugin unavailable'}}},
                      {'results': {'A': {'frames': []}}},
                      {'results': {'B': {'frames': []}}}):
            sandbox = FakeSandbox()
            sandbox.query = query
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.check(sandbox)
        sandbox = FakeSandbox()
        sandbox.health = {'status': 'ERROR'}
        with self.assertRaises(ValueError):
            self.check(sandbox)

    def test_wrong_empty_or_boolean_sample_rejected(self):
        for value in (0, 2, True, None, '1'):
            sandbox = FakeSandbox()
            sandbox.query['results']['A']['frames'][0]['data']['values'][1] = [value]
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.check(sandbox)

    def test_backend_http_not_found_is_failure_without_raw_response_or_credentials(self):
        sandbox = FakeSandbox()
        sandbox.health_exception = urllib.error.HTTPError(
            'http://127.0.0.1:45678/api/datasources/uid/fixture/health', 404,
            'plugin missing; synthetic credential must not appear', None, None)
        with self.assertRaisesRegex(ValueError, '^grafana_plugin_fixture_http_404$'):
            self.check(sandbox)

    def test_existing_foreign_target_is_not_overwritten(self):
        sandbox = FakeSandbox()
        sandbox.sources = [{'uid': FIXTURE.DATASOURCE_UID, 'type': 'prometheus',
                            'access': 'proxy', 'url': 'https://outside.invalid'}]
        with self.assertRaises(ValueError):
            self.check(sandbox)
        self.assertFalse(any(payload is not None for _, payload in sandbox.calls))

    def test_external_origin_and_non_owned_prometheus_alias_rejected_before_http(self):
        for origin, url in (('https://outside.invalid', FIXTURE.PROMETHEUS_URL),
                            ('http://127.0.0.1:45678', 'http://outside.invalid:9090')):
            sandbox = FakeSandbox()
            with self.subTest(origin=origin, url=url), self.assertRaises(ValueError):
                FIXTURE.check(sandbox, origin, 'fixture-admin:synthetic', url)
            self.assertEqual(sandbox.calls, [])


if __name__ == '__main__':
    unittest.main()
