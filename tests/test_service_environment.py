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
        # The database container's operator password must not reach a service
        # merely because both read a variable of the same name.
        self.assertNotEqual(services['agent']['environment']['POSTGRES_PASSWORD'],
                            'sentinel-database-password')
        self.assertEqual(services['agent']['environment']['POSTGRES_USER'],'map_agent_runtime')
        self.assertNotIn('AGENT_CHECKPOINT_MIGRATION_DSN',services['agent']['environment'])
        self.assertNotIn('HUB_MIGRATION_DATABASE_URL',services['hub']['environment'])
        self.assertIn('map_hub_runtime',services['hub']['environment']['HUB_DATABASE_URL'])
        spec=importlib.util.spec_from_file_location('migration_job_contract',ROOT/'scripts/service-migration-job.py')
        job=importlib.util.module_from_spec(spec);spec.loader.exec_module(job)
        rendered=json.loads(result.stdout)
        rendered['name']='map-test'
        database=services['user']['environment']['POSTGRES_DB']
        for name,credentials in (
                ('user',{'USER_MIGRATION_URL':f'jdbc:postgresql://postgres:5432/{database}?currentSchema=user_service',
                         'USER_MIGRATION_USERNAME':'map_user_migrator',
                         'USER_MIGRATION_PASSWORD':'separate-migration-sentinel'}),
                ('hub',{'HUB_MIGRATION_DATABASE_URL':
                        f'postgresql+psycopg://map_hub_migrator:separate-migration-sentinel@postgres:5432/{database}'}),
                ('agent',{'AGENT_CHECKPOINT_MIGRATION_DSN':
                          f'postgresql://map_agent_migrator:separate-migration-sentinel@postgres:5432/{database}'})):
            with self.subTest(service=name):
                rendered['services'][name]['image']=(
                    f'ghcr.io/we-meet-trip/map-service-{name}@sha256:'+'a'*64)
                self.assertEqual(job.contract(job.service_contract(name),rendered,credentials)[0],'map-test')
