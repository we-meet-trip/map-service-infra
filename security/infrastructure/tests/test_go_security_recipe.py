"""Synthetic stdlib tests; do not start Docker, Go, services or remote builds."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import struct
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'go-security/build.py'
SPEC = importlib.util.spec_from_file_location('go_security_builder', SCRIPT)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class GoSecurityRecipeTest(unittest.TestCase):
    def recipe(self):
        spec = builder.read_pins()['services']['prometheus']
        return builder.plan('prometheus', spec['runtime_base'], '1' * 40)

    def test_unreviewed_runtime_or_source_cannot_build(self):
        recipe = self.recipe()
        with self.assertRaisesRegex(ValueError, 'runtime_base'):
            builder.plan('prometheus', 'prom/prometheus:latest', '1' * 40)
        with self.assertRaisesRegex(ValueError, 'source_sha'):
            builder.plan('prometheus', recipe['spec']['runtime_base'], 'develop')
        with self.assertRaisesRegex(ValueError, 'unknown_service'):
            builder.plan('grafana', recipe['spec']['runtime_base'], '1' * 40)

    def test_local_execute_fails_before_subprocess(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(builder.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'remote_linux_builder_only'):
                builder.execute(self.recipe())
            run.assert_not_called()

    def test_module_checksums_and_exact_version_are_all_required(self):
        pin = {'version': 'v1.2.3', 'sum': 'h1:synthetic-module', 'go_mod_sum': 'h1:synthetic-modfile'}
        good = {'Version': pin['version'], 'Sum': pin['sum'], 'GoModSum': pin['go_mod_sum']}
        builder.verify_download(pin, good)
        for key in good:
            with self.subTest(key=key), self.assertRaises(ValueError):
                builder.verify_download(pin, {**good, key: 'changed'})

    def archive(self, root, extra=None):
        path = root / 'ui.tar.gz'
        with tarfile.open(path, 'w:gz') as tar:
            for name in ['static/mantine-ui/index.html', 'static/react-app/index.html']:
                info = tarfile.TarInfo(name)
                info.size = len(b'<html>synthetic</html>')
                tar.addfile(info, io.BytesIO(b'<html>synthetic</html>'))
            if extra:
                tar.addfile(extra)
        return path

    def test_ui_archive_accepts_both_real_layout_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / 'unpacked'
            builder.unpack_ui(self.archive(root), destination)
            self.assertTrue((destination / 'static/react-app/index.html').is_file())
            self.assertTrue((destination / 'static/mantine-ui/index.html').is_file())

    def test_ui_archive_rejects_traversal_links_and_duplicate_paths(self):
        for name, kind in [('../outside', tarfile.REGTYPE), ('static/link', tarfile.SYMTYPE),
                           ('static/react-app/index.html', tarfile.REGTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                info = tarfile.TarInfo(name)
                info.type, info.linkname = kind, '/outside'
                archive = self.archive(root, info)
                with self.assertRaises(ValueError):
                    builder.unpack_ui(archive, root / 'unpacked')
                self.assertFalse((root / 'outside').exists())

    def test_module_inventory_preserves_add_remove_replace_diffs(self):
        before = [{'Path': 'main', 'Main': True}, {'Path': 'module-a', 'Version': 'v1.0.0'},
                  {'Path': 'removed', 'Version': 'v1.0.0'}]
        after = [{'Path': 'main', 'Main': True}, {'Path': 'module-a', 'Version': 'v1.1.0'},
                 {'Path': 'added', 'Version': 'v1.0.0', 'Replace': {'Path': './local'}}]
        decoded = builder.json_stream('\n'.join(json.dumps(x) for x in before))
        self.assertEqual(decoded, before)
        changes = builder.module_changes(decoded, after)
        self.assertEqual({x['module'] for x in changes}, {'module-a', 'removed', 'added'})
        self.assertEqual(changes[0]['after']['Replace'], {'Path': './local'})

    def test_actual_elf_shape_rejects_arm_or_dynamic_loader(self):
        header = bytearray(64)
        header[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', header, 18, 62)
        struct.pack_into('<Q', header, 32, 64)
        struct.pack_into('<HH', header, 54, 56, 1)
        entry = bytearray(56)
        struct.pack_into('<I', entry, 0, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.elf'
            path.write_bytes(header + entry)
            self.assertEqual(builder.static_elf(path)['machine'], 'EM_X86_64')
            struct.pack_into('<I', entry, 0, 3)
            path.write_bytes(header + entry)
            with self.assertRaisesRegex(ValueError, 'dynamic_loader'):
                builder.static_elf(path)
            struct.pack_into('<H', header, 18, 183)
            path.write_bytes(header + entry)
            with self.assertRaisesRegex(ValueError, 'amd64_machine'):
                builder.static_elf(path)


if __name__ == '__main__':
    unittest.main()
