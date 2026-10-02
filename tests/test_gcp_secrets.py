"""gcp_secrets.py against a fake gcloud on PATH that keeps its state in a temporary JSON file."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gcp_secrets', ROOT / 'scripts/gcp_secrets.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
PROJECT = 'mapservice-test'

# Records every argv and the three gcloud settings, then answers like gcloud:
# NOT_FOUND text for a missing secret and url-safe base64 without padding.
FAKE_GCLOUD = r'''#!/usr/bin/env python3
import base64, json, os, sys
path = os.environ['FAKE_GCLOUD_STATE']
with open(path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
state['calls'].append({'argv': args, 'prompts': os.environ.get('CLOUDSDK_CORE_DISABLE_PROMPTS'),
                       'log_http': os.environ.get('CLOUDSDK_CORE_LOG_HTTP'),
                       'file_logging': os.environ.get('CLOUDSDK_CORE_DISABLE_FILE_LOGGING'),
                       'binary': sys.argv[0]})


def finish(code=0, out='', err=''):
    with open(path, 'w') as handle:
        json.dump(state, handle)
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)


def flag(name):
    for index, arg in enumerate(args):
        if arg == name:
            return args[index + 1]
        if arg.startswith(name + '='):
            return arg[len(name) + 1:]
    return None


command = tuple(args[:3] if args[1] == 'versions' else args[:2])
name = flag('--secret') or args[len(command)]
resource = 'projects/%s/secrets/%s' % (flag('--project'), name)
secret = state['secrets'].get(name)
if name in state.get('denied', []):
    finish(1, err='ERROR: (gcloud.fake) PERMISSION_DENIED: Permission denied on [%s].\n' % resource)
if command == ('secrets', 'create'):
    if secret is not None or flag('--replication-policy') != 'user-managed':
        finish(1, err='ERROR: (gcloud.fake) ALREADY_EXISTS or bad policy\n')
    replicas = [{'location': location} for location in flag('--locations').split(',')]
    state['secrets'][name] = {'replication': {'userManaged': {'replicas': replicas}}, 'versions': []}
    finish(err='Created secret [%s].\n' % name)
if secret is None:
    finish(1, err='ERROR: (gcloud.fake) NOT_FOUND: Secret [%s] not found.\n' % resource)
if command == ('secrets', 'describe'):
    finish(out=json.dumps({'name': resource, 'replication': secret['replication']}))
if command == ('secrets', 'versions', 'list'):
    count = len(secret['versions'])
    finish(out=json.dumps([{'name': '%s/versions/%d' % (resource, count)}] if count else []))
if command == ('secrets', 'versions', 'add'):
    if flag('--data-file') != '-':
        finish(1, err='ERROR: (gcloud.fake) INVALID_ARGUMENT: stdin only\n')
    secret['versions'].append(base64.b64encode(sys.stdin.buffer.read()).decode())
    finish(out='%s/versions/%d\n' % (resource, len(secret['versions'])))
if command == ('secrets', 'versions', 'access') and secret['versions']:
    data = base64.b64decode(secret['versions'][-1])
    finish(out=base64.urlsafe_b64encode(data).decode().rstrip('=') + '\n')
finish(1, err='ERROR: (gcloud.fake) NOT_FOUND: unsupported or empty\n')
'''


class Terminal(io.TextIOWrapper):
    def isatty(self):
        return True


class GcpSecretsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='map-gcp-secrets-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'gcloud').write_text(FAKE_GCLOUD)
        (bin_dir / 'gcloud').chmod(0o755)
        self.state_file = self.root / 'state.json'
        self.save({'secrets': {}, 'calls': []})
        # The system path is pointed away so a real /usr/bin/gcloud is never used here.
        for patcher in (patch.object(tool, 'GCLOUD', str(self.root / 'no-system-gcloud')),
                        patch.dict(os.environ, {'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', ''),
                                                'FAKE_GCLOUD_STATE': str(self.state_file)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, state):
        self.state_file.write_text(json.dumps(state))

    def state(self):
        return json.loads(self.state_file.read_text())

    def seed(self, name, *values, replication=None, location='us-central1'):
        state = self.state()
        state['secrets'][name] = {
            'replication': replication or {'userManaged': {'replicas': [{'location': location}]}},
            'versions': [base64.b64encode(value).decode() for value in values]}
        self.save(state)

    def stored(self, name):
        return [base64.b64decode(value) for value in self.state()['secrets'][name]['versions']]

    def calls(self, *prefix):
        return [call['argv'] for call in self.state()['calls'] if tuple(call['argv'][:len(prefix)]) == prefix]

    def run_tool(self, *argv, stdin=b'', stream=io.TextIOWrapper):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'stdin', stream(io.BytesIO(stdin))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def assert_never_exposed(self, value, *texts):
        text = value.decode()
        for argv in self.calls():
            self.assertNotIn(text, ' '.join(argv))
        for shown in texts:
            self.assertNotIn(text, shown)

    def test_ensure_creates_once_with_one_user_managed_location(self):
        args = ('ensure', '--project', PROJECT, '--location', 'us-central1', 'test-a', 'test-b')
        code, out, err = self.run_tool(*args)
        self.assertEqual((code, out, err), (0, 'created test-a\ncreated test-b\n', ''))
        self.assertEqual(self.calls('secrets', 'create')[0], ['secrets', 'create', 'test-a', '--project', PROJECT,
                                                              '--replication-policy=user-managed',
                                                              '--locations=us-central1'])
        for name in ('test-a', 'test-b'):
            self.assertEqual(self.state()['secrets'][name]['replication'],
                             {'userManaged': {'replicas': [{'location': 'us-central1'}]}})
        # A second run only verifies.
        self.assertEqual(self.run_tool(*args)[:2], (0, 'ok test-a\nok test-b\n'))
        self.assertEqual(len(self.calls('secrets', 'create')), 2)
        # gcloud copies what it prints into its log files unless file logging is off.
        for call in self.state()['calls']:
            self.assertEqual((call['prompts'], call['log_http'], call['file_logging']), ('1', 'false', '1'))

    def test_ensure_rejects_any_other_replication_without_creating(self):
        self.seed('test-elsewhere', location='asia-northeast3')
        self.seed('test-automatic', replication={'automatic': {}})
        self.seed('test-two', replication={'userManaged': {'replicas': [{'location': 'us-central1'},
                                                                         {'location': 'us-east1'}]}})
        for name in ('test-elsewhere', 'test-automatic', 'test-two'):
            code, out, err = self.run_tool('ensure', '--project', PROJECT, '--location', 'us-central1', name)
            self.assertEqual((code, out), (1, ''))
            self.assertIn(name + ': replication is not user-managed in exactly us-central1', err)
        self.assertEqual(self.calls('secrets', 'create'), [])

    def test_check_only_verifies_without_creating(self):
        self.seed('prod-ready', location='asia-northeast3')
        args = ('ensure', '--project', 'mapcenter-b59ca', '--location', 'asia-northeast3', '--check-only')
        code, out, err = self.run_tool(*args, 'prod-ready', 'prod-missing')
        self.assertEqual((code, out, err), (1, 'ok prod-ready\n', 'prod-missing: missing\n'))
        self.assertEqual(self.run_tool(*args, 'prod-ready')[0], 0)
        self.assertEqual(self.calls('secrets', 'create'), [])
        self.assertNotIn('prod-missing', self.state()['secrets'])

    def test_a_failure_other_than_missing_stops_before_creating(self):
        state = self.state()
        state['denied'] = ['test-denied']
        self.save(state)
        code, out, err = self.run_tool('ensure', '--project', PROJECT, '--location', 'us-central1', 'test-denied')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('PERMISSION_DENIED', err)
        self.assertEqual(self.calls('secrets', 'create'), [])

    def test_generate_refuses_an_existing_version_unless_rotating(self):
        self.seed('test-token')
        self.assertEqual(self.run_tool('generate', '--project', PROJECT, 'test-token'), (0, '1\n', ''))
        [first] = self.stored('test-token')
        self.assertEqual(len(first), 43)
        self.assertRegex(first.decode(), r'^[A-Za-z0-9_-]+$')
        code, out, err = self.run_tool('generate', '--project', PROJECT, 'test-token')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('--rotate', err)
        self.assertEqual(len(self.calls('secrets', 'versions', 'add')), 1)
        code, out, err = self.run_tool('generate', '--project', PROJECT, 'test-token', '--rotate')
        self.assertEqual((code, out, err), (0, '2\n', ''))
        second = self.stored('test-token')[1]
        self.assertNotEqual(first, second)
        self.assert_never_exposed(first, out, err)
        self.assert_never_exposed(second, out, err)
        with self.assertRaises(SystemExit):
            self.run_tool('generate', '--project', PROJECT, 'test-token', '--bytes', '8')

    def test_put_reads_one_value_from_stdin_and_never_from_argv(self):
        self.seed('test-places-key')
        value = b'copied-from-the-old-test-vm'
        code, out, err = self.run_tool('put', '--project', PROJECT, 'test-places-key', stdin=value + b'\n')
        self.assertEqual((code, out, err), (0, '1\n', ''))
        self.assertEqual(self.stored('test-places-key'), [value])
        self.assert_never_exposed(value, out, err)
        for bad in (b'', b'\n', b'two\nlines', b'one\n\n', b'carriage\r'):
            with self.subTest(bad=bad):
                code, out, err = self.run_tool('put', '--project', PROJECT, 'test-places-key', stdin=bad)
                self.assertEqual((code, out), (1, ''))
        code, _, err = self.run_tool('put', '--project', PROJECT, 'test-places-key', stdin=value, stream=Terminal)
        self.assertEqual(code, 1)
        self.assertIn('pipe', err)
        self.assertEqual(len(self.calls('secrets', 'versions', 'add')), 1)

    def test_materialize_replaces_the_file_atomically_with_mode_0600(self):
        password, token = b'Abc.def_~+/=:@-09', b'token-without-padding'
        dsn = b'postgresql://exporter:p%2Bw@postgres:5432/map_test?sslmode=disable&connect_timeout=5'
        self.seed('test-db-password', password)
        self.seed('test-token', b'older-version', token)
        self.seed('test-exporter-dsn', dsn)
        output = self.root / 'env' / 'runtime.env'
        output.parent.mkdir()
        output.write_text('STALE=1\n')
        output.chmod(0o644)
        inode = output.stat().st_ino
        with patch.object(tool.os, 'replace', wraps=os.replace) as replace:
            code, out, err = self.run_tool('materialize', '--project', PROJECT, '--output', str(output),
                                           'POSTGRES_PASSWORD=test-db-password', 'INTERNAL_SERVICE_TOKEN=test-token',
                                           'POSTGRES_EXPORTER_DSN=test-exporter-dsn')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(output.read_bytes(), b'POSTGRES_PASSWORD=' + password + b'\nINTERNAL_SERVICE_TOKEN=' + token
                         + b'\nPOSTGRES_EXPORTER_DSN=' + dsn + b'\n')
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        # A rename installs a new inode; rewriting in place would keep the old one.
        self.assertNotEqual(output.stat().st_ino, inode)
        # The temporary file sits next to the target so the rename never crosses filesystems.
        [(source, target), _] = replace.call_args
        self.assertEqual((Path(source).parent, Path(target)), (output.parent, output))
        self.assertEqual([path.name for path in output.parent.iterdir()], ['runtime.env'])
        self.assert_never_exposed(password, out, err)
        self.assert_never_exposed(token, out, err)
        self.assert_never_exposed(dsn, out, err)

    def test_materialize_refuses_unsafe_values_and_keeps_the_old_file(self):
        self.seed('test-good', b'fine')
        output = self.root / 'runtime.env'
        output.write_text('OLD=1\n')
        for bad in (b'has space', b'quote"d', b'dollar$sign', b'semi;colon', b'hash#tag', b'back\\slash',
                    b'\xff\xfe', b''):
            with self.subTest(bad=bad):
                self.seed('test-bad', bad)
                code, out, err = self.run_tool('materialize', '--project', PROJECT, '--output', str(output),
                                               'GOOD=test-good', 'BAD_KEY=test-bad')
                self.assertEqual((code, out), (1, ''))
                self.assertTrue(err.startswith('gcp_secrets: BAD_KEY: '), err)
                if bad:
                    self.assertNotIn(bad.decode(errors='replace'), err)
                self.assertEqual(output.read_text(), 'OLD=1\n')
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['bin', 'runtime.env', 'state.json'])
        code, out, err = self.run_tool('materialize', '--project', PROJECT, '--output', str(output),
                                       'GOOD=test-good', 'ABSENT_KEY=test-absent')
        self.assertEqual((code, out, err), (1, '', 'gcp_secrets: ABSENT_KEY: test-absent: '
                                                    'gcloud secrets versions access failed: NOT_FOUND\n'))
        self.assertEqual(output.read_text(), 'OLD=1\n')
        for argv in (['lower=test-good'], ['GOOD=bad/name'], ['GOOD=test-good', 'GOOD=test-good'], ['GOOD']):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.run_tool('materialize', '--project', PROJECT, '--output', str(output), *argv)

    def test_payload_decoding_accepts_unpadded_url_safe_base64(self):
        for text in (b'-_8', b'-_8=', b'+/8=', b'-_8\n'):
            self.assertEqual(tool.decode_payload(text, 'KEY'), b'\xfb\xff')
        self.assertEqual(tool.decode_payload(b'', 'KEY'), b'')
        with self.assertRaises(tool.SecretError):
            tool.decode_payload(b'not*base64', 'KEY')

    def test_the_system_gcloud_is_used_before_one_on_path(self):
        system = self.root / 'system-gcloud'
        system.write_text(FAKE_GCLOUD)
        system.chmod(0o755)
        with patch.object(tool, 'GCLOUD', str(system)):
            self.assertEqual(self.run_tool('ensure', '--project', PROJECT, '--location', 'us-central1', 'test-a')[0], 0)
        self.assertEqual({call['binary'] for call in self.state()['calls']}, {str(system)})

    def test_names_and_projects_are_checked_before_gcloud_runs(self):
        for argv in (('ensure', '--project', 'Bad_Project', '--location', 'us-central1', 'test-a'),
                     ('ensure', '--project', PROJECT, '--location', 'us central1', 'test-a'),
                     ('generate', '--project', PROJECT, 'bad/name'),
                     ('put', '--project', PROJECT, 'x' * 256)):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.run_tool(*argv)
        self.assertEqual(self.state()['calls'], [])


if __name__ == '__main__':
    unittest.main()
