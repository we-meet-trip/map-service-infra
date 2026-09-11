import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('pg_backup', Path(__file__).parents[1] / 'scripts/pg_backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class S3IntegrityTest(unittest.TestCase):
    def upload(self, downloaded, exit_code=0):
        self.commands = []
        def run(args, **kwargs):
            self.commands.append(args)
            return SimpleNamespace(stdout='4\n')
        process = SimpleNamespace(stdout=io.BytesIO(downloaded), wait=lambda: exit_code,
                                  poll=lambda: exit_code)
        with tempfile.TemporaryDirectory() as temporary:
            files = [Path(temporary) / 'map-data.sql.gz', Path(temporary) / 'map.manifest.json']
            for path in files:
                path.write_bytes(b'good')
            with patch.object(backup, 'run', side_effect=run), patch.object(
                    backup.subprocess, 'Popen', return_value=process):
                # Only the first file is needed to prove fail-before-manifest.
                backup.upload(files[:1] if downloaded == b'good' and not exit_code else files,
                              's3://backup-bucket/test', 'https://objects.example.invalid')

    def test_same_length_corruption_prevents_manifest_publication(self):
        with self.assertRaisesRegex(backup.BackupError, 'checksum mismatch'):
            self.upload(b'evil')
        self.assertFalse(any('map.manifest.json' in str(arg) for args in self.commands for arg in args))

    def test_valid_remote_bytes_pass(self):
        self.upload(b'good')

    def test_download_failure_is_not_success(self):
        with self.assertRaisesRegex(backup.BackupError, 'checksum mismatch'):
            self.upload(b'good', 1)

    def test_parent_path_is_rejected_before_upload(self):
        with self.assertRaises(backup.BackupError), patch.object(backup, 'run') as run:
            backup.upload([], 's3://backup-bucket/../prod', 'https://objects.example.invalid')
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
