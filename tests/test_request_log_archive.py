"""Request-log archive: no Docker daemon, network or cloud account.

Docker is a stand-in that keeps timestamped records per container and answers
`docker logs -t --since --until` like the engine (both ends inclusive, over-long messages as
16 KiB fragments under one timestamp, a cut multi-byte character stored as U+FFFD). The metadata
server and Cloud Storage are a stand-in opener with create-only objects (a second create of the
same name is 412). age runs for real when installed.
"""
import base64
import contextlib
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import request_log_archive as archive

AGE = shutil.which('age')
AGE_KEYGEN = shutil.which('age-keygen')
NS = 10**9
START = 1_791_000_000 * NS
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
        self.running = {}
        self.log_config = {'Type': 'json-file', 'Config': {'max-size': '10m', 'max-file': '3'}}
        self.fail = {}

    def add(self, service, ns, text, stream='stdout'):
        self.records[self.containers[service]].append((ns, stream, text.encode()))

    def replace(self, service, cid):
        self.containers[service] = cid
        self.records[cid] = []

    def __call__(self, argv, stdin=None):
        if argv[0] == archive.AGE:
            if AGE:
                done = subprocess.run([AGE] + argv[1:], input=stdin, capture_output=True)
                return done.returncode, done.stdout, done.stderr
            return 0, b'age-encryption.org/v1\n' + stdin, b''
        command = argv[1]
        if command in self.fail:
            raise self.fail[command]
        if command == 'ps':
            assert argv[2:5] == ['-a', '-q', '--no-trunc'] and 'label=com.docker.compose.oneoff=False' in argv
            service = argv[-1].rsplit('=', 1)[1]
            cid = self.containers.get(service)
            return 0, (cid + '\n').encode() if cid else b'', b''
        if command == 'inspect':
            running = 'true' if self.running.get(argv[-1], True) else 'false'
            return 0, (json.dumps(self.log_config) + ' ' + running + '\n').encode(), b''
        if command == 'logs':
            since, until, cid = argv[4], argv[6], argv[7]
            since_ns, until_ns = (int(value.replace('.', '')) for value in (since, until))
            out, err = io.BytesIO(), io.BytesIO()
            for ns, stream, text in self.records.get(cid, []):
                if since_ns <= ns <= until_ns:
                    pieces = [text[i:i + 16384] for i in range(0, len(text), 16384)] or [b'']
                    stored = [piece.decode('utf-8', 'replace').encode() for piece in pieces]
                    (out if stream == 'stdout' else err).write(
                        b''.join(stamp(ns) + b' ' + piece for piece in stored) + b'\n')
            return 0, out.getvalue(), err.getvalue()
        raise AssertionError(argv)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCloud:
    def __init__(self):
        self.objects = {}
        self.deny = False
        self.after_store = None

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
        if self.after_store:
            raise self.after_store
        return FakeResponse(json.dumps({'name': meta['name'], 'md5Hash': meta['md5Hash']}).encode())


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir(mode=0o700)
        self.config = dict(CONFIG)
        self.identity = None
        if AGE and AGE_KEYGEN:
            key = subprocess.run([AGE_KEYGEN], capture_output=True, text=True, check=True).stdout
            self.identity = Path(self.tmp.name) / 'identity'
            self.identity.write_text(key)
            self.config['recipient'] = next(line.split(': ')[1] for line in key.splitlines() if 'public key' in line)
        self.docker, self.cloud = FakeDocker(), FakeCloud()
        self.clock = [START + 600 * NS]
        self.archive = self.make_archive()

    def make_archive(self):
        return archive.Archive(self.config, archive.Host(self.config, self.docker, self.cloud), self.state,
                               now_ns=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def state_file(self):
        return json.loads((self.state / 'state.json').read_text())

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

    def test_arm_bounds_and_the_recorded_containers(self):
        with self.assertRaisesRegex(archive.ArchiveError, 'start_in_future'):
            self.archive.arm(archive.ns_to_rfc3339(self.clock[0] + NS))
        with self.assertRaisesRegex(archive.ArchiveError, 'start_too_old'):
            self.archive.arm(archive.ns_to_rfc3339(self.clock[0] - 6 * 3600 * NS - 1))
        del self.docker.containers['proxy']
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.assertEqual(self.state_file()['services'], {'hub': {'cursor': START, 'container': 'c' * 64},
                                                         'proxy': {'cursor': START}})
        with self.assertRaisesRegex(archive.ArchiveError, 'already_armed'):
            self.archive.arm(archive.ns_to_rfc3339(START))

    def test_nothing_before_the_armed_cursor_is_shipped_and_lines_are_masked(self):
        self.docker.add('hub', START - 5 * NS, 'GET /v1/weather/now?lat=37.566500&lng=126.978000 before arming')
        self.docker.add('hub', START + NS, f'GET /v1/weather/now?loc={ENVELOPE} HTTP/1.1" 200')
        self.docker.add('hub', START + 2 * NS, 'HTTP Request: GET http://osrm-foot:5000/route/v1/foot/127.0276,37.4979;126.978,37.5665', 'stderr')
        self.docker.add('proxy', START + 3 * NS, 'request: "GET /api/v1/bikes?latitude=37.49795&longitude=127.02761 HTTP/1.1"')
        self.archive.arm(archive.ns_to_rfc3339(START))
        results = self.archive.run()
        self.assertEqual({name: code for name, (code, _) in results.items()}, {'hub': 'ok', 'proxy': 'ok'})
        hub = results['hub'][1]
        self.assertEqual((hub['lines'], hub['masked'], hub['withheld'], hub['status']), (2, 2, 0, 200))
        documents = self.documents(hub['object'])
        self.assertEqual([doc['stream'] for doc in documents], ['stdout', 'stderr'])
        text = json.dumps(documents)
        for value in ('37.5665', '126.978', '127.0276', '37.4979', 'AbCdEfGhIjKlMnOp', 'before arming'):
            self.assertNotIn(value, text)
        self.assertRegex(hub['object'], r'^log-6m/requests/\d{4}/\d{2}/\d{2}/hub/\d{6}\.\d{9}Z\.ndjson\.gz\.age$')
        self.assertEqual(self.cloud.objects[hub['object']]['customTime'], archive.ns_to_rfc3339(self.clock[0]))

    def test_windows_join_without_overlap_or_gap(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        until = self.clock[0] - 10 * NS
        self.docker.add('hub', until, 'last line of window one')
        self.docker.add('hub', until + 1, 'first line of window two')
        self.archive.run()
        self.clock[0] += 600 * NS
        results = self.archive.run()
        names = sorted(self.cloud.objects)
        self.assertEqual(len(names), 2)
        self.assertEqual([doc['line'] for doc in self.documents(names[0])], ['last line of window one'])
        self.assertEqual([doc['line'] for doc in self.documents(names[1])], ['first line of window two'])
        self.assertEqual(results['hub'][0], 'ok')

    def test_a_repeated_window_after_a_failure_keeps_its_object_name(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + NS, 'one line')
        self.cloud.deny = True
        results = self.archive.run()
        self.assertEqual(results['hub'][0], 'upload_403')
        pending = self.state_file()['services']['hub']['pending']
        self.assertEqual(self.state_file()['services']['hub']['cursor'], START)
        self.cloud.deny = False
        self.clock[0] += 300 * NS
        results = self.archive.run()
        self.assertEqual(results['hub'][0], 'ok')
        self.assertEqual(results['hub'][1]['object'], self.archive.object_name('hub', pending['until']))
        self.assertEqual(self.archive.run()['hub'][1].get('lines'), 0)

    def test_a_response_lost_after_the_object_was_stored_ships_it_once(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + NS, 'one line')
        self.cloud.after_store = urllib.error.URLError(ConnectionResetError('reset after the object was stored'))
        self.assertEqual(self.archive.run()['hub'][0], 'URLError')
        self.assertIn('pending', self.state_file()['services']['hub'])
        self.cloud.after_store = None
        self.clock[0] += 300 * NS
        code, details = self.archive.run()['hub']
        self.assertEqual((code, details['status']), ('ok', 412))
        self.assertEqual(len([name for name in self.cloud.objects if '/hub/' in name]), 1)
        self.assertNotIn('pending', self.state_file()['services']['hub'])

    def test_a_run_killed_during_the_upload_repeats_the_same_name(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + NS, 'one line')
        # A kill skips every later state write, so only the write made before the upload counts.
        self.cloud.after_store = SystemExit('killed by the unit timeout')
        with self.assertRaises(SystemExit):
            self.archive.run()
        self.cloud.after_store = None
        self.clock[0] += 300 * NS
        code, details = self.make_archive().run()['hub']
        self.assertEqual((code, details['status']), ('ok', 412))
        self.assertEqual(len([name for name in self.cloud.objects if '/hub/' in name]), 1)

    def test_a_clock_step_back_is_reported_and_the_cursor_never_moves_back(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.archive.run()
        cursor = self.state_file()['services']['hub']['cursor']
        self.clock[0] -= 3600 * NS
        self.assertEqual(self.archive.run()['hub'],
                         ('gap', {'clock_behind': True, 'lines': 0, 'masked': 0, 'withheld': 0}))
        self.assertEqual(self.state_file()['services']['hub']['cursor'], cursor)
        self.clock[0] += 7200 * NS
        self.assertEqual(self.archive.run()['hub'][0], 'ok')

    def test_rotation_gap_and_a_replaced_container_are_reported(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + NS, 'line that rotates away later')
        self.archive.run()
        self.docker.records[self.docker.containers['hub']].clear()
        self.clock[0] += 600 * NS
        code, details = self.archive.run()['hub']
        self.assertEqual((code, details.get('rotation_gap')), ('gap', True))
        self.docker.replace('hub', 'e' * 64)
        self.clock[0] += 600 * NS
        code, details = self.archive.run()['hub']
        self.assertEqual((code, details.get('replaced'), details.get('lost_on_replace')), ('gap', True, True))
        self.assertEqual(self.archive.run()['hub'][0], 'ok')

    def test_a_stopped_container_is_read_to_its_end_so_its_replacement_loses_nothing(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.archive.run()
        self.clock[0] += 60 * NS
        self.docker.add('hub', self.clock[0] - NS, 'last words inside the settle time')
        self.docker.running['c' * 64] = False
        results = self.archive.run()
        self.assertEqual(results['hub'][1]['lines'], 1)
        self.assertEqual(self.state_file()['services']['hub']['drained'], 'c' * 64)
        self.docker.replace('hub', 'e' * 64)
        self.clock[0] += 60 * NS
        code, details = self.archive.run()['hub']
        self.assertEqual((code, details.get('replaced'), details.get('lost_on_replace')), ('ok', True, None))
        self.assertNotIn('drained', self.state_file()['services']['hub'])

    def test_a_window_left_from_a_failed_attempt_does_not_count_as_read_to_the_end(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + NS, 'one line')
        self.cloud.deny = True
        self.archive.run()
        self.cloud.deny = False
        self.docker.running['c' * 64] = False
        self.archive.run()
        self.assertNotIn('drained', self.state_file()['services']['hub'])
        self.archive.run()
        self.assertEqual(self.state_file()['services']['hub']['drained'], 'c' * 64)

    def test_changed_log_settings_and_missing_containers_fail_closed(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.log_config = {'Type': 'local', 'Config': {}}
        self.assertEqual(self.archive.run()['hub'][0], 'log_config_changed')
        self.docker.log_config = {'Type': 'json-file', 'Config': {'max-size': '10m', 'max-file': '3', 'mode': 'non-blocking'}}
        self.assertEqual(self.archive.run()['hub'][0], 'log_config_changed')
        self.docker.log_config = {'Type': 'json-file', 'Config': {'max-size': '10m', 'max-file': '3'}}
        del self.docker.containers['proxy']
        self.assertEqual(self.archive.run()['proxy'][0], 'container_not_single')

    def test_one_failing_service_leaves_the_others_running(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('proxy', START + NS, 'proxy line')
        original = self.docker.__call__

        def broken(argv, stdin=None):
            if argv[1:2] == ['logs'] and argv[-1] == 'c' * 64:
                raise TypeError('unexpected')
            return original(argv, stdin)

        self.archive.host.command = broken
        results = self.archive.run()
        self.assertEqual({name: code for name, (code, _) in results.items()}, {'hub': 'TypeError', 'proxy': 'ok'})
        self.assertEqual(self.state_file()['last_run']['codes'], {'hub': 'TypeError', 'proxy': 'ok'})

    def test_a_withheld_line_is_counted_shipped_and_the_cursor_moves(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + NS, 'anything')
        original = archive.redaction.scrub
        archive.redaction.scrub = lambda line: (archive.redaction.WITHHELD, 0, True)
        try:
            code, details = self.archive.run()['hub']
        finally:
            archive.redaction.scrub = original
        self.assertEqual((code, details['withheld'], details['lines']), ('withheld', 1, 1))
        self.assertEqual([doc['line'] for doc in self.documents(details['object'])], [archive.redaction.WITHHELD])
        self.assertEqual(self.state_file()['services']['hub']['cursor'], self.clock[0] - 10 * NS)

    def test_over_long_messages_are_rejoined_before_masking(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        # A 3-byte character cut after its first byte at the first edge (stored as U+FFFD, which
        # shifts the later edges) and a coordinate across the second edge.
        head = 'a' * 16383 + '좌'
        text = head + 'b' * (32768 - len(head.encode()) - 6) + 'lat=37.123456' + 'c' * 100
        self.docker.add('hub', START + NS, text)
        name = self.archive.run()['hub'][1]['object']
        line = self.documents(name)[0]['line']
        self.assertEqual(line, 'a' * 16383 + '�' * 3 + 'b' * (32768 - len(head.encode()) - 6) + 'lat=***' + 'c' * 100)

    def test_state_holds_no_log_text(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        self.docker.add('hub', START + NS, 'secret-looking text lat=37.566500')
        self.archive.run()
        for path in self.state.iterdir():
            self.assertNotIn(b'secret-looking', path.read_bytes())

    def test_a_second_run_waits_for_the_first(self):
        self.archive.arm(archive.ns_to_rfc3339(START))
        holder = os.open(self.state / 'lock', os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        done = []
        worker = threading.Thread(target=lambda: done.append(self.make_archive().run()))
        worker.start()
        worker.join(0.3)
        self.assertEqual(done, [])
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
        worker.join(10)
        self.assertEqual(done[0]['hub'][0], 'ok')


class CommandTests(unittest.TestCase):
    """main() as the timer runs it, with the stand-ins behind Host."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config_path, self.state = root / 'config.json', root / 'state'
        self.config_path.write_text(json.dumps(CONFIG))
        self.config_path.chmod(0o600)
        self.state.mkdir(mode=0o700)
        self.docker, self.cloud = FakeDocker(), FakeCloud()
        original = archive.Host
        archive.Host = lambda config: original(config, self.docker, self.cloud)
        self.addCleanup(setattr, archive, 'Host', original)

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = archive.main(['--config', str(self.config_path), '--state-dir', str(self.state), *args])
        return code, out.getvalue().splitlines()

    def test_success_prints_codes_and_the_backup_marker_only_when_every_service_shipped(self):
        start = archive.ns_to_rfc3339(archive.time.time_ns() - 60 * NS)
        self.assertEqual(self.main('arm', '--start', start)[0], 0)
        code, lines = self.main('run')
        self.assertEqual(code, 0)
        self.assertEqual(lines[-2:], ['MAP_REQLOG_RESULT=OK', 'MAP_BACKUP_RESULT=COMPLETE kind=reqlog'])
        del self.docker.containers['proxy']
        code, lines = self.main('run')
        self.assertEqual(code, 1)
        self.assertIn('MAP_REQLOG_RESULT=FAILED services=proxy', lines)
        self.assertFalse(any('MAP_BACKUP_RESULT' in line for line in lines))

    def test_status_shows_whether_a_stopped_container_was_read_to_its_end(self):
        start = archive.ns_to_rfc3339(archive.time.time_ns() - 60 * NS)
        self.main('arm', '--start', start)
        self.docker.running['c' * 64] = False
        self.main('run')
        code, lines = self.main('status')
        self.assertEqual(code, 0)
        self.assertRegex(lines[0], r'^MAP_REQLOG_STATUS service=hub cursor=\S+ pending=False drained=True$')
        self.assertRegex(lines[1], r'^MAP_REQLOG_STATUS service=proxy cursor=\S+ pending=False drained=False$')
        self.assertTrue(lines[2].startswith('MAP_REQLOG_LAST {'))

    def test_errors_print_a_code_and_never_a_message(self):
        self.config_path.chmod(0o644)
        self.assertEqual(self.main('status'), (1, ['MAP_REQLOG_RESULT=FAILED code=config_not_private']))
        self.config_path.chmod(0o600)
        self.state.chmod(0o755)
        self.assertEqual(self.main('status'), (1, ['MAP_REQLOG_RESULT=FAILED code=state_dir_not_private']))
        self.state.chmod(0o700)
        self.config_path.unlink()
        self.assertEqual(self.main('status'), (1, ['MAP_REQLOG_RESULT=FAILED code=FileNotFoundError']))


class ConfigTests(unittest.TestCase):
    def load(self, config, mode=0o600):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(config))
            path.chmod(mode)
            return archive.load_config(path)

    def test_a_private_config_pointing_at_log_6m_loads(self):
        self.assertEqual(self.load(CONFIG), CONFIG)

    def test_every_field_is_checked(self):
        cases = {'config_not_private': (CONFIG, 0o640),
                 'config_keys': (dict(CONFIG, extra=1), 0o600),
                 'config_prefix': (dict(CONFIG, prefix='log-1y/requests/'), 0o600),
                 'config_recipient': (dict(CONFIG, recipient='age1short'), 0o600),
                 'config_services': (dict(CONFIG, services=['hub', 'hub']), 0o600),
                 'config_settle': (dict(CONFIG, settle_seconds=1), 0o600),
                 'config_log': (dict(CONFIG, log_config={'type': 'json-file', 'max-size': '10m'}), 0o600)}
        for code, (config, mode) in cases.items():
            with self.subTest(code=code), self.assertRaisesRegex(archive.ArchiveError, code):
                self.load(config, mode)

    def test_time_round_trip(self):
        ns = START + 123_456_789
        self.assertEqual(archive.rfc3339_to_ns(archive.ns_to_rfc3339(ns)), ns)
        self.assertEqual(archive.ns_to_docker(ns), f'{ns // NS}.123456789')


class UnitTests(unittest.TestCase):
    def test_the_unit_runs_this_collector_on_its_state_directory(self):
        unit = (ROOT / 'deploy/gcp/map-prod-reqlog-backup.service').read_text().splitlines()
        values = dict(line.split('=', 1) for line in unit if '=' in line and not line.startswith('#'))
        self.assertTrue(values['ExecStart'].endswith(' /opt/map-reqlog/scripts/request_log_archive.py run'))
        self.assertEqual(values['WorkingDirectory'], '/opt/map-reqlog')
        self.assertEqual(Path('/var/lib') / values['StateDirectory'], archive.STATE_DIR)
        self.assertEqual(values['RequiresMountsFor'], '/srv/map-prod')
        self.assertNotIn('ConditionPathExists', values)


if __name__ == '__main__':
    unittest.main()
