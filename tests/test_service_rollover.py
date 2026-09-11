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


class DrainTests(unittest.TestCase):
    """A service allowed minutes to finish must get them as its copy goes away."""

    def test_the_configured_grace_is_read_in_the_forms_compose_writes(self):
        for raw, expected in (('5m20s', 320), ('2m', 120), ('90s', 90), ('1h', 3600),
                              ('1h2m3s', 3723)):
            with self.subTest(raw=raw):
                self.assertEqual(roll.stop_seconds({'stop_grace_period': raw}), expected)

    def test_a_service_without_one_keeps_the_ordinary_wait(self):
        self.assertEqual(roll.stop_seconds({}), roll.STOP_SECONDS)
        self.assertEqual(roll.stop_seconds({'stop_grace_period': '5s'}), roll.STOP_SECONDS)

    def test_an_unreadable_grace_stops_the_replacement(self):
        with self.assertRaisesRegex(roll.RolloverError, 'duration_unreadable'):
            roll.stop_seconds({'stop_grace_period': 'a while'})

    def test_the_temporary_container_is_created_with_that_wait(self):
        entry, image = roll.service_config(config(), 'hub')
        args = roll.create_args('map-test-hub-rollover', 'map-test', 'hub', image, entry,
                                ['map-test_default'], 320)
        self.assertEqual(args[args.index('--stop-timeout') + 1], '320')

    def test_a_service_that_names_its_own_process_is_refused(self):
        for key in ('command', 'entrypoint'):
            broken = config()
            broken['services']['hub'][key] = ['sh', '-c', 'something else']
            with self.subTest(key=key), self.assertRaisesRegex(
                    roll.RolloverError, 'service_overrides_its_own_process'):
                roll.service_config(broken, 'hub')


class AdmissionTests(unittest.TestCase):
    """The reading the admission check depends on has to survive real output."""

    REAL = {'259MiB / 3GiB': 259 * 1024 ** 2,
            '1.5GiB / 3GiB': int(1.5 * 1024 ** 3),
            '54.32MiB / 384MiB': int(54.32 * 1024 ** 2),
            '812KiB / 64MiB': 812 * 1024,
            '512B / 64MiB': 512}

    def test_every_size_docker_prints_is_read_back(self):
        for raw, expected in self.REAL.items():
            with self.subTest(raw=raw), patch.object(roll, 'docker', return_value=raw):
                self.assertEqual(roll.working_set(BLUE), expected)

    def test_an_unreadable_reading_stops_the_replacement(self):
        for raw in ('', '-- / --', 'plenty / 3GiB', '259 Zib / 3GiB'):
            with self.subTest(raw=raw), patch.object(roll, 'docker', return_value=raw):
                with self.assertRaisesRegex(roll.RolloverError, 'memory_usage_unreadable'):
                    roll.working_set(BLUE)

    def test_a_host_with_room_admits_and_reports_what_it_measured(self):
        with patch.object(roll, 'docker', return_value='259MiB / 3GiB'), \
                patch.object(roll, 'memory_available', return_value=2 * 1024 ** 3):
            report = roll.admission(BLUE)
        self.assertEqual(report['working_set_bytes'], 259 * 1024 ** 2)
        self.assertEqual(report['required_bytes'],
                         int(259 * 1024 ** 2 * 1.5) + roll.PROTECTED_RESERVE)

    def test_a_host_without_room_is_refused_on_the_measured_size(self):
        with patch.object(roll, 'docker', return_value='1.5GiB / 3GiB'), \
                patch.object(roll, 'memory_available', return_value=600 * 1024 ** 2):
            with self.assertRaisesRegex(roll.RolloverError, 'insufficient_memory_for_second_container'):
                roll.admission(BLUE)



