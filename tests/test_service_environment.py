"""Exercise real Compose interpolation without exposing any environment values."""
import json
import importlib.util
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
        for name in ('user','hub','yolo'):
            self.assertNotIn('POSTGRES_PASSWORD', services[name]['environment'])
        for name in ('user','agent','hub','yolo'):
            self.assertNotIn('UNRELATED_SECRET', services[name]['environment'])
            self.assertNotIn('env_file', services[name])
        self.assertNotIn('POSTGRES_USER',services['user']['environment'])
        self.assertFalse(any(k.startswith('USER_MIGRATION_') for k in services['user']['environment']))
        self.assertEqual(services['user']['environment']['USER_DATABASE_USER'],'map_user_runtime')
        self.assertEqual(services['user']['environment']['JWT_PRIVATE_KEY'],'sentinel-signing-key')
        self.assertEqual(services['agent']['environment']['TRAINING_CAPTURE_ENABLED'],'false')
        spec=importlib.util.spec_from_file_location('migration_job_contract',ROOT/'scripts/user-migration-job.py')
        job=importlib.util.module_from_spec(spec);spec.loader.exec_module(job)
        rendered=json.loads(result.stdout)
        rendered['name']='map-test'
        rendered['services']['user']['image']='ghcr.io/we-meet-trip/map-service-user@sha256:'+'a'*64
        database=services['user']['environment']['POSTGRES_DB']
        self.assertEqual(job.contract(rendered,{
            'USER_MIGRATION_URL':f'jdbc:postgresql://postgres:5432/{database}?currentSchema=user_service',
            'USER_MIGRATION_USERNAME':'map_user_migrator',
            'USER_MIGRATION_PASSWORD':'separate-migration-sentinel'})[0],'map-test')
