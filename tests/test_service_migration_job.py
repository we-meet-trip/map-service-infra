import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('service_migration_job', ROOT/'scripts/service-migration-job.py')
job = importlib.util.module_from_spec(spec); spec.loader.exec_module(job)
IMAGE = 'ghcr.io/we-meet-trip/map-service-user@sha256:'+'a'*64
HUB_IMAGE = 'ghcr.io/we-meet-trip/map-service-hub@sha256:'+'a'*64
AGENT_IMAGE = 'ghcr.io/we-meet-trip/map-service-agent@sha256:'+'a'*64
CID = 'c'*64; PGID = 'd'*64
IMAGE_ID = 'sha256:'+'b'*64
USER = job.service_contract('user')
HUB = job.service_contract('hub')
AGENT = job.service_contract('agent')

def config():
    return {'name':'map-test','services':{'user':{'image':IMAGE,'environment':{
        'USER_DATABASE_USER':'map_user_runtime','USER_DATABASE_PASSWORD':'runtime-only',
        'POSTGRES_DB':'map_test'}}}}

def credentials():
    return {'USER_MIGRATION_USERNAME':'map_user_migrator','USER_MIGRATION_PASSWORD':'migration-only',
            'USER_MIGRATION_URL':'jdbc:postgresql://postgres:5432/map_test?currentSchema=user_service'}

def hub_config():
    return {'name':'map-test','services':{'hub':{'image':HUB_IMAGE,'environment':{
        'HUB_DATABASE_URL':'postgresql+psycopg://map_hub_runtime:runtime-only@postgres:5432/map_test'}}}}

def hub_credentials():
    return {'HUB_MIGRATION_DATABASE_URL':
            'postgresql+psycopg://map_hub_migrator:migration-only@postgres:5432/map_test'}

def agent_config():
    return {'name':'map-test','services':{'agent':{'image':AGENT_IMAGE,'environment':{
        'POSTGRES_USER':'map_agent_runtime','POSTGRES_PASSWORD':'runtime-only',
        'POSTGRES_DB':'map_test','LANGGRAPH_SCHEMA':'langgraph'}}}}

def agent_credentials():
    return {'AGENT_CHECKPOINT_MIGRATION_DSN':
            'postgresql://map_agent_migrator:migration-only@postgres:5432/map_test'}

