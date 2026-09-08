import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('user_migration_job', ROOT/'scripts/user-migration-job.py')
job = importlib.util.module_from_spec(spec); spec.loader.exec_module(job)
IMAGE = 'ghcr.io/we-meet-trip/map-service-user@sha256:'+'a'*64
CID = 'c'*64; PGID = 'd'*64
IMAGE_ID = 'sha256:'+'b'*64

def config():
    return {'name':'map-test','services':{'user':{'image':IMAGE,'environment':{
        'USER_DATABASE_USER':'map_user_runtime','USER_DATABASE_PASSWORD':'runtime-only',
        'POSTGRES_DB':'map_test'}}}}

def credentials():
    return {'USER_MIGRATION_USERNAME':'map_user_migrator','USER_MIGRATION_PASSWORD':'migration-only',
            'USER_MIGRATION_URL':'jdbc:postgresql://postgres:5432/map_test?currentSchema=user_service'}

class ContractTests(unittest.TestCase):
    def test_mutable_image_or_shared_serving_secret_is_rejected(self):
        for mutate in (lambda c:c['services']['user'].update(image='user:latest'),
                       lambda c:c['services']['user']['environment'].update(POSTGRES_PASSWORD='shared'),
                       lambda c:c['services']['user']['environment'].update(USER_MIGRATION_PASSWORD='owner'),
                       lambda c:c['services']['user']['environment'].update(USER_DATABASE_USER='postgres')):
            c=config();mutate(c)
            with self.assertRaises(job.JobError):job.contract(c,credentials())
    def test_cross_database_url_and_password_reuse_are_rejected(self):
        for key,value in [('USER_MIGRATION_URL','jdbc:postgresql://foreign/db?currentSchema=user_service'),
                          ('USER_MIGRATION_PASSWORD','runtime-only')]:
            v=credentials();v[key]=value
            with self.assertRaises(job.JobError):job.contract(config(),v)
    def test_serving_target_and_hidden_spring_overrides_cannot_diverge(self):
        for key, value in [('POSTGRES_HOST','foreign.invalid'), ('POSTGRES_PORT','6543'),
                           ('SPRING_DATASOURCE_URL','jdbc:postgresql://foreign.invalid/db'),
                           ('SPRING_APPLICATION_JSON','{"spring":{"datasource":{}}}'),
                           ('JAVA_TOOL_OPTIONS','-Dspring.datasource.url=foreign'),
                           ('JDK_JAVA_OPTIONS','-Dspring.datasource.username=map'),
                           ('LOADER_MAIN','another.Main'),
                           ('spring.datasource.url','jdbc:postgresql://foreign.invalid/db'),
                           ('SPRING.DATASOURCE.URL','jdbc:postgresql://foreign.invalid/db'),
                           ('spring_datasource_url','jdbc:postgresql://foreign.invalid/db'),
                           ('Spring-Datasource-Url','jdbc:postgresql://foreign.invalid/db'),
                           ('java_tool_options','-Dspring.datasource.url=foreign'),
                           ('loader.main','another.Main')]:
            c=config();c['services']['user']['environment'][key]=value
            with self.subTest(key=key),self.assertRaises(job.JobError):job.contract(c,credentials())
        for key in ('command','entrypoint'):
            c=config();c['services']['user'][key]=['java','-Dspring.datasource.username=map']
            with self.subTest(key=key),self.assertRaises(job.JobError):job.contract(c,credentials())
        c=config();c['services']['user']['environment']['JAVA_TOOL_OPTIONS']='-XX:MaxRAMPercentage=70'
        self.assertEqual(job.contract(c,credentials()),('map-test',IMAGE))
    def test_credential_file_rejects_extra_keys_links_duplicates_and_world_readability(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'env'
            body=''.join(k+'='+v+'\n' for k,v in credentials().items())
            p.write_text(body);p.chmod(0o600)
            self.assertEqual(job.read_credentials(p),credentials())
            for text in (body+'JWT_PRIVATE_KEY=unrelated\n',body+'USER_MIGRATION_PASSWORD=duplicate\n'):
                p.write_text(text)
                with self.assertRaises(job.JobError):job.read_credentials(p)
            p.write_text(body);p.chmod(0o644)
            with self.assertRaises(job.JobError):job.read_credentials(p)
            p.chmod(0o600);link=root/'symlink';link.symlink_to(p)
            with self.assertRaises(job.JobError):job.read_credentials(link)
            hard=root/'hard';os.link(p,hard)
            with self.assertRaises(job.JobError):job.read_credentials(p)
    def test_another_launcher_cannot_race_a_migration(self):
        with tempfile.TemporaryDirectory() as d,job.job_lock(Path(d)):
            with self.assertRaisesRegex(job.JobError,'another_migration_launcher_active'):
                with job.job_lock(Path(d)):self.fail('concurrent launcher entered')
    def test_orphan_running_job_blocks_another_image_without_stopping_it(self):
        calls=[]
        def command(args,timeout=30):
            calls.append(args)
            return CID if args[0]=='ps' else self.fail('must not remove a running job')
        with patch.object(job,'command',side_effect=command),patch.object(job,'container_state',return_value={'running':True}):
            with tempfile.TemporaryDirectory() as d,self.assertRaisesRegex(job.JobError,'previous_migration_still_running'):
                job.no_previous_job('map-test',Path(d))
        self.assertEqual(len(calls),1)

    def test_lost_create_response_is_recovered_without_starting_old_job(self):
        with tempfile.TemporaryDirectory() as d:
            directory=job.prepare_private_job(Path(d),'map-test',IMAGE)
            secret=directory/'migration.env';secret.write_text('private');secret.chmod(0o600)
            calls=[]
            def command(args,timeout=30):
                calls.append(args)
                return CID if args[0]=='ps' else ''
            with patch.object(job,'command',side_effect=command),patch.object(job,'container_state',return_value={'running':False}):
                job.no_previous_job('map-test',Path(d))
            self.assertFalse(directory.exists())
            self.assertEqual([a for a in calls if a[0]!='ps'],[['rm',CID]])

    def test_orphan_cleanup_rejects_unknown_files_symlinks_and_foreign_markers(self):
        for kind in ('unknown','symlink','foreign','missing'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d:
                directory=job.prepare_private_job(Path(d),'map-test',IMAGE)
                secret=directory/'migration.env';secret.write_text('private');secret.chmod(0o600)
                if kind=='unknown':(directory/'operator-backup').write_text('preserve')
                elif kind=='symlink':secret.unlink();secret.symlink_to(Path(d)/'unrelated')
                elif kind=='foreign':
                    marker=directory/'job.json';record=json.loads(marker.read_text());record['project']='map-service';marker.write_text(json.dumps(record))
                else:(directory/'job.json').unlink()
                with self.assertRaises(job.JobError):job.remove_private_job(directory,'map-test')
                self.assertTrue(secret.exists() or secret.is_symlink())

class NetworkTests(unittest.TestCase):
    def network(self, override=None):
        value={'Internal':True,'Driver':'bridge','EnableIPv6':False,
               'Options':{job.GATEWAY_OPTION:'isolated'},
               'Labels':{job.LABEL:job.KIND,'kr.mapservice.project':'map-test'},'Containers':{PGID:{}}}
        value.update(override or {})
        return value
    def prepare(self, network, existing=True):
        calls=[]
        def command(args,timeout=30):
            calls.append(args)
            if args[0]=='ps':return PGID
            if args[:2]==['network','ls']:return 'map-test-user-migration' if existing else ''
            if args[:2]==['network','inspect']:return json.dumps([network])
            if args[:2]==['network','create']:return 'network-id'
            self.fail('unexpected Docker mutation')
        with patch.object(job,'command',side_effect=command),patch.object(job,'container_state',return_value={'id':PGID}):
            result=job.prepare_network('map-test')
        return result,calls
    def test_host_gateway_and_ipv6_must_be_isolated_before_attachment(self):
        for override in ({'Options':{}},{'Options':{job.GATEWAY_OPTION:'nat'}},{'EnableIPv6':True}):
            with self.subTest(override=override),self.assertRaisesRegex(job.JobError,'gateway_not_isolated'):
                self.prepare(self.network(override))
        result,calls=self.prepare(self.network(),existing=False)
        self.assertEqual(result[1],PGID)
        create=next(a for a in calls if a[:2]==['network','create'])
        self.assertIn('--internal',create);self.assertIn('--ipv6=false',create)
        self.assertIn(job.GATEWAY_OPTION+'=isolated',create)
    def test_foreign_network_endpoint_is_rejected(self):
        with self.assertRaisesRegex(job.JobError,'foreign_endpoint'):
            self.prepare(self.network({'Containers':{PGID:{},CID:{}}}))

class ExecutionTests(unittest.TestCase):
    def test_unknown_create_result_retains_private_marker_for_locked_recovery(self):
        def command(args,timeout=30):
            if args[:2]==['image','inspect']:return IMAGE_ID
            if args[0]=='create':raise job.JobError('docker_command_unavailable')
            self.fail('must not start or delete an unknown Docker job')
        with tempfile.TemporaryDirectory() as d,patch.object(job,'command',side_effect=command),\
             patch.object(job,'no_previous_job'),patch.object(job,'prepare_network',return_value=('private',PGID,{})):
            with self.assertRaisesRegex(job.JobError,'docker_command_unavailable'):
                job.run_job(config(),credentials(),'migrate',Path(d))
            directory=next(Path(d).iterdir())
            self.assertEqual(job.read_credentials(directory/'migration.env'),credentials())
            self.assertEqual(json.loads((directory/'job.json').read_text())['name'],directory.name)
    def execute(self, failure=False):
        calls=[];secret_files=[]
        pg={'id':PGID,'image':'sha256:'+'e'*64,'running':True,'exit':0,'oom':False}
        state={'id':CID,'image':IMAGE_ID,'running':failure,'exit':0,'oom':False}
        def command(args,timeout=30):
            calls.append(args)
            if args[:2]==['image','inspect']:return IMAGE_ID
            if args[0]=='create':
                secret=Path(args[args.index('--env-file')+1]);secret_files.append(secret)
                self.assertEqual(job.read_credentials(secret),credentials())
                self.assertNotIn('runtime-only',' '.join(args))
                self.assertNotIn('migration-only',' '.join(args))
                return CID
            if args[0] in ('stop','rm'):return CID
            if args[:2]==['network','inspect']:return json.dumps([{'Containers':{PGID:{}}}])
            self.fail('unexpected Docker command')
        def start(*args,**kwargs):
            self.assertFalse(secret_files[0].exists(),'host secret retained while job runs')
            if failure:raise subprocess.TimeoutExpired(args[0],330)
            return subprocess.CompletedProcess(args[0],0,json.dumps({'status':'complete','operation':'migrate','migrations_executed':0}),'')
        with tempfile.TemporaryDirectory() as d,patch.object(job,'command',side_effect=command),\
             patch.object(job,'no_previous_job'),patch.object(job,'prepare_network',return_value=('map-test-user-migration',PGID,pg)),\
             patch.object(job,'container_state',side_effect=lambda cid:pg if cid==PGID else state),\
             patch.object(job.subprocess,'run',side_effect=start):
            if failure:
                with self.assertRaisesRegex(job.JobError,'migration_host_deadline_exceeded'):
                    job.run_job(config(),credentials(),'migrate',Path(d))
            else:
                result=job.run_job(config(),credentials(),'migrate',Path(d))
                self.assertEqual(result['status'],'PASS')
                self.assertNotIn('migration-only',json.dumps(result))
        return calls
    def test_success_requires_job_receipt_and_removes_only_created_job(self):
        calls=self.execute()
        self.assertEqual([x for x in calls if x[0]=='rm'],[['rm',CID]])
        self.assertFalse(any(x[0]=='stop' for x in calls))
    def test_host_timeout_stops_and_removes_the_job_and_does_not_touch_database(self):
        calls=self.execute(True)
        self.assertEqual([x for x in calls if x[0] in ('stop','rm')],[['stop','--time','10',CID],['rm',CID]])
    def test_daemon_deadline_and_serving_environment_are_independent_of_host_parent(self):
        args=job.create_args('job','map-test',IMAGE,'private',Path('/private/env'),'migrate')
        self.assertIn('/usr/bin/timeout',args)
        self.assertIn('300s',args)
        self.assertIn('--kill-after=10s',args)
        self.assertNotIn('--env',args)
        self.assertFalse(any('REDIS' in a or 'JWT' in a or 'LOCATION' in a for a in args))

if __name__=='__main__':unittest.main()
