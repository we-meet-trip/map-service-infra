"""Request-log archive: no Docker daemon, network or cloud account.

Docker is a stand-in that keeps timestamped records per container and answers
`docker logs -t --since --until` like the engine (both ends inclusive, over-long messages as
16 KiB fragments). The metadata server and Cloud Storage are a stand-in opener with create-only
objects (a second create of the same name is 412). age runs for real when installed.
"""
import base64
import datetime as dt
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import request_log_archive as archive

AGE = shutil.which('age')
AGE_KEYGEN = shutil.which('age-keygen')
START = 1_791_000_000_000_000_000
ENVELOPE = 'v1.AbCdEfGhIjKlMnOp.' + 'Q1w2E3r4T5y6U7i8O9p0-_aSdFgHjKl'
CONFIG = {'gcp_project': 'mapcenter-b59ca', 'instance': 'map-prod', 'bucket': 'map-prod-archive',
          'prefix': 'log-6m/requests/', 'recipient': 'age1' + 'q' * 58, 'compose_project': 'map-prod',
          'services': ['hub', 'proxy'], 'settle_seconds': 10,
          'log_config': {'type': 'json-file', 'max-size': '10m', 'max-file': '3'}}


def stamp(ns):
    return archive.ns_to_rfc3339(ns).encode()


