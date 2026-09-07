#!/usr/bin/env python3
"""Exact current/candidate scans and synthetic compatibility; default is plan only."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / 'security/infrastructure/manifest.json'
SERVICES = {'postgres','redis','proxy','dns','prometheus','grafana','postgres-exporter','redis-exporter','node-exporter'}
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')


def require(value, message):
    if not value: raise ValueError(message)


def run(args, *, accepted=(0,), timeout=900, input=None):
    result = subprocess.run(args, input=input, capture_output=True, timeout=timeout)
    require(result.returncode in accepted, 'command_failed:' + Path(args[0]).name)
    return result


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def write(path, data):
    with Path(path).open('x') as stream: json.dump(data, stream, indent=2, sort_keys=True); stream.write('\n')


def load_spec():
    spec = json.loads(SPEC.read_text())
    require(spec['schema_version']==1 and spec['platform']=='linux/amd64', 'manifest_schema')
    require({r['service'] for r in spec['services']}==SERVICES and len(spec['services'])==9, 'exact_nine_services')
    for row in spec['services']:
        require(re.fullmatch(r'[a-z0-9/_-]+@sha256:[a-f0-9]{64}', row['current']), 'immutable_current_required')
        require(row['build'] in ('postgres','preserve','upstream','os-update'), 'build_mode')
        require(row['candidate_selector'].split(':')[0].split('@')[0]==row['current'].split('@')[0], 'repository_change_forbidden')
    require(spec['candidate_security_approved'] is False, 'unreviewed_manifest_cannot_approve')
    return spec


def resolve(ref, receipt):
    raw=run(['docker','buildx','imagetools','inspect','--raw',ref],timeout=120).stdout
    data=json.loads(raw)
    if '@sha256:' in ref:
        actual=ref
    else:
        choices={m['digest'] for m in data.get('manifests',[]) if m.get('platform',{}).get('os')=='linux' and m.get('platform',{}).get('architecture')=='amd64' and m.get('platform',{}).get('variant') in (None,'')}
        require(len(choices)==1,'unique_linux_amd64_manifest_required')
        digest=choices.pop();require(DIGEST.fullmatch(digest),'manifest_digest')
        actual=ref.rsplit(':',1)[0]+'@'+digest
    write(receipt,{'selector':ref,'resolved':actual,'resolved_at':datetime.now(timezone.utc).isoformat(),'registry_manifest_json_sha256':hashlib.sha256(raw).hexdigest()})
    return actual


def oci_identity(path):
    with tarfile.open(path,'r:*') as archive:
        def read(name):
            member=archive.getmember(name)
            require(member.isfile() and member.size<8*1024*1024,'bounded_oci_metadata')
            return archive.extractfile(member).read()
        def blob(digest):
            require(DIGEST.fullmatch(digest),'oci_digest')
            raw=read('blobs/sha256/'+digest.split(':')[1])
            require('sha256:'+hashlib.sha256(raw).hexdigest()==digest,'oci_blob_checksum')
            return json.loads(raw)
        index_raw=read('index.json');index_digest='sha256:'+hashlib.sha256(index_raw).hexdigest()
        index=json.loads(index_raw)
        while 'manifests' in index:
            candidates=[x for x in index['manifests'] if x.get('platform',{}).get('architecture') not in ('unknown','arm64')]
            require(len(candidates)==1,'single_platform_candidate_required')
            descriptor=candidates[0];index=blob(descriptor['digest'])
        config=blob(index['config']['digest'])
        require(config['architecture']=='amd64' and config['os']=='linux','oci_platform')
        return {'index_digest':index_digest,'manifest_digest':descriptor['digest'],'config_digest':index['config']['digest'],'archive_sha256':sha(path),'source_sha':config.get('config',{}).get('Labels',{}).get('org.opencontainers.image.revision'),'runtime_config':config.get('config',{}),'rootfs':config.get('rootfs',{})}


def build_candidate(row, base, output, source_sha):
    run(['docker','pull','--platform','linux/amd64',base],timeout=600)
    inspected=json.loads(run(['docker','image','inspect',base]).stdout)[0]
    user=inspected.get('Config',{}).get('User') or '0'
    require(re.fullmatch(r'[a-zA-Z0-9_.:-]+',user),'runtime_user_contract')
    oci=output/'candidate.oci.tar';docker=output/'candidate.docker.tar'
    tag='map-infra-candidate:'+row['service']+'-'+source_sha[:12]
    run(['docker','buildx','build','--platform','linux/amd64','--provenance=false','--file',str(ROOT/'security/infrastructure'/('Dockerfile.'+row['build'])),'--build-arg','BASE='+base,'--build-arg','SOURCE_SHA='+source_sha,'--build-arg','RUNTIME_USER='+user,'--tag',tag,'--output','type=oci,dest='+str(oci),'--output','type=docker,dest='+str(docker),str(ROOT/'security/infrastructure')],timeout=1200)
    identity=oci_identity(oci)
    require(identity['source_sha']==source_sha,'candidate_source_sha')
    run(['docker','load','--input',str(docker)],timeout=600)
    current=json.loads(run(['docker','image','inspect',tag]).stdout)[0]
    # Docker containerd stores can expose an OCI index as Image ID. Re-export the
    # loaded tag and verify its exact config bytes instead of conflating digest kinds.
    loaded=output/'loaded-runtime.docker.tar'
    run(['docker','image','save','--output',str(loaded),tag],timeout=600)
    with tarfile.open(loaded,'r:*') as archive:
        entry=archive.getmember('manifest.json');require(entry.isfile() and entry.size<1024*1024,'docker_export_metadata')
        manifests=json.loads(archive.extractfile(entry).read());require(len(manifests)==1,'single_loaded_image_required')
        config_entry=archive.getmember(manifests[0]['Config']);require(config_entry.isfile() and config_entry.size<8*1024*1024,'bounded_loaded_config')
        config_bytes=archive.extractfile(config_entry).read()
        require('sha256:'+hashlib.sha256(config_bytes).hexdigest()==identity['config_digest'],'fixture_and_scan_image_config_mismatch')
    require(DIGEST.fullmatch(current['Id']),'loaded_runtime_image_identifier')
    identity['runtime_image_id']=current['Id'];identity['loaded_config_bytes_verified']=True
    identity['scan_archive_sha256']=sha(loaded)
    identity['scan_archive_format']='docker-save'
    docker.unlink()  # Keep the verified Docker save archive until Trivy reads it.
    write(output/'candidate-identity.json',identity)
    return current['Id'],identity


def findings(report):
    counts=Counter();records=[];packages=[]
    for result in report.get('Results',[]):
        for p in result.get('Packages') or []:
            packages.append({'target':result.get('Target'),'type':result.get('Type'),'name':p.get('Name'),'version':p.get('Version'),'id':p.get('ID')})
        for v in result.get('Vulnerabilities') or []:
            if v['Severity'] not in ('HIGH','CRITICAL'): continue
            counts[v['Severity']]+=1
            records.append({k:v.get(k) for k in ('VulnerabilityID','PkgName','InstalledVersion','FixedVersion','Status','Severity','PrimaryURL')})
    return {'counts':{k:counts[k] for k in ('HIGH','CRITICAL')},'findings':records,'packages':packages}


def scan(spec, output, cache, name, *, ref=None, archive=None, identity=None):
    args=['docker','run','--rm','--platform','linux/amd64','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','2g','--cpus','2','--user',f'{os.getuid()}:{os.getgid()}','-v',str(cache)+':/cache','-v',str(output)+':/reports','-v',str(cache/'scratch')+':/tmp',spec['scanner'],'image','--cache-dir','/cache','--skip-db-update','--skip-java-db-update','--offline-scan','--timeout','15m','--no-progress','--scanners','vuln','--severity','HIGH,CRITICAL','--ignore-unfixed=false','--ignorefile','/dev/null','--list-all-pkgs','--exit-code','1','--format','json','--output','/reports/'+name+'.json']
    if archive:args.extend(['--input','/reports/'+archive.name])
    else:args.extend(['--image-src','remote','--platform','linux/amd64',ref])
    process=run(args,accepted=(0,1),timeout=1000)
    rc=process.returncode
    # Trivy uses exit 1 for both findings and fatal input errors. A missing report
    # must remain an execution failure, never an empty vulnerability result.
    write(output/(name+'-scanner-execution.json'),{
        'exit_code':rc,'stderr_bytes':len(process.stderr),
        'stderr_sha256':hashlib.sha256(process.stderr).hexdigest(),
        'archive_format':'docker-save' if archive else None,
        'report_created':(output/(name+'.json')).is_file()})
    if not (output/(name+'.json')).is_file():
        # These hosted scans have only public image refs and no credentials.
        # Bound diagnostics and remove URL authentication/query material.
        diagnostic=process.stderr.decode(errors='replace')[-12000:]
        diagnostic=re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@',r'\1[redacted]@',diagnostic)
        diagnostic=re.sub(r'(https?://[^\s?]+)\?[^\s]+',r'\1?[redacted]',diagnostic)
        (output/(name+'-scanner-error.txt')).write_text(diagnostic)
        raise ValueError('scanner_report_missing_fatal_exit_'+str(rc))
    report=json.loads((output/(name+'.json')).read_text())
    require(report.get('Trivy',{}).get('Version')==spec['scanner_version'],'scanner_version')
    cfg=report.get('Metadata',{}).get('ImageConfig',{})
    require(cfg.get('architecture')=='amd64' and cfg.get('os')=='linux','scan_platform')
    require(report.get('Results'),'scan_results_missing')
    if archive: require(not report.get('Metadata',{}).get('OS',{}).get('EOSL',False),'candidate_distribution_eosl')
    if archive:
        require(report['Metadata']['ImageID'] in {identity['index_digest'],identity['manifest_digest'],identity['config_digest']},'archive_scan_identity')
        require(cfg.get('rootfs')==identity['rootfs'],'archive_scan_rootfs_identity')
        actual=cfg.get('config',{});expected=identity['runtime_config']
        fields=['User','Env','Entrypoint','Cmd','WorkingDir','Labels','ExposedPorts','Volumes','StopSignal','Healthcheck','Shell']
        require(all((actual.get(k) or None)==(expected.get(k) or None) for k in fields),'archive_scan_runtime_config_identity')
    else:require(report.get('ArtifactName')==ref and ref in report.get('Metadata',{}).get('RepoDigests',[]),'registry_scan_identity')
    data=findings(report)
    require(rc==(1 if sum(data['counts'].values()) else 0),'strict_exit_count_mismatch')
    write(output/(name+'-modules.json'),data['packages'])
    return {k:v for k,v in data.items() if k!='packages'} | {'scanner_exit_code':rc,'report_sha256':sha(output/(name+'.json'))}


def fixture(row, before, candidate, output):
    path=ROOT/'security/infrastructure/fixtures.py'
    module_spec=importlib.util.spec_from_file_location('infra_candidate_fixtures',path)
    module=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(module)
    return module.check(row['service'],before,candidate,output)


def execute(spec, output, selected):
    require(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted' and sys.platform=='linux','remote_hosted_ci_only')
    require(not output.exists(),'fresh_output_required');output.mkdir(parents=True)
    cache=output/'scanner-cache';cache.mkdir();(cache/'scratch').mkdir()
    source_sha=run(['git','-C',str(ROOT),'rev-parse','HEAD']).stdout.decode().strip()
    require(re.fullmatch(r'[a-f0-9]{40}',source_sha),'source_sha')
    write(output/'input-manifest.json',spec)
    run(['docker','pull',spec['scanner']],timeout=600)
    run(['docker','run','--rm','--user',f'{os.getuid()}:{os.getgid()}','-v',str(cache)+':/cache',spec['scanner'],'image','--cache-dir','/cache','--download-db-only'],timeout=600)
    db=json.loads((cache/'db/metadata.json').read_text())
    updated=datetime.fromisoformat(db['UpdatedAt'].replace('Z','+00:00'))
    require(timedelta(0)<=datetime.now(timezone.utc)-updated<=timedelta(hours=48),'fresh_scanner_database_required')
    db_hash=sha(cache/'db/trivy.db');write(output/'scanner-db-metadata.json',db|{'db_sha256':db_hash,'scanner':spec['scanner'],'version':spec['scanner_version']})
    summary={'source_sha':source_sha,'started_at':datetime.now(timezone.utc).isoformat(),'services':{},'strict_policy':'HIGH=0 and CRITICAL=0 including unfixed; no ignore file','actual_gcp_changes':0,'production_data_used':False,'security_approved':False}
    for row in spec['services']:
        service=row['service']
        if service not in selected:continue
        directory=output/service;directory.mkdir();item={'status':'INCOMPLETE','current':row['current'],'candidate_selector':row['candidate_selector'],'kind':row['kind']}
        try:
            baseline=scan(spec,directory,cache,'current',ref=row['current'])
            item['current_scan']=baseline
            if row['build']=='preserve':
                item.update(candidate_reference=row['current'],candidate_scan=baseline,fixture={'status':'UNCHANGED_EXACT_IMAGE','reason':'Redis exact image preserved; new restore test not claimed'},status='PASS' if not sum(baseline['counts'].values()) else 'BLOCK_FINDINGS')
            else:
                base=resolve(row['candidate_selector'],directory/'candidate-resolution.json')
                candidate,identity=build_candidate(row,base,directory,source_sha)
                updated=scan(spec,directory,cache,'candidate',archive=directory/'loaded-runtime.docker.tar',identity=identity)
                (directory/'loaded-runtime.docker.tar').unlink()  # Exact OCI remains preserved.
                item.update(candidate_base=base,candidate_identity=identity,candidate_scan=updated)
                key=lambda r:json.dumps(r,sort_keys=True)
                old={key(x) for x in baseline['findings']};new={key(x) for x in updated['findings']}
                write(directory/'vulnerability-diff.json',{'removed':[json.loads(x) for x in sorted(old-new)],'added':[json.loads(x) for x in sorted(new-old)],'unchanged_records':len(old&new),'comparison_db_sha256':db_hash})
                run(['docker','pull','--platform','linux/amd64',row['current']],timeout=600)
                item['fixture']=fixture(row,row['current'],candidate,directory)
                item['status']='PASS' if item['fixture']['status']=='PASS' and not sum(updated['counts'].values()) else 'BLOCK_FINDINGS_OR_COMPATIBILITY'
        except Exception as error:
            item['error_type']=type(error).__name__;item['failure_code']=str(error) if isinstance(error,ValueError) else 'inspection_or_fixture_failed'
        finally:
            require(sha(cache/'db/trivy.db')==db_hash,'scanner_database_changed_between_pair')
            summary['services'][service]=item;write(directory/'result.json',item)
            print(json.dumps({'service':service,'status':item['status']}),flush=True)
    summary['complete']=len(summary['services'])==len(selected)
    summary['strict_candidate_gate']=summary['complete'] and all(r['status']=='PASS' for r in summary['services'].values())
    summary['all_nine_evaluated']=set(summary['services'])==SERVICES
    summary['completed_at']=datetime.now(timezone.utc).isoformat();write(output/'summary.json',summary)
    hashes={str(p.relative_to(output)):sha(p) for p in output.rglob('*') if p.is_file() and cache not in p.parents}
    write(output/'SHA256SUMS.json',hashes)
    return 0 if summary['strict_candidate_gate'] else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',action='store_true');parser.add_argument('--services',default='all');parser.add_argument('--output',type=Path,default=ROOT/'candidate-results')
    args=parser.parse_args();spec=load_spec();selected=SERVICES if args.services=='all' else set(args.services.split(','));require(selected and selected<=SERVICES,'service_allowlist')
    if not args.run:
        print(json.dumps({'status':'PLAN_ONLY','services':[r for r in spec['services'] if r['service'] in selected],'docker_or_network_calls':0,'remote_ci_required':True,'publish_images':False}));return 0
    return execute(spec,args.output.resolve(),selected)


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'status':'INCOMPLETE','error_type':type(error).__name__,'raw_command_output_suppressed':True}),file=sys.stderr);raise SystemExit(2)
