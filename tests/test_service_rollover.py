import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('service_rollover', ROOT/'scripts/service-rollover.py')
roll = importlib.util.module_from_spec(spec); spec.loader.exec_module(roll)
IMAGE = 'ghcr.io/we-meet-trip/map-service-hub@sha256:'+'a'*64
IMAGE_ID = 'sha256:'+'b'*64
BLUE = 'b'*64; GREEN = 'g'.replace('g','c')*64; NEW = 'd'*64; PROXY = 'e'*64
PROBES = [('http://127.0.0.1:8090/healthz', 200)]


def config():
    return {'services': {'hub': {'image': IMAGE, 'environment': {'HUB_DATABASE_URL': 'x', 'LOG_LEVEL': 'INFO'},
                                 'deploy': {'resources': {'limits': {'memory': '384m', 'cpus': '1.0'}}},
                                 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
                                 'ports': [{'published': '8201', 'target': 8000}]}}}


class ArgumentTests(unittest.TestCase):
    def test_temporary_container_copies_configuration_but_never_publishes_a_port(self):
        entry, image = roll.service_config(config(), 'hub')
        args = roll.create_args('map-test-hub-rollover', 'map-test', 'hub', image, entry, ['map-test_default'])
        self.assertEqual(args[-1], IMAGE)
        self.assertNotIn('--publish', args)
        self.assertNotIn('-p', args)
        self.assertNotIn('--network-alias', args)
        self.assertIn('--restart=no', args)
        self.assertIn('--env', args)
        self.assertIn('HUB_DATABASE_URL=x', args)
        self.assertIn('384m', args)
        self.assertIn('1.0', args)
        self.assertIn('no-new-privileges:true', args)
        self.assertEqual(args[args.index('--network')+1], 'map-test_default')

    def test_a_mutable_or_foreign_image_is_refused(self):
        for image in ('hub:latest', 'ghcr.io/other/map-service-hub@sha256:'+'a'*64):
            broken = config(); broken['services']['hub']['image'] = image
            with self.subTest(image=image), self.assertRaises(roll.RolloverError):
                roll.service_config(broken, 'hub')

    def test_each_service_switches_only_its_own_upstream_name(self):
        self.assertEqual(roll.upstream_body('hub', 'map-test-hub-rollover'),
                         '# Written by a running deployment; removed when it finishes.\n'
                         'set $hub_upstream http://map-test-hub-rollover:8000;\n')
        self.assertIn('$bff_upstream', roll.upstream_body('user', 'x'))
        self.assertIn(':8080', roll.upstream_body('user', 'x'))
        self.assertIn('$vision_upstream', roll.upstream_body('yolo', 'x'))


