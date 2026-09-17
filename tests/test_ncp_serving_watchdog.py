import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ncp_serving_watchdog', ROOT / 'scripts/ncp-serving-watchdog.py')
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)

PUBLIC = ('edge', 'proxy', 'user', 'yolo')
CONTAINERS = {name: {'container_id': str(index + 1) * 64,
                     'image_id': 'sha256:' + chr(ord('a') + index) * 64}
              for index, name in enumerate(PUBLIC)}


class JobError(Exception):
    pass


class Backend:
    """Records what the watchdog asked Docker to do; nothing is ever launched."""

    def __init__(self, running, replaced=(), missing=()):
        self.running, self.replaced, self.missing = dict(running), set(replaced), set(missing)
        self.started = []

    def docker(self, args, **kwargs):
        name = next(key for key, entry in CONTAINERS.items() if entry['container_id'] == args[-1])
        if args[0] == 'start':
            self.started.append(name)
            self.running[name] = True
            return ''
        if name in self.missing:
            raise RuntimeError('no such container')
        return json.dumps({'id': CONTAINERS[name]['container_id'],
                           'image': ('sha256:' + 'f' * 64) if name in self.replaced else CONTAINERS[name]['image_id'],
                           'running': self.running[name]})


def receipt(**overrides):
    value = {'status': 'PUBLIC_READY', 'public_serving': 'OPEN', 'resume_public': True,
             'containers': {name: dict(entry) for name, entry in CONTAINERS.items()}}
    value.update(overrides)
    return value


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.locks = 0
        self.lock_fails = False

        @contextmanager
        def writer(path):
            assert path == self.state
            if self.lock_fails:
                raise JobError('another_migration_launcher_active')
            self.locks += 1
            yield

        self.serving = SimpleNamespace(
            PUBLIC_SERVICES=PUBLIC, STATE=self.state, STATE_NAME='serving-state.json',
            receiver=SimpleNamespace(
                HEX=re.compile(r'[a-f0-9]{64}'), IMAGE=re.compile(r'sha256:[a-f0-9]{64}'),
                writer=writer, migration=SimpleNamespace(JobError=JobError),
                private=SimpleNamespace(read_json=lambda path, private: json.loads(path.read_text()))))

    def write(self, value):
        (self.state / 'serving-state.json').write_text(json.dumps(value))

    def test_all_running_does_nothing_and_never_takes_the_lock(self):
        self.write(receipt())
        backend = Backend({name: True for name in PUBLIC})
        self.assertEqual(watchdog.run(self.serving, backend),
                         {'action': 'none', 'reason': 'no_stopped_container'})
        self.assertEqual((backend.started, self.locks), ([], 0))

    def test_a_stopped_container_is_started_back(self):
        self.write(receipt())
        backend = Backend({**{name: True for name in PUBLIC}, 'edge': False})
        self.assertEqual(watchdog.run(self.serving, backend),
                         {'action': 'started', 'started': ['edge'], 'failed': []})
        self.assertEqual((backend.started, self.locks), (['edge'], 1))

    def test_a_deliberately_stopped_receipt_never_reopens_anything(self):
        for override in ({'public_serving': 'HOLD', 'status': 'PUBLIC_STOPPED'},
                         {'resume_public': False}, {'status': 'SERVING_FAILED'}):
            with self.subTest(override=override):
                self.write(receipt(**override))
                backend = Backend({**{name: True for name in PUBLIC}, 'edge': False})
                self.assertEqual(watchdog.run(self.serving, backend),
                                 {'action': 'none', 'reason': 'not_publicly_serving'})
                self.assertEqual((backend.started, self.locks), ([], 0))

    def test_a_replaced_or_missing_container_is_left_alone(self):
        self.write(receipt())
        for kwargs in ({'replaced': ['edge']}, {'missing': ['edge']}):
            with self.subTest(kwargs=kwargs):
                backend = Backend({**{name: True for name in PUBLIC}, 'edge': False}, **kwargs)
                self.assertEqual(watchdog.run(self.serving, backend),
                                 {'action': 'none', 'reason': 'no_stopped_container'})
                self.assertEqual((backend.started, self.locks), ([], 0))

    def test_a_running_deployment_holds_the_lock_and_the_watchdog_backs_off(self):
        self.write(receipt())
        self.lock_fails = True
        backend = Backend({**{name: True for name in PUBLIC}, 'edge': False})
        self.assertEqual(watchdog.run(self.serving, backend),
                         {'action': 'none', 'reason': 'deployment_lock_held'})
        self.assertEqual(backend.started, [])

    def test_a_missing_or_malformed_receipt_authorizes_nothing(self):
        backend = Backend({name: True for name in PUBLIC})
        self.assertEqual(watchdog.run(self.serving, backend),
                         {'action': 'none', 'reason': 'not_publicly_serving'})
        for value in (receipt(containers={}), receipt(containers={'edge': {'container_id': 'nope'}})):
            with self.subTest(value=value):
                self.write(value)
                self.assertEqual(watchdog.run(self.serving, backend),
                                 {'action': 'none', 'reason': 'not_publicly_serving'})
        self.assertEqual((backend.started, self.locks), ([], 0))


if __name__ == '__main__':
    unittest.main()
