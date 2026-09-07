import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location('grafana_core_build', HERE / 'build.py')
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)
PINS = json.loads((HERE / 'pins.json').read_text())


class CoreSecurityContract(unittest.TestCase):
    def source(self):
        return 'module fixture\nrequire (\n' + ''.join(
            '\t' + name + ' ' + version + ' // retained owner\n'
            for name, version in PINS['original_versions'].items()) + ')\n'

    def test_patching_rejects_missing_duplicate_or_unexpected_source(self):
        source = self.source()
        actual = BUILD.patch_module_text(source, PINS)
        self.assertEqual(actual.count('// retained owner'), 2)
        for name, value in PINS['module_patches'].items():
            self.assertIn(name + ' ' + value['version'], actual)
        for invalid in (source + source, source.replace('v1.82.1', 'v1.83.0'), ''):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                BUILD.patch_module_text(invalid, PINS)

    def test_effective_graph_rejects_hidden_replacements_downgrades_and_duplicates(self):
        good = [{'Path': name, 'Version': value['version']}
                for name, value in PINS['module_patches'].items()]
        BUILD.verify_effective_modules(good, PINS)
        for bad in (good[1:], good + good[:1],
                    [dict(good[0], Replace={'Path': '/tmp/untrusted'}), good[1]],
                    [dict(good[0], Version='v0.1.0'), good[1]]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                BUILD.verify_effective_modules(bad, PINS)

    def test_module_json_stream_does_not_discard_trailing_garbage(self):
        self.assertEqual(BUILD.json_stream('{"Path":"a"}\n {"Path":"b"}'),
                         [{'Path': 'a'}, {'Path': 'b'}])
        with self.assertRaises(json.JSONDecodeError):
            BUILD.json_stream('{"Path":"a"} unexpected')

    def test_default_plan_does_not_start_build_or_subprocess(self):
        with patch('sys.argv', ['build.py']), patch.object(BUILD, 'build') as build, \
                patch.object(BUILD.subprocess, 'run') as execute, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            BUILD.main()
        build.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['status'], 'PLAN_ONLY')


if __name__ == '__main__':
    unittest.main()