class ReadinessTests(unittest.TestCase):
    """The copy has to be judged by the same test as the container it replaces."""

    CHECK = {'test': ['CMD-SHELL', 'wget -qO- http://localhost:8080/actuator/health | grep -q UP'],
             'interval': '15s', 'timeout': '3s', 'retries': 5, 'start_period': '40s'}

    def test_the_configured_test_is_carried_over_with_its_timings(self):
        args = roll.health_args({'healthcheck': self.CHECK})
        self.assertEqual(args[args.index('--health-cmd') + 1], self.CHECK['test'][1])
        self.assertEqual(args[args.index('--health-interval') + 1], '15s')
        self.assertEqual(args[args.index('--health-timeout') + 1], '3s')
        self.assertEqual(args[args.index('--health-start-period') + 1], '40s')
        self.assertEqual(args[args.index('--health-retries') + 1], '5')

    def test_a_service_without_one_carries_nothing(self):
        self.assertEqual(roll.health_args({}), [])
        self.assertEqual(roll.health_args({'healthcheck': {'test': ['NONE']}}), ['--no-healthcheck'])
        self.assertEqual(roll.health_args({'healthcheck': {'test': ['CMD-SHELL', 'x'],
                                                           'disable': True}}), [])

    def test_a_test_that_cannot_be_reproduced_is_refused(self):
        for test in (['CMD', 'wget', '-q', 'http://x'], ['CMD-SHELL'], ['CMD-SHELL', 1]):
            with self.subTest(test=test), self.assertRaisesRegex(
                    roll.RolloverError, 'healthcheck_cannot_be_reproduced'):
                roll.health_args({'healthcheck': {'test': test}})

    def test_the_temporary_container_is_created_with_it(self):
        entry, image = roll.service_config(config(), 'hub')
        entry['healthcheck'] = self.CHECK
        args = roll.create_args('map-test-hub-rollover', 'map-test', 'hub', image, entry,
                                ['map-test_default'])
        self.assertIn('--health-cmd', args)

    def test_a_copy_that_is_still_starting_is_given_time_to_answer(self):
        answers = [0, 0, 200]
        opened = []

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def urlopen(url, timeout=None):
            opened.append(url)
            value = answers.pop(0)
            if value != 200:
                raise OSError('not yet')
            return Response()

        with patch.object(roll.urllib.request, 'urlopen', side_effect=urlopen), \
                patch.object(roll.time, 'sleep'):
            self.assertEqual(roll.probe('10.0.0.5', 'hub', seconds=60), 200)
        self.assertEqual(len(opened), 3)

    def test_a_copy_that_never_answers_is_reported_as_such(self):
        clock = iter([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 99, 99, 99])
        with patch.object(roll.urllib.request, 'urlopen', side_effect=OSError('no')), \
                patch.object(roll.time, 'sleep'), \
                patch.object(roll.time, 'monotonic', side_effect=lambda: next(clock)):
            self.assertEqual(roll.probe('10.0.0.5', 'hub', seconds=5), 0)


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
                patch.object(roll, 'upstream_source', return_value=d), \
                patch.object(roll.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(roll.RolloverError, 'proxy_configuration_rejected'):
                roll.reload_proxy('map-test', Path(d), 'hub', 'map-test-hub-rollover')
            self.assertEqual(list(Path(d).iterdir()), [])
            self.assertFalse(any(c[3:5] == ['nginx', '-s'] for c in calls))

    def test_returning_traffic_removes_the_override_file(self):
        with tempfile.TemporaryDirectory() as d, patch.object(roll, 'container_id', return_value=PROXY), \
                patch.object(roll, 'upstream_source', return_value=d), \
                patch.object(roll, 'docker', return_value=''), \
                patch.object(roll.subprocess, 'run', return_value=type('R', (), {'returncode': 0, 'stdout': ''})()):
            path = Path(d)/'hub.conf'
            roll.reload_proxy('map-test', Path(d), 'hub', 'map-test-hub-rollover')
            self.assertTrue(path.exists())
            roll.reload_proxy('map-test', Path(d), 'hub', None)
            self.assertFalse(path.exists())

    def test_a_proxy_reading_another_directory_stops_before_anything_moves(self):
        with tempfile.TemporaryDirectory() as ours, tempfile.TemporaryDirectory() as theirs, \
                patch.object(roll, 'container_id', return_value=PROXY), \
                patch.object(roll, 'upstream_source', return_value=theirs), \
                patch.object(roll.subprocess, 'run') as run:
            with self.assertRaisesRegex(roll.RolloverError, 'proxy_reads_a_different_upstream_directory'):
                roll.reload_proxy('map-test', Path(ours), 'hub', 'map-test-hub-rollover')
            self.assertEqual(list(Path(ours).iterdir()), [])
            run.assert_not_called()

    def test_a_proxy_without_the_mount_stops_the_switch(self):
        with tempfile.TemporaryDirectory() as d, patch.object(roll, 'container_id', return_value=PROXY), \
                patch.object(roll, 'upstream_source', return_value=''), \
                patch.object(roll.subprocess, 'run') as run:
            with self.assertRaises(roll.RolloverError):
                roll.reload_proxy('map-test', Path(d), 'hub', 'map-test-hub-rollover')
            run.assert_not_called()

    def test_the_mount_is_read_from_the_upstream_destination(self):
        seen = []
        with patch.object(roll, 'inspect', side_effect=lambda cid, template: seen.append(template) or '/host/dir'):
            self.assertEqual(roll.upstream_source(PROXY), '/host/dir')
        self.assertIn('/etc/nginx/upstreams', seen[0])
        self.assertIn('.Mounts', seen[0])
        self.assertIn('.Source', seen[0])


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
                patch.object(roll, 'upstream_source', side_effect=lambda cid: str(self.upstreams)), \
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


    def test_the_canonical_container_is_always_actually_replaced(self):
        # Compose leaves a container alone when it sees no change, and the caller
        # then reports that the canonical container was never replaced.
        seen = []
        with patch.object(roll.subprocess, 'run', side_effect=lambda args, **kw: seen.append(args)):
            roll.compose_recreate(['docker', 'compose'], 'hub')
        self.assertIn('--force-recreate', seen[0])
        self.assertIn('--no-deps', seen[0])
        self.assertIn('never', seen[0])
        self.assertEqual(seen[0][-1], 'hub')


if __name__ == '__main__':
    unittest.main()