class FakeDocker:
    def __init__(self):
        self.containers = {'hub': 'c' * 64, 'proxy': 'd' * 64}
        self.records = {cid: [] for cid in self.containers.values()}
        self.log_config = {'Type': 'json-file', 'Config': {'max-size': '10m', 'max-file': '3'}}
        self.identity = None

    def add(self, service, ns, text, stream='stdout'):
        self.records[self.containers[service]].append((ns, stream, text.encode()))

    def __call__(self, argv, stdin=None):
        if argv[0] == archive.AGE:
            if AGE:
                done = subprocess.run([AGE] + argv[1:], input=stdin, capture_output=True)
                return done.returncode, done.stdout, done.stderr
            return 0, b'age-encryption.org/v1\n' + stdin, b''
        command = argv[1]
        if command == 'ps':
            service = argv[-1].rsplit('=', 1)[1]
            cid = self.containers.get(service)
            return 0, (cid + '\n').encode() if cid else b'', b''
        if command == 'inspect':
            return 0, json.dumps(self.log_config).encode(), b''
        if command == 'logs':
            since, until, cid = argv[4], argv[6], argv[7]
            since_ns, until_ns = (int(value.replace('.', '')) for value in (since, until))
            out, err = io.BytesIO(), io.BytesIO()
            for ns, stream, text in self.records.get(cid, []):
                if since_ns <= ns <= until_ns:
                    target = out if stream == 'stdout' else err
                    pieces = [text[i:i + 16384] for i in range(0, len(text), 16384)] or [b'']
                    target.write(b''.join(stamp(ns + index) + b' ' + piece for index, piece in enumerate(pieces)) + b'\n')
            return 0, out.getvalue(), err.getvalue()
        raise AssertionError(argv)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCloud:
    def __init__(self):
        self.objects, self.statuses = {}, []
        self.deny = False

    def open(self, request, timeout=None):
        url = request.full_url
        if url.startswith(archive.METADATA):
            path = url[len(archive.METADATA):]
            answers = {'/project/project-id': 'mapcenter-b59ca', '/instance/name': 'map-prod',
                       '/instance/service-accounts/default/token': json.dumps({'access_token': 'token'})}
            return FakeResponse(answers[path].encode())
        assert url.startswith('https://storage.googleapis.com/upload/storage/v1/b/map-prod-archive/o?')
        assert 'ifGenerationMatch=0' in url and request.get_method() == 'POST'
        boundary = request.headers['Content-type'].split('boundary=')[1].encode()
        parts = request.data.split(b'--' + boundary)
        meta = json.loads(parts[1].split(b'\r\n\r\n', 1)[1].rstrip(b'\r\n'))
        data = parts[2].split(b'\r\n\r\n', 1)[1][:-2]
        if self.deny:
            raise urllib.error.HTTPError(url, 403, 'Forbidden', {}, None)
        if meta['name'] in self.objects:
            raise urllib.error.HTTPError(url, 412, 'Precondition Failed', {}, None)
        assert meta['md5Hash'] == base64.b64encode(hashlib.md5(data).digest()).decode()
        self.objects[meta['name']] = {'data': data, 'customTime': meta['customTime']}
        return FakeResponse(json.dumps({'name': meta['name'], 'md5Hash': meta['md5Hash']}).encode())


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.config = dict(CONFIG)
        self.identity = None
        if AGE and AGE_KEYGEN:
            key = subprocess.run([AGE_KEYGEN], capture_output=True, text=True, check=True).stdout
            self.identity = self.state / 'identity'
            self.identity.write_text(key)
            self.config['recipient'] = next(line.split(': ')[1] for line in key.splitlines() if 'public key' in line)
        self.docker, self.cloud = FakeDocker(), FakeCloud()
        self.clock = [START + 600 * 10**9]
        self.archive = archive.Archive(self.config, archive.Host(self.config, self.docker, self.cloud), self.state,
                                       now_ns=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def documents(self, name):
        data = self.cloud.objects[name]['data']
        if self.identity:
            data = subprocess.run([AGE, '--decrypt', '--identity', str(self.identity)], input=data,
                                  capture_output=True, check=True).stdout
        else:
            data = data.split(b'\n', 1)[1]
        return [json.loads(line) for line in gzip.decompress(data).decode().splitlines()]

    def test_run_refuses_until_armed(self):
        with self.assertRaisesRegex(archive.ArchiveError, 'not_armed'):
            self.archive.run()

    def test_arm_refuses_a_future_start_and_a_second_arm(self):
        with self.assertRaisesRegex(archive.ArchiveError, 'start_in_future'):
            self.archive.arm(archive.ns_to_rfc3339(self.clock[0] + 10**9))
        self.archive.arm(archive.ns_to_rfc3339(START))
        with self.assertRaisesRegex(archive.ArchiveError, 'already_armed'):
            self.archive.arm(archive.ns_to_rfc3339(START))

    def test_nothing_before_the_armed_cursor_is_shipped_and_lines_are_masked(self):
        self.docker.add('hub', START - 5 * 10**9, 'GET /v1/weather/now?lat=37.566500&lng=126.978000 before arming')
        self.docker.add('hub', START + 10**9, f'GET /v1/weather/now?loc={ENVELOPE} HTTP/1.1" 200')
        self.docker.add('hub', START + 2 * 10**9, 'HTTP Request: GET http://osrm-foot:5000/route/v1/foot/127.0276,37.4979;126.978,37.5665', 'stderr')
        self.docker.add('proxy', START + 3 * 10**9, 'request: "GET /api/v1/bikes?latitude=37.49795&longitude=127.02761 HTTP/1.1"')
        self.archive.arm(archive.ns_to_rfc3339(START))
        results = self.archive.run()
        self.assertEqual({name: code for name, (code, _) in results.items()}, {'hub': 'ok', 'proxy': 'ok'})
        hub = results['hub'][1]
        self.assertEqual((hub['lines'], hub['masked'], hub['status']), (2, 2, 200))
        documents = self.documents(hub['object'])
        self.assertEqual([doc['stream'] for doc in documents], ['stdout', 'stderr'])
        text = json.dumps(documents)
        for value in ('37.5665', '126.978', '127.0276', '37.4979', 'AbCdEfGhIjKlMnOp', 'before arming'):
            self.assertNotIn(value, text)
        self.assertRegex(hub['object'], r'^log-6m/requests/\d{4}/\d{2}/\d{2}/hub/\d{6}\.\d{9}Z\.ndjson\.gz\.age$')
        self.assertEqual(self.cloud.objects[hub['object']]['customTime'], archive.ns_to_rfc3339(self.clock[0]))

    def test_windows_join_without_overlap_or_gap(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        until = self.clock[0] - 10 * 10**9
        self.docker.add('hub', until, 'last line of window one')
        self.docker.add('hub', until + 1, 'first line of window two')
        self.archive.run()
        self.clock[0] += 600 * 10**9
        results = self.archive.run()
        names = sorted(self.cloud.objects)
        self.assertEqual(len(names), 2)
        self.assertEqual([doc['line'] for doc in self.documents(names[0])], ['last line of window one'])
        self.assertEqual([doc['line'] for doc in self.documents(names[1])], ['first line of window two'])
        self.assertEqual(results['hub'][0], 'ok')

    def test_a_repeated_window_after_a_crash_keeps_its_object_name(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + 10**9, 'one line')
        self.cloud.deny = True
        results = self.archive.run()
        self.assertEqual(results['hub'][0], 'upload_403')
        state = json.loads((self.state / 'state.json').read_text())
        pending = state['services']['hub']['pending']
        self.assertEqual(state['services']['hub']['cursor'], START)
        self.cloud.deny = False
        self.clock[0] += 300 * 10**9
        results = self.archive.run()
        self.assertEqual(results['hub'][0], 'ok')
        self.assertEqual(results['hub'][1]['object'], self.archive.object_name('hub', pending['until']))
        results = self.archive.run()
        self.assertEqual(results['hub'][1].get('lines'), 0)
        # A name that already exists (an earlier attempt finished the upload) counts as shipped.
        state = json.loads((self.state / 'state.json').read_text())
        state['services']['hub']['pending'] = pending
        state['services']['hub']['cursor'] = START
        (self.state / 'state.json').write_text(json.dumps(state))
        self.assertEqual(self.archive.run()['hub'][1]['status'], 412)

    def test_rotation_gap_and_replaced_container_are_reported(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + 10**9, 'line that rotates away later')
        self.archive.run()
        self.docker.records[self.docker.containers['hub']].clear()
        self.clock[0] += 600 * 10**9
        self.assertTrue(self.archive.run()['hub'][1]['rotation_gap'])
        self.docker.containers['hub'] = 'e' * 64
        self.docker.records['e' * 64] = []
        self.clock[0] += 600 * 10**9
        code, details = self.archive.run()['hub']
        self.assertEqual((code, details.get('replaced')), ('gap', True))

    def test_changed_log_settings_and_missing_containers_fail_closed(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.log_config = {'Type': 'local', 'Config': {}}
        self.assertEqual(self.archive.run()['hub'][0], 'log_config_changed')
        self.docker.log_config = {'Type': 'json-file', 'Config': {'max-size': '10m', 'max-file': '3'}}
        del self.docker.containers['proxy']
        self.assertEqual(self.archive.run()['proxy'][0], 'container_not_single')

    def test_residue_refuses_the_window_and_keeps_the_cursor(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + 10**9, 'anything')
        original = archive.redaction.scrub
        archive.redaction.scrub = lambda line: (line, 0, ['named'])
        try:
            self.assertEqual(self.archive.run()['hub'][0], 'residue_after_wide_mask')
        finally:
            archive.redaction.scrub = original
        self.assertEqual(json.loads((self.state / 'state.json').read_text())['services']['hub']['cursor'], START)
        self.assertEqual(self.cloud.objects, {})

    def test_over_long_messages_are_rejoined_before_masking(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + 10**9, 'a' * 16378 + ' lat=37.123456 ' + 'b' * 20000)
        name = self.archive.run()['hub'][1]['object']
        line = self.documents(name)[0]['line']
        self.assertEqual((len(line), line[16378:16389]), (16378 + len(' lat=*** ') + 20000, ' lat=*** ' + 'bb'))

    def test_state_holds_no_log_text(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + 10**9, 'secret-looking text lat=37.566500')
        self.archive.run()
        for path in self.state.iterdir():
            if path.name != 'identity':
                self.assertNotIn(b'secret-looking', path.read_bytes())


class ConfigTests(unittest.TestCase):
    def test_config_must_be_root_private_and_point_at_log_6m(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(CONFIG))
            path.chmod(0o600)
            # The test user is not root, so the ownership check is the one that fires here.
            with self.assertRaisesRegex(archive.ArchiveError, 'config_not_root_private'):
                archive.load_config(path)

    def test_time_round_trip(self):
        ns = START + 123_456_789
        self.assertEqual(archive.rfc3339_to_ns(archive.ns_to_rfc3339(ns)), ns)
        self.assertEqual(archive.ns_to_docker(ns), f'{ns // 10**9}.123456789')


if __name__ == '__main__':
    unittest.main()