class ProxyTests(unittest.TestCase):
    def test_a_rejected_configuration_is_removed_before_any_reload(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            class Result:
                returncode = 1 if args[3:5] == ['nginx', '-t'] else 0
                stdout = ''
            return Result()
        with tempfile.TemporaryDirectory() as d, patch.object(roll, 'container_id', return_value=PROXY), \
                patch.object(roll.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(roll.RolloverError, 'proxy_configuration_rejected'):
                roll.reload_proxy('map-test', Path(d), 'hub', 'map-test-hub-rollover')
            self.assertEqual(list(Path(d).iterdir()), [])
            self.assertFalse(any(c[3:5] == ['nginx', '-s'] for c in calls))

    def test_returning_traffic_removes_the_override_file(self):
        with tempfile.TemporaryDirectory() as d, patch.object(roll, 'container_id', return_value=PROXY), \
                patch.object(roll, 'docker', return_value=''), \
                patch.object(roll.subprocess, 'run', return_value=type('R', (), {'returncode': 0, 'stdout': ''})()):
            path = Path(d)/'hub.conf'
            roll.reload_proxy('map-test', Path(d), 'hub', 'map-test-hub-rollover')
            self.assertTrue(path.exists())
            roll.reload_proxy('map-test', Path(d), 'hub', None)
            self.assertFalse(path.exists())


class RolloverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.upstreams = Path(self.temp.name)
        self.calls = []
        self.canonical = BLUE
        self.states = {BLUE: {'id': BLUE, 'image': IMAGE_ID, 'running': True, 'health': 'healthy'},
                       GREEN: {'id': GREEN, 'image': IMAGE_ID, 'running': True, 'health': 'healthy'},
                       NEW: {'id': NEW, 'image': IMAGE_ID, 'running': True, 'health': 'healthy'}}

    def docker(self, args, timeout=60):
        self.calls.append(args)
        if args[:2] == ['image', 'inspect']:
            return IMAGE_ID
        if args[0] == 'ps':
            return '' if '^map-test-hub-rollover$' in ' '.join(args) else self.canonical
        if args[0] == 'create':
            return GREEN
        if args[0] in ('start', 'stop', 'rm', 'exec'):
            return ''
        if args[0] == 'network':
            return ''
        self.fail('unexpected docker command: %r' % (args,))

    def recreate(self, service):
        self.calls.append(['recreate', service])
        self.canonical = NEW

    def run_rollover(self, *, recreate=None, probe_status=200, public=True, report=None):
        with patch.object(roll, 'docker', side_effect=self.docker), \
                patch.object(roll, 'container_id',
                             side_effect=lambda p, s: PROXY if s == 'proxy' else self.canonical), \
                patch.object(roll, 'state', side_effect=lambda cid: self.states[cid]), \
                patch.object(roll, 'inspect', return_value='map-test_default'), \
                patch.object(roll, 'address', return_value='10.0.0.5'), \
                patch.object(roll, 'admission', return_value={'ok': True}), \
                patch.object(roll, 'probe', return_value=probe_status), \
                patch.object(roll, 'public_ok', return_value=(public, '' if public else 'url')), \
                patch.object(roll.subprocess, 'run',
                             return_value=type('R', (), {'returncode': 0, 'stdout': ''})()), \
                patch.object(roll.time, 'sleep'):
            return roll.rollover(config(), 'map-test', 'hub', self.upstreams, PROBES,
                                 recreate or self.recreate, settle=0, report=report)

    def steps(self, report):
        return [s.get('step') or ('switch:' + s['target']) for s in report['steps']]

    def test_traffic_moves_to_the_new_container_before_the_old_one_is_replaced(self):
        report = self.run_rollover()
        self.assertEqual(report['status'], 'PASS')
        order = self.steps(report)
        self.assertEqual(order[:5], ['green_started', 'green_healthy', 'green_answered',
                                     'switch:map-test-hub-rollover', 'traffic_on_green'])
        self.assertIn('canonical_recreate_requested', order)
        self.assertLess(order.index('switch:map-test-hub-rollover'),
                        order.index('canonical_recreate_requested'))
        self.assertLess(order.index('canonical_recreate_requested'), order.index('switch:canonical'))
        self.assertEqual(order[-1], 'green_removed')
        self.assertEqual(list(self.upstreams.iterdir()), [])
        recreate_at = next(i for i, c in enumerate(self.calls) if c[0] == 'recreate')
        create_at = next(i for i, c in enumerate(self.calls) if c[0] == 'create')
        self.assertLess(create_at, recreate_at)

    def test_a_new_container_that_never_answers_leaves_traffic_where_it_was(self):
        with self.assertRaisesRegex(roll.RolloverError, 'did_not_answer'):
            self.run_rollover(probe_status=503)
        self.assertEqual(list(self.upstreams.iterdir()), [])
        self.assertTrue(any(c[0] == 'rm' for c in self.calls))
        self.assertFalse(any(c[0] == 'recreate' for c in self.calls))

    def test_a_failed_replacement_returns_traffic_and_removes_the_temporary_container(self):
        def failing(service):
            self.calls.append(['recreate', service])
            raise RuntimeError('compose refused')
        report = {}
        with self.assertRaises(RuntimeError):
            self.run_rollover(recreate=failing, report=report)
        self.assertEqual(list(self.upstreams.iterdir()), [])
        self.assertIn('traffic_returned_after_failure', self.steps(report))
        self.assertIn('green_removed', self.steps(report))
        self.assertEqual(report['status'], 'FAIL')

    def test_a_public_failure_right_after_the_switch_stops_the_deployment(self):
        report = {}
        with self.assertRaisesRegex(roll.RolloverError, 'public_probe_failed_after_switch'):
            self.run_rollover(public=False, report=report)
        self.assertEqual(list(self.upstreams.iterdir()), [])
        self.assertFalse(any(c[0] == 'recreate' for c in self.calls))

    def test_a_host_without_room_for_a_second_container_changes_nothing(self):
        with patch.object(roll, 'docker', side_effect=self.docker), \
                patch.object(roll, 'container_id', side_effect=lambda p, s: self.canonical), \
                patch.object(roll, 'state', side_effect=lambda cid: self.states[cid]), \
                patch.object(roll, 'working_set', return_value=400 * 1024**2), \
                patch.object(roll, 'memory_available', return_value=100 * 1024**2), \
                patch.object(roll.shutil, 'disk_usage',
                             return_value=type('U', (), {'free': 50 * 1024**3})()):
            with self.assertRaisesRegex(roll.RolloverError, 'insufficient_memory'):
                roll.rollover(config(), 'map-test', 'hub', self.upstreams, PROBES, self.recreate)
        self.assertFalse(any(c[0] == 'create' for c in self.calls))

    def test_a_leftover_temporary_container_blocks_a_new_attempt(self):
        def docker(args, timeout=60):
            self.calls.append(args)
            if args[:2] == ['image', 'inspect']:
                return IMAGE_ID
            if args[0] == 'ps':
                return GREEN if '^map-test-hub-rollover$' in ' '.join(args) else self.canonical
            self.fail('must not create beside an existing temporary container')
        with patch.object(roll, 'docker', side_effect=docker), \
                patch.object(roll, 'container_id', side_effect=lambda p, s: self.canonical), \
                patch.object(roll, 'state', side_effect=lambda cid: self.states[cid]), \
                patch.object(roll, 'inspect', return_value='map-test_default'), \
                patch.object(roll, 'admission', return_value={}):
            with self.assertRaisesRegex(roll.RolloverError, 'previous_rollover_container_present'):
                roll.rollover(config(), 'map-test', 'hub', self.upstreams, PROBES, self.recreate)


if __name__ == '__main__':
    unittest.main()