class ContractTests(unittest.TestCase):
    def test_mutable_image_or_shared_serving_secret_is_rejected(self):
        for mutate in (lambda c:c['services']['user'].update(image='user:latest'),
                       lambda c:c['services']['user'].update(image=HUB_IMAGE),
                       lambda c:c['services']['user']['environment'].update(POSTGRES_PASSWORD='shared'),
                       lambda c:c['services']['user']['environment'].update(USER_MIGRATION_PASSWORD='owner'),
                       lambda c:c['services']['user']['environment'].update(USER_MIGRATION_EXTRA='owner'),
                       lambda c:c['services']['user']['environment'].update(USER_DATABASE_USER='postgres')):
            c=config();mutate(c)
            with self.assertRaises(job.JobError):job.contract(USER,c,credentials())
    def test_cross_database_url_and_password_reuse_are_rejected(self):
        for key,value in [('USER_MIGRATION_URL','jdbc:postgresql://foreign/db?currentSchema=user_service'),
                          ('USER_MIGRATION_PASSWORD','runtime-only')]:
            v=credentials();v[key]=value
            with self.assertRaises(job.JobError):job.contract(USER,config(),v)
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
            with self.subTest(key=key),self.assertRaises(job.JobError):job.contract(USER,c,credentials())
        for key in ('command','entrypoint'):
            c=config();c['services']['user'][key]=['java','-Dspring.datasource.username=map']
            with self.subTest(key=key),self.assertRaises(job.JobError):job.contract(USER,c,credentials())
        c=config();c['services']['user']['environment']['JAVA_TOOL_OPTIONS']='-XX:MaxRAMPercentage=70'
        self.assertEqual(job.contract(USER,c,credentials()),('map-test',IMAGE,{}))

    def test_hub_serving_must_hold_only_the_restricted_runtime_dsn(self):
        self.assertEqual(job.contract(HUB,hub_config(),hub_credentials()),('map-test',HUB_IMAGE,{}))
        for mutate in (
                lambda c:c['services']['hub']['environment'].update(
                    HUB_DATABASE_URL='postgresql+psycopg://map:pw@postgres:5432/map_test'),
                lambda c:c['services']['hub']['environment'].update(
                    HUB_DATABASE_URL='postgresql+psycopg://map_hub_runtime:pw@foreign.invalid:5432/map_test'),
                lambda c:c['services']['hub']['environment'].update(HUB_MIGRATION_DATABASE_URL='owner'),
                lambda c:c['services']['hub']['environment'].pop('HUB_DATABASE_URL'),
                lambda c:c['services']['hub'].update(entrypoint=['python','-m','app.db.migrate'])):
            c=hub_config();mutate(c)
            with self.assertRaises(job.JobError):job.contract(HUB,c,hub_credentials())
    def test_hub_migrator_identity_target_and_password_must_differ_from_runtime(self):
        for value in ('postgresql+psycopg://map_hub_runtime:migration-only@postgres:5432/map_test',
                      'postgresql+psycopg://map_hub_migrator:migration-only@postgres:5432/other_db',
                      'postgresql+psycopg://map_hub_migrator:runtime-only@postgres:5432/map_test',
                      'postgresql+asyncpg://map_hub_migrator:migration-only@postgres:5432/map_test'):
            v={'HUB_MIGRATION_DATABASE_URL':value}
            with self.subTest(value=value.split('@')[0]),self.assertRaises(job.JobError):
                job.contract(HUB,hub_config(),v)

    def test_agent_serving_must_use_the_restricted_runtime_role(self):
        self.assertEqual(job.contract(AGENT,agent_config(),agent_credentials()),
                         ('map-test',AGENT_IMAGE,{'LANGGRAPH_SCHEMA':'langgraph'}))
        for mutate in (lambda c:c['services']['agent']['environment'].update(POSTGRES_USER='map'),
                       lambda c:c['services']['agent']['environment'].update(POSTGRES_PASSWORD=''),
                       lambda c:c['services']['agent']['environment'].update(POSTGRES_HOST='foreign.invalid'),
                       lambda c:c['services']['agent']['environment'].update(LANGGRAPH_SCHEMA='public; drop'),
                       lambda c:c['services']['agent']['environment'].update(
                           AGENT_CHECKPOINT_MIGRATION_DSN='postgresql://map_agent_migrator:x@postgres:5432/map_test')):
            c=agent_config();mutate(c)
            with self.assertRaises(job.JobError):job.contract(AGENT,c,agent_credentials())
    def test_agent_migrator_target_and_password_must_differ_from_runtime(self):
        for value in ('postgresql://map_agent_runtime:migration-only@postgres:5432/map_test',
                      'postgresql://map_agent_migrator:migration-only@postgres:5432/other_db',
                      'postgresql://map_agent_migrator:runtime-only@postgres:5432/map_test'):
            v={'AGENT_CHECKPOINT_MIGRATION_DSN':value}
            with self.subTest(value=value.split('@')[0]),self.assertRaises(job.JobError):
                job.contract(AGENT,agent_config(),v)
    def test_agent_schema_reaches_the_job_environment(self):
        c=agent_config();c['services']['agent']['environment']['LANGGRAPH_SCHEMA']='langgraph_test'
        self.assertEqual(job.contract(AGENT,c,agent_credentials())[2],{'LANGGRAPH_SCHEMA':'langgraph_test'})

    def test_credential_file_rejects_extra_keys_links_duplicates_and_world_readability(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'env'
            body=''.join(k+'='+v+'\n' for k,v in credentials().items())
            p.write_text(body);p.chmod(0o600)
            self.assertEqual(job.read_credentials(USER,p),credentials())
            for text in (body+'JWT_PRIVATE_KEY=unrelated\n',body+'USER_MIGRATION_PASSWORD=duplicate\n'):
                p.write_text(text)
                with self.assertRaises(job.JobError):job.read_credentials(USER,p)
            p.write_text(body);p.chmod(0o644)
            with self.assertRaises(job.JobError):job.read_credentials(USER,p)
            p.chmod(0o600);link=root/'symlink';link.symlink_to(p)
            with self.assertRaises(job.JobError):job.read_credentials(USER,link)
            hard=root/'hard';os.link(p,hard)
            with self.assertRaises(job.JobError):job.read_credentials(USER,p)
    def test_one_service_credential_file_is_not_accepted_by_another(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'env'
            for service,values in ((HUB,hub_credentials()),(AGENT,agent_credentials())):
                p.write_text(''.join(k+'='+v+'\n' for k,v in values.items()));p.chmod(0o600)
                self.assertEqual(job.read_credentials(service,p),values)
                for other in (USER,HUB,AGENT):
                    if other is service:continue
                    with self.subTest(owner=service['name'],other=other['name']),self.assertRaises(job.JobError):
                        job.read_credentials(other,p)
    def test_user_credential_file_must_name_the_dedicated_migrator_login(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'env';v=credentials();v['USER_MIGRATION_USERNAME']='postgres'
            p.write_text(''.join(k+'='+x+'\n' for k,x in v.items()));p.chmod(0o600)
            with self.assertRaises(job.JobError):job.read_credentials(USER,p)

    def test_another_launcher_cannot_race_a_migration_of_the_same_service(self):
        with tempfile.TemporaryDirectory() as d,job.job_lock(USER,Path(d)):
            with self.assertRaisesRegex(job.JobError,'another_migration_launcher_active'):
                with job.job_lock(USER,Path(d)):self.fail('concurrent launcher entered')
            # A different service has its own lock and is not blocked by this one.
            with job.job_lock(HUB,Path(d)):pass
    def test_orphan_running_job_blocks_another_image_without_stopping_it(self):
        calls=[]
        def command(args,timeout=30):
            calls.append(args)
            return CID if args[0]=='ps' else self.fail('must not remove a running job')
        with patch.object(job,'command',side_effect=command),patch.object(job,'container_state',return_value={'running':True}):
            with tempfile.TemporaryDirectory() as d,self.assertRaisesRegex(job.JobError,'previous_migration_still_running'):
                job.no_previous_job(USER,'map-test',Path(d))
        self.assertEqual(len(calls),1)

    def test_lost_create_response_is_recovered_without_starting_old_job(self):
        with tempfile.TemporaryDirectory() as d:
            directory=job.prepare_private_job(USER,Path(d),'map-test',IMAGE)
            secret=directory/'migration.env';secret.write_text('private');secret.chmod(0o600)
            calls=[]
            def command(args,timeout=30):
                calls.append(args)
                return CID if args[0]=='ps' else ''
            with patch.object(job,'command',side_effect=command),patch.object(job,'container_state',return_value={'running':False}):
                job.no_previous_job(USER,'map-test',Path(d))
            self.assertFalse(directory.exists())
            self.assertEqual([a for a in calls if a[0]!='ps'],[['rm',CID]])
    def test_cleanup_never_touches_another_service_private_job(self):
        with tempfile.TemporaryDirectory() as d:
            other=job.prepare_private_job(HUB,Path(d),'map-test',HUB_IMAGE)
            def command(args,timeout=30):return '' if args[0]=='ps' else self.fail('unexpected')
            with patch.object(job,'command',side_effect=command):
                job.no_previous_job(USER,'map-test',Path(d))
            self.assertTrue(other.exists())

    def test_orphan_cleanup_rejects_unknown_files_symlinks_and_foreign_markers(self):
        for kind in ('unknown','symlink','foreign','missing','other-service-image'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d:
                directory=job.prepare_private_job(USER,Path(d),'map-test',IMAGE)
                secret=directory/'migration.env';secret.write_text('private');secret.chmod(0o600)
                if kind=='unknown':(directory/'operator-backup').write_text('preserve')
                elif kind=='symlink':secret.unlink();secret.symlink_to(Path(d)/'unrelated')
                elif kind=='foreign':
                    marker=directory/'job.json';record=json.loads(marker.read_text());record['project']='map-service';marker.write_text(json.dumps(record))
                elif kind=='other-service-image':
                    marker=directory/'job.json';record=json.loads(marker.read_text());record['image']=HUB_IMAGE;marker.write_text(json.dumps(record))
                else:(directory/'job.json').unlink()
                with self.assertRaises(job.JobError):job.remove_private_job(USER,directory,'map-test')
                self.assertTrue(secret.exists() or secret.is_symlink())

class NetworkTests(unittest.TestCase):
    def network(self, override=None, service=None):
        service=service or USER
        value={'Internal':True,'Driver':'bridge','EnableIPv6':False,
               'Options':{job.GATEWAY_OPTION:'isolated'},
               'Labels':{job.LABEL:service['kind'],'kr.mapservice.project':'map-test'},'Containers':{PGID:{}}}
        value.update(override or {})
        return value
    def prepare(self, network, existing=True, service=None):
        service=service or USER
        calls=[]
        def command(args,timeout=30):
            calls.append(args)
            if args[0]=='ps':return PGID
            if args[:2]==['network','ls']:return 'map-test-'+service['suffix'] if existing else ''
            if args[:2]==['network','inspect']:return json.dumps([network])
            if args[:2]==['network','create']:return 'network-id'
            self.fail('unexpected Docker mutation')
        with patch.object(job,'command',side_effect=command),patch.object(job,'container_state',return_value={'id':PGID}):
            result=job.prepare_network(service,'map-test')
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
    def test_each_service_owns_its_own_private_network(self):
        for service in (USER,HUB,AGENT):
            result,_=self.prepare(self.network(service=service),service=service)
            self.assertEqual(result[0],'map-test-'+service['name']+'-migration')
        with self.subTest('other service label'),self.assertRaisesRegex(job.JobError,'network_not_owned'):
            self.prepare(self.network(service=HUB),service=USER)

class ExecutionTests(unittest.TestCase):
    def test_unknown_create_result_retains_private_marker_for_locked_recovery(self):
        def command(args,timeout=30):
            if args[:2]==['image','inspect']:return IMAGE_ID
            if args[0]=='create':raise job.JobError('docker_command_unavailable')
            self.fail('must not start or delete an unknown Docker job')
        with tempfile.TemporaryDirectory() as d,patch.object(job,'command',side_effect=command),\
             patch.object(job,'no_previous_job'),patch.object(job,'prepare_network',return_value=('private',PGID,{})):
            with self.assertRaisesRegex(job.JobError,'docker_command_unavailable'):
                job.run_job(USER,config(),credentials(),'migrate',Path(d))
            directory=next(Path(d).iterdir())
            self.assertEqual(job.read_credentials(USER,directory/'migration.env'),credentials())
            self.assertEqual(json.loads((directory/'job.json').read_text())['name'],directory.name)
    def execute(self, failure=False, service=None, setup=None, stdout=None):
        service=service or USER
        given=setup or (config(),credentials())
        calls=[];secret_files=[]
        pg={'id':PGID,'image':'sha256:'+'e'*64,'running':True,'exit':0,'oom':False}
        state={'id':CID,'image':IMAGE_ID,'running':failure,'exit':0,'oom':False}
        expected=dict(given[1]);expected.update(job.contract(service,given[0],given[1])[2])
        def command(args,timeout=30):
            calls.append(args)
            if args[:2]==['image','inspect']:return IMAGE_ID
            if args[0]=='create':
                secret=Path(args[args.index('--env-file')+1]);secret_files.append(secret)
                self.assertNotIn('runtime-only',' '.join(args))
                self.assertNotIn('migration-only',' '.join(args))
                return CID
            if args[0] in ('stop','rm'):return CID
            if args[:2]==['network','inspect']:return json.dumps([{'Containers':{PGID:{}}}])
            self.fail('unexpected Docker command')
        def start(*args,**kwargs):
            self.assertFalse(secret_files[0].exists(),'host secret retained while job runs')
            if failure:raise subprocess.TimeoutExpired(args[0],330)
            body=stdout if stdout is not None else json.dumps(
                {'status':'complete','operation':'migrate','migrations_executed':0})
            return subprocess.CompletedProcess(args[0],0,body,'')
        with tempfile.TemporaryDirectory() as d,patch.object(job,'command',side_effect=command),\
             patch.object(job,'no_previous_job'),patch.object(job,'prepare_network',return_value=('map-test-'+service['suffix'],PGID,pg)),\
             patch.object(job,'container_state',side_effect=lambda cid:pg if cid==PGID else state),\
             patch.object(job.subprocess,'run',side_effect=start):
            if failure:
                with self.assertRaisesRegex(job.JobError,'migration_host_deadline_exceeded'):
                    job.run_job(service,given[0],given[1],'migrate',Path(d))
                result=None
            else:
                result=job.run_job(service,given[0],given[1],'migrate',Path(d))
                self.assertEqual(result['status'],'PASS')
                self.assertEqual(result['service'],service['name'])
                self.assertNotIn('migration-only',json.dumps(result))
        return calls,result
    def test_success_requires_job_receipt_and_removes_only_created_job(self):
        calls,_=self.execute()
        self.assertEqual([x for x in calls if x[0]=='rm'],[['rm',CID]])
        self.assertFalse(any(x[0]=='stop' for x in calls))
    def test_host_timeout_stops_and_removes_the_job_and_does_not_touch_database(self):
        calls,_=self.execute(True)
        self.assertEqual([x for x in calls if x[0] in ('stop','rm')],[['stop','--time','10',CID],['rm',CID]])
    def test_hub_and_agent_completion_lines_are_required_exactly_once(self):
        for service,setup,line in ((HUB,(hub_config(),hub_credentials()),'Hub migration completed'),
                                   (AGENT,(agent_config(),agent_credentials()),'Agent checkpoint migration completed')):
            with self.subTest(service=service['name']):
                _,result=self.execute(service=service,setup=setup,stdout='noise\n'+line+'\n')
                self.assertEqual(result['result'],{'status':'complete','operation':'migrate'})
                for body in ('','migration failed\n',line+'\n'+line+'\n'):
                    with self.assertRaisesRegex(job.JobError,'migration_completion_receipt_missing'):
                        self.execute(service=service,setup=setup,stdout=body)
    def test_daemon_deadline_and_serving_environment_are_independent_of_host_parent(self):
        args=job.create_args(USER,'job','map-test',IMAGE,'private',Path('/private/env'),'migrate')
        self.assertIn('/usr/bin/timeout',args)
        self.assertIn('300s',args)
        self.assertIn('--kill-after=10s',args)
        self.assertNotIn('--env',args)
        self.assertFalse(any('REDIS' in a or 'JWT' in a or 'LOCATION' in a for a in args))
    def test_each_service_runs_its_own_pinned_migration_command_and_deadline(self):
        for service,image,expected,seconds in (
                (HUB,HUB_IMAGE,['python','-m','app.db.migrate'],'900s'),
                (AGENT,AGENT_IMAGE,['python','-m','app.checkpoint_migrate'],'900s')):
            args=job.create_args(service,'job','map-test',image,'private',Path('/private/env'),'migrate')
            self.assertEqual(args[-len(expected):],expected)
            self.assertIn(seconds,args)
            self.assertIn('--read-only',args);self.assertIn('--cap-drop=ALL',args)
            self.assertIn('--user',args);self.assertIn('10001',args)
        for service,operation in ((HUB,'validate'),(AGENT,'check-config')):
            with self.subTest(service=service['name']),self.assertRaisesRegex(job.JobError,'migration_operation_invalid'):
                job.create_args(service,'job','map-test',IMAGE,'private',Path('/private/env'),operation)

if __name__=='__main__':unittest.main()
