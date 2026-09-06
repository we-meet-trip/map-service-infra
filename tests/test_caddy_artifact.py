import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('install_caddy', Path(__file__).resolve().parents[1] / 'scripts/install-caddy-artifact.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class CaddyArtifactTests(unittest.TestCase):
    def test_override_cannot_select_other_services_mutable_images_or_unreviewed_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'compose.yml'
            for content in ('services: {edge: {image: "caddy:2-alpine"}}',
                            'services: {postgres: {image: "unrelated"}}'):
                output.write_text(content)
                with self.assertRaisesRegex(ValueError, 'unexpected edge override'):
                    installer.verify_compose(output)
            output.write_text('services:\n  edge:\n    image: sha256:' + 'a' * 64 +
                              '\n    platform: linux/amd64\n    pull_policy: never\n')
            with patch.object(installer.security, 'metadata', return_value={'platform_image_id': 'wrong'}):
                with self.assertRaisesRegex(ValueError, 'not the reviewed image'):
                    installer.verify_compose(output)

    def test_corrupted_transfer_and_changed_report_are_rejected_before_docker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, report, manifest = [root / name for name in ('image.tar', 'scan.json', 'manifest.json')]
            archive.write_bytes(b'image')
            report.write_text('{}')
            manifest.write_text(json.dumps({
                'schema_version': 1, 'platform': 'linux/amd64', 'archive_bytes': 5,
                'archive_sha256': hashlib.sha256(b'image').hexdigest(),
                'report_sha256': hashlib.sha256(b'{}').hexdigest(),
                'source_image_id': 'source', 'platform_image_id': 'platform',
            }))
            with patch.object(installer, 'MANIFEST', manifest), patch.object(installer.security, 'verify_report'), patch.object(installer.security, 'run') as run:
                installer.verify_files(archive, report)
                archive.write_bytes(b'other')
                with self.assertRaisesRegex(ValueError, 'archive checksum'):
                    installer.verify_files(archive, report)
                archive.write_bytes(b'image')
                report.write_text('{ }')
                with self.assertRaisesRegex(ValueError, 'report checksum'):
                    installer.verify_files(archive, report)
                run.assert_not_called()

    def test_loaded_wrong_platform_image_cannot_produce_install_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'compose.yml'
            with patch.object(installer.security, 'run'), patch.object(installer.security, 'metadata', return_value={'platform_image_id': 'wrong'}):
                with self.assertRaisesRegex(ValueError, 'loaded platform image mismatch'):
                    installer.install(Path('image.tar'), {'image': 'reviewed', 'platform_image_id': 'right'}, output)
            self.assertFalse(output.exists())

    def test_installer_preserves_an_existing_override_before_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'compose.yml'
            output.write_text('prior release')
            with patch.object(installer.security, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'already exists'):
                    installer.install(Path('image.tar'), {}, output)
                run.assert_not_called()
            self.assertEqual(output.read_text(), 'prior release')
