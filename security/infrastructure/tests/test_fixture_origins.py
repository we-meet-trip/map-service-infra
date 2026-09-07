import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('origin_fixtures',
    Path(__file__).resolve().parents[1] / 'fixtures.py')
F = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(F)


class FixtureOriginTests(unittest.TestCase):
    def setUp(self):
        self.s = F.Sandbox(Path('/unused'))
        self.name = 'map-infra-' + self.s.token + '-test'
        self.network = 'map-infra-' + self.s.token + '-network'
        self.s.containers = [self.name]
        self.s.networks = [self.network]
        self.container = {'Id': 'a' * 64, 'Name': '/' + self.name,
            'Config': {'Labels': {'map.infra.fixture': self.s.token},
                       'Env': ['PRIVATE_SENTINEL=never-output']},
            'State': {'Running': True, 'Status': 'running', 'ExitCode': 0},
            'HostConfig': {'PortBindings': {'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': ''}]}},
            'NetworkSettings': {'Ports': {}, 'Networks': {self.network: {
                'NetworkID': 'b' * 64, 'IPAddress': '172.19.0.2'}}}}
        self.net = {'Id': 'b' * 64, 'Name': self.network, 'Internal': True,
            'Driver': 'bridge', 'Labels': {'map.infra.fixture': self.s.token},
            'Containers': {'a' * 64: {'Name': self.name, 'IPv4Address': '172.19.0.2/16'}}}
        self.patch = patch.object(self.s, 'run', side_effect=self.fake_run)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def fake_run(self, args, **kwargs):
        if args == ['docker', 'inspect', self.name]:
            return json.dumps([self.container]).encode()
        if args == ['docker', 'network', 'inspect', self.network]:
            return json.dumps([self.net]).encode()
        raise AssertionError(args)

    def test_docker28_internal_empty_published_ports_uses_owned_bridge(self):
        origin = self.s.origin(self.name, 3000)
        self.assertEqual(origin, 'http://172.19.0.2:3000')
        self.assertTrue(self.s.owns_origin(origin))
        self.assertFalse(self.s.owns_origin('http://172.19.0.3:3000'))
        self.assertEqual(self.s.origins[origin]['mode'], 'owned_internal_bridge')

    def test_public_or_unowned_network_rejected_even_with_loopback_mapping(self):
        for mutation in ('external', 'foreign_label', 'extra_network', 'wrong_driver', 'wrong_network_id'):
            container, net = copy.deepcopy(self.container), copy.deepcopy(self.net)
            if mutation == 'external': self.net['Internal'] = False
            elif mutation == 'foreign_label': self.net['Labels'] = {}
            elif mutation == 'extra_network': self.container['NetworkSettings']['Networks']['bridge'] = {}
            elif mutation == 'wrong_driver': self.net['Driver'] = 'host'
            else: self.net['Id'] = 'c' * 64
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.s.origin(self.name, 3000)
            self.container, self.net = container, net

    def test_global_metadata_reserved_and_mismatched_member_addresses_rejected(self):
        for address in ('8.8.8.8', '169.254.169.254', '127.0.0.1', '0.0.0.0',
                        '224.0.0.1', '198.18.0.1', '192.0.0.1', '172.19.0.3'):
            self.container['NetworkSettings']['Networks'][self.network]['IPAddress'] = address
            with self.subTest(address=address), self.assertRaises(ValueError):
                self.s.origin(self.name, 3000)

    def test_internal_network_cannot_hide_wildcard_or_malformed_publication(self):
        for binding in ({'HostIp': '0.0.0.0', 'HostPort': '45678'},
                        {'HostIp': '127.0.0.1', 'HostPort': ''},
                        {'HostIp': '127.0.0.1', 'HostPort': '65536'}):
            self.container['NetworkSettings']['Ports'] = {'3000/tcp': [binding]}
            with self.subTest(binding=binding), self.assertRaisesRegex(ValueError, 'unexpected_publication'):
                self.s.origin(self.name, 3000)

    def test_foreign_or_stopped_or_unregistered_container_rejected(self):
        for mutation in ('owner', 'stopped', 'unregistered'):
            container = copy.deepcopy(self.container)
            if mutation == 'owner': self.container['Config']['Labels'] = {}
            elif mutation == 'stopped': self.container['State']['Running'] = False
            else: self.s.containers = []
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.s.origin(self.name, 3000)
            self.container = container
            self.s.containers = [self.name]

    def test_restart_or_container_replacement_revokes_previous_origin(self):
        origin = self.s.origin(self.name, 3000)
        self.container['State']['Running'] = False
        self.assertFalse(self.s.owns_origin(origin))
        self.container['State']['Running'] = True
        self.container['Id'] = 'c' * 64
        self.net['Containers']['c' * 64] = self.net['Containers'].pop('a' * 64)
        self.assertFalse(self.s.owns_origin(origin))

    def test_default_bridge_still_requires_exact_loopback_binding(self):
        settings = self.container['NetworkSettings']
        settings['Networks'] = {'bridge': {}}
        settings['Ports'] = {'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '45678'}]}
        origin = self.s.origin(self.name, 3000)
        self.assertEqual(origin, 'http://127.0.0.1:45678')
        settings['Ports']['3000/tcp'][0]['HostPort'] = '45679'
        self.assertFalse(self.s.owns_origin(origin))
        settings['Ports']['3000/tcp'][0]['HostIp'] = '0.0.0.0'
        with self.assertRaises(ValueError): self.s.origin(self.name, 3000)
        settings['Ports'] = {}
        with self.assertRaisesRegex(ValueError, 'loopback_binding_missing'):
            self.s.origin(self.name, 3000)

    def test_request_rejects_unissued_origin_and_blocks_proxy_redirects(self):
        with patch.object(F.urllib.request, 'build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'origin_not_owned'):
                self.s.request('http://172.19.0.2:3000', '/')
            opener.assert_not_called()
            origin = self.s.origin(self.name, 3000)
            self.s.request(origin, '/api/health', credentials='fixture-admin:synthetic')
            handlers = opener.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], F.NoFixtureRedirect)
            with self.assertRaisesRegex(ValueError, 'redirect_refused'):
                handlers[1].redirect_request(None, None, 302, '', {}, 'https://outside.invalid')

    def test_diagnostics_exclude_environment_and_raw_error_details(self):
        states = self.s.diagnostics()
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(states))
        self.assertNotIn('never-output', json.dumps(states))
        self.assertEqual(states[0]['state']['Status'], 'running')


if __name__ == '__main__': unittest.main()
