import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / 'security/infrastructure/gosu-security/build.py'
SPEC = importlib.util.spec_from_file_location('gosu_security_build', PATH)
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class GosuRecipeTests(unittest.TestCase):
    def test_plan_requires_exact_source_sha(self):
        for bad in ['latest', '6456aaa', 'a' * 39, 'g' * 40]:
            with self.subTest(value=bad), self.assertRaises(ValueError):
                BUILD.plan(bad)
        self.assertFalse(BUILD.plan('a' * 40)['candidate_security_approved'])

    def test_local_execution_rejected_before_writes(self):
        with mock.patch.dict(BUILD.os.environ, {}, clear=True), \
                mock.patch.object(BUILD.Path, 'mkdir') as mkdir, \
                self.assertRaisesRegex(ValueError, 'remote_linux_root_builder'):
            BUILD.execute(BUILD.plan('a' * 40))
        mkdir.assert_not_called()

    def test_module_checksum_and_replacement_rejected(self):
        pin = {'version': 'v0.1.0', 'sum': 'h1:expected', 'go_mod_sum': 'h1:mod'}
        good = {'Version': 'v0.1.0', 'Sum': 'h1:expected', 'GoModSum': 'h1:mod'}
        BUILD.verify_module(pin, good)
        for field, value in [('Sum', 'h1:wrong'), ('GoModSum', 'h1:wrong'),
                             ('Version', 'v0.2.0'), ('Replace', {'Path': '../local'})]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                BUILD.verify_module(pin, {**good, field: value})

    def test_json_stream_keeps_each_download_receipt(self):
        self.assertEqual(BUILD.json_stream('{"Path":"one"}\n {"Path":"two"}'),
                         [{'Path': 'one'}, {'Path': 'two'}])

    def test_key_armor_cannot_be_private_or_invalid(self):
        armored = b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nYWJj\n=crc0\n-----END PGP PUBLIC KEY BLOCK-----\n'
        self.assertEqual(BUILD.dearmor_key(armored), b'abc')
        with self.assertRaises(ValueError):
            BUILD.dearmor_key(armored.replace(b'PUBLIC', b'PRIVATE'))

    def test_elf_rejects_dynamic_loader_and_other_architecture(self):
        data = bytearray(120)
        data[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', data, 18, 62)
        struct.pack_into('<Q', data, 32, 64)
        struct.pack_into('<HH', data, 54, 56, 1)
        struct.pack_into('<I', data, 64, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'public-synthetic-elf'
            path.write_bytes(data)
            self.assertFalse(BUILD.elf_identity(path)['PT_INTERP'])
            struct.pack_into('<I', data, 64, 3)
            path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'static_elf'):
                BUILD.elf_identity(path)
            struct.pack_into('<H', data, 18, 183)
            path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'amd64'):
                BUILD.elf_identity(path)

    def test_timeout_preserves_remote_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)
            runner = BUILD.Runner(evidence, evidence)
            failure = subprocess.TimeoutExpired(['go', 'test'], 3, output=b'public test started', stderr=b'fixture timeout')
            output = io.StringIO()
            with mock.patch.object(BUILD.subprocess, 'run', side_effect=failure), \
                    contextlib.redirect_stdout(output), self.assertRaisesRegex(ValueError, 'timeout'):
                runner(['go', 'test'], 'unit', timeout=3)
            self.assertIn('fixture timeout', output.getvalue())
            self.assertEqual((evidence / 'unit.stdout').read_bytes(), b'public test started')
            self.assertTrue(json.loads((evidence / 'commands.json').read_text())[0]['timed_out'])

    def test_expected_child_failure_is_recorded_without_becoming_pass_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = BUILD.Runner(Path(directory), Path(directory))
            result = subprocess.CompletedProcess(['gosu'], 37, b'', b'')
            with mock.patch.object(BUILD.subprocess, 'run', return_value=result):
                runner(['gosu'], 'exit-propagation', expected=37)
            row = json.loads((Path(directory) / 'commands.json').read_text())[0]
            self.assertEqual((row['returncode'], row['expected_returncode']), (37, 37))


if __name__ == '__main__':
    unittest.main()
