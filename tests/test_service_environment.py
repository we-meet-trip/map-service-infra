"""Exercise real Compose interpolation without exposing any environment values."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent

@unittest.skipUnless(shutil.which('docker'), 'Docker Compose is required')
class ServiceEnvironmentTests(unittest.TestCase):
    def test_secret_access_is_limited_to_its_consumers(self):
        result = subprocess.run(['docker','compose','--env-file','.env.example','-f','docker-compose.yml',
                                 '--profile','full','--profile','vision','config','--format','json'],
                                cwd=ROOT, capture_output=True, text=True,
                                env={**os.environ,'JWT_PRIVATE_KEY':'sentinel-signing-key',
                                     'POSTGRES_PASSWORD':'sentinel-database-password',
                                     'UNRELATED_SECRET':'must-not-be-forwarded'})
        self.assertEqual(result.returncode, 0, 'Compose must render')
        services=json.loads(result.stdout)['services']
        for name in ('agent','hub','yolo'):
            self.assertNotIn('JWT_PRIVATE_KEY', services[name]['environment'])
        for name in ('hub','yolo'):
            self.assertNotIn('POSTGRES_PASSWORD', services[name]['environment'])
        for name in ('user','agent','hub','yolo'):
            self.assertNotIn('UNRELATED_SECRET', services[name]['environment'])
            self.assertNotIn('env_file', services[name])
        self.assertEqual(services['user']['environment']['JWT_PRIVATE_KEY'],'sentinel-signing-key')
        self.assertEqual(services['agent']['environment']['TRAINING_CAPTURE_ENABLED'],'false')
