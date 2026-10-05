"""Secret values stay off command lines: every local account can read /proc/<pid>/cmdline."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
# An unquoted shell expansion splits a value at spaces, so argv is searched for MARK.
MARK = 'argv-sentinel'
SECRET = MARK + ' "q\' \\ #$x`y`'
# Stand-in for redis-cli, redis-server, curl and sleep: records argv, the client auth
# variable and stdin, answers like the real program, and sleep ends the update loop.
STUB = ('#!/usr/bin/env python3\nimport json, os, signal, sys\n'
        'name = os.path.basename(sys.argv[0])\n'
        'record = {"argv": sys.argv[1:], "auth": os.environ.get("REDISCLI_AUTH"), "stdin": sys.stdin.read()}\n'
        'with open(os.environ["CALL_LOG"], "a") as log: log.write(json.dumps([name, record]) + "\\n")\n'
        'print("PONG" if name == "redis-cli" else "OK", flush=True)\n'
        'if name == "sleep": os.kill(os.getppid(), signal.SIGTERM)\n')


class ShellSourceTests(unittest.TestCase):
    def test_startup_passes_the_console_password_by_name(self):
        text = (ROOT / 'scripts/cloud-up.sh').read_text()
        self.assertIsNone(re.search(r'(?:-e|--env)[ =]+[A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|KEY|DSN)[A-Z0-9_]*=', text))
        self.assertRegex(text, r'MAP_ADMIN_PASSWORD="\$\(grep [^\n]*"\$ENV_FILE"[^\n]*\)" \\\n'
                               r' +dc exec -T -e MAP_ADMIN_PASSWORD postgres ')

    def test_admin_role_script_reads_the_password_inside_psql(self):
        text = (ROOT / 'db/init/10-admin.sh').read_text()
        self.assertNotRegex(text, r'-v +\w*pw\w*=')
        self.assertIn("<<'EOSQL'\n\\getenv mapadminpw MAP_ADMIN_PASSWORD\n", text)


class RolloverArgvTests(unittest.TestCase):
    def test_temporary_container_gets_values_through_the_client_environment(self):
        spec = importlib.util.spec_from_file_location('service_rollover', ROOT / 'scripts/service-rollover.py')
        roll = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(roll)
        entry = {'environment': {'USER_DATABASE_PASSWORD': SECRET, 'LOG_LEVEL': None}}
        args = roll.create_args('map-test-user-rollover', 'map-test', 'user', 'image', entry, ['net'])
        self.assertFalse(any(MARK in arg for arg in args))
        self.assertIn('USER_DATABASE_PASSWORD', args)
        self.assertEqual(roll.create_env(entry)['USER_DATABASE_PASSWORD'], SECRET)
        with self.assertRaisesRegex(roll.RolloverError, 'collides_with_docker_client'):
            roll.create_env({'environment': {'DOCKER_HOST': 'tcp://elsewhere:2375'}})


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose is required')
class ComposeShellTests(unittest.TestCase):
    def render(self, *files):
        result = subprocess.run(['docker', 'compose', '--env-file', '.env.example',
                                 *(arg for name in files for arg in ('-f', name)),
                                 '--profile', 'full', '--profile', 'dns', 'config', '--format', 'json'],
                                cwd=ROOT, capture_output=True, text=True,
                                env={**os.environ, 'MAP_STACK_ENV': 'test', 'DUCKDNS_SUBDOMAIN': 'name',
                                     'DUCKDNS_TOKEN': 'render-only'})
        self.assertEqual(result.returncode, 0, 'Compose must render')
        return json.loads(result.stdout)['services']

    def run_shell(self, script, **env):
        # The rendered output keeps Compose's $$ escape; the container shell sees $.
        with tempfile.TemporaryDirectory() as directory:
            for name in ('redis-cli', 'redis-server', 'curl', 'sleep'):
                (Path(directory) / name).write_text(STUB)
                (Path(directory) / name).chmod(0o700)
            log = Path(directory) / 'calls.jsonl'
            result = subprocess.run(['sh', '-c', script.replace('$$', '$')], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=60,
                                    env={'PATH': directory + os.pathsep + os.environ['PATH'],
                                         'CALL_LOG': str(log), **env})
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        self.assertTrue(calls, result.stderr)
        self.assertFalse(any(MARK in arg for _, call in calls for arg in call['argv']))
        return result, dict(calls)

    def test_redis_password_reaches_the_server_on_stdin_and_the_client_by_environment(self):
        base = self.render('docker-compose.yml')['redis']
        micro = self.render('docker-compose.yml', 'docker-compose.test.yml', 'docker-compose.micro.yml')['redis']
        quoted = 'requirepass "%s"\n' % SECRET.replace('\\', '\\\\').replace('"', '\\"')
        for label, command in (('base', base['command']), ('micro', micro['command'])):
            for password, stdin in (('', ''), (SECRET, quoted)):
                with self.subTest(command=label, password=bool(password)):
                    result, calls = self.run_shell(command[2], REDIS_PASSWORD=password)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(calls['redis-server']['stdin'], stdin)
                    self.assertEqual('-' in calls['redis-server']['argv'], bool(password))
        for password, auth in (('', None), (SECRET, SECRET)):
            with self.subTest(healthcheck=True, password=bool(password)):
                result, calls = self.run_shell(base['healthcheck']['test'][1], REDIS_PASSWORD=password)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls['redis-cli'], {'argv': ['ping'], 'auth': auth, 'stdin': ''})

    def test_dynamic_dns_token_is_imported_by_curl_itself(self):
        loop = self.render('docker-compose.yml', 'docker-compose.dns.yml')['dns']['command'][2]
        _, calls = self.run_shell(loop, DUCKDNS_TOKEN=SECRET, DUCKDNS_SUBDOMAIN='name')
        self.assertIn('%DUCKDNS_TOKEN', calls['curl']['argv'])
        self.assertTrue(any('{{DUCKDNS_TOKEN}}' in arg for arg in calls['curl']['argv']))


if __name__ == '__main__':
    unittest.main()
