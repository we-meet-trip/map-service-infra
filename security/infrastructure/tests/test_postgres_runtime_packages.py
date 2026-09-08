"""Exercise the actual Dockerfile tuple parser without APT, Docker or a build."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest


DOCKERFILE = Path(__file__).resolve().parents[1] / 'Dockerfile.postgres-debian'
RUNTIME_FILE = '/usr/share/map-candidate/postgis-build-runtime-packages.tsv'
# Public repository versions, used as representative parser input. This is not
# the unexported package TSV from a remote candidate build.
ROWS = [
    ('libgdal39', 'amd64', '3.13.2+dfsg-1.pgdg12+1', 'installed'),
    ('libgeos-c1t64', 'amd64', '3.14.1-2.pgdg12+1', 'installed'),
    ('libjson-c5', 'amd64', '0.16-2', 'installed'),
    ('libpcre2-8-0', 'amd64', '10.42-1', 'installed'),
    ('libproj25', 'amd64', '9.8.1-1.pgdg12+1', 'installed'),
    ('libprotobuf-c1', 'amd64', '1.4.1-1+b1', 'installed'),
    ('libsfcgal1', 'amd64', '1.4.1-5', 'installed'),
    ('libxml2', 'amd64', '2.9.14+dfsg-1.3~deb12u5', 'installed'),
]


def dockerfile_runs():
    logical = DOCKERFILE.read_text().replace('\\\n', '')
    return [line[4:] for line in logical.splitlines() if line.startswith('RUN ')]


class PostgresRuntimePackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        matches = [run for run in dockerfile_runs() if 'pg_runtime_seen=' in run]
        if len(matches) != 1:
            raise AssertionError('exact actual Dockerfile parser required')
        # Stop before any package manager invocation; use a builtin to inspect
        # the exact argv accumulated by the production shell fragment.
        cls.parser = matches[0].split('    apt-get update;', 1)[0]
        if 'apt-get' in cls.parser or 'rm -rf' in cls.parser:
            raise AssertionError('package manager and removal must not run locally')
        cls.shells = [shell for shell in ('/bin/sh', '/bin/dash') if Path(shell).exists()]

    def run_parser(self, rows, shell='/bin/sh'):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic-runtime.tsv'
            path.write_text(''.join('\t'.join(row) + '\n' for row in rows))
            script = self.parser.replace(RUNTIME_FILE, str(path))
            script += '\n printf "%s\\n" "$@"\n'
            return subprocess.run([shell, '-c', script], capture_output=True, text=True,
                                  timeout=10, env={**os.environ, 'LC_ALL': 'en_US.UTF-8'})

    def assert_rejected(self, rows, reason):
        for shell in self.shells:
            with self.subTest(shell=shell):
                result = self.run_parser(rows, shell)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('postgis_runtime_tuple_invalid:' + reason, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertLess(len(result.stderr.encode()), 8400)

    def test_exact_current_runtime_names_and_versions_become_separate_arguments(self):
        expected = [name + ':' + arch + '=' + version for name, arch, version, _ in ROWS]
        for shell in self.shells:
            with self.subTest(shell=shell):
                result = self.run_parser(ROWS, shell)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)

    def test_obsolete_virtual_package_with_empty_metadata_is_rejected(self):
        rows = list(ROWS)
        rows[1] = ('libgeos-c1v5', '', '', 'not-installed')
        self.assert_rejected(rows, 'package')

    def test_current_concrete_package_without_installed_status_is_rejected(self):
        for status in ('not-installed', 'config-files', 'unpacked', ''):
            rows = list(ROWS)
            rows[1] = (*rows[1][:3], status)
            with self.subTest(status=status):
                self.assert_rejected(rows, 'not_installed')

    def test_empty_or_foreign_architecture_is_rejected(self):
        for arch in ('', 'arm64'):
            rows = list(ROWS)
            rows[1] = (rows[1][0], arch, *rows[1][2:])
            with self.subTest(arch=arch):
                self.assert_rejected(rows, 'architecture')

    def test_architecture_independent_tuple_remains_supported(self):
        rows = list(ROWS)
        rows[0] = (rows[0][0], 'all', *rows[0][2:])
        result = self.run_parser(rows)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], 'libgdal39:all=' + rows[0][2])

    def test_version_shell_injection_is_rejected_without_execution(self):
        for value in ('1;echo injected', '1$(echo injected)', '1`echo injected`', '-1'):
            rows = list(ROWS)
            rows[0] = (*rows[0][:2], value, 'installed')
            with self.subTest(version=value):
                self.assert_rejected(rows, 'version_start' if value == '-1' else 'version_characters')

    def test_missing_extra_and_duplicate_rows_are_rejected(self):
        self.assert_rejected(ROWS[:-1], 'package_count')
        self.assert_rejected([*ROWS[:-1], ROWS[0]], 'duplicate')
        self.assert_rejected([(*ROWS[0], 'extra'), *ROWS[1:]], 'extra_column')

    def test_actual_run_shell_syntax_and_strict_postinstall_guards(self):
        for index, run in enumerate(dockerfile_runs()):
            for shell in self.shells:
                with self.subTest(run=index, shell=shell):
                    result = subprocess.run([shell, '-n', '-c', run], capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
        text = DOCKERFILE.read_text()
        self.assertEqual(text.count('${db:Status-Status}'), 2)
        self.assertIn('if ! cmp -s ', text)
        self.assertIn("! ldd \"$module\" | grep -q 'not found'", text)
        self.assertIn('--no-install-recommends --no-remove "$@"', text)
        self.assertNotIn('--allow-downgrades', text)
        self.assertNotIn('--allow-unauthenticated', text)
        self.assertTrue(text.rstrip().endswith('USER 999:999'))


if __name__ == '__main__':
    unittest.main()
