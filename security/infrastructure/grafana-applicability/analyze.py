#!/usr/bin/env python3
"""Exact, non-executing binary applicability evidence; never overrides strict Trivy gate."""
import argparse,collections,concurrent.futures,datetime,gzip,hashlib,http.server,importlib.util,io,json,os,pathlib,re,shutil,subprocess,tarfile,threading,time,urllib.request,zipfile
ROOT=pathlib.Path(__file__).resolve().parents[3]
SOURCE='11e4c140c291536dd24cd6860e753c3165120422'
ARTIFACT=10009460195
WRAPPER_SHA='b2b4184fceb80e7605c2c1e6e6abc5db5ab8c1b3280c885b41c6500c6440c1d9'
OCI_SHA='f85f296f1187050a04e24e7d822ee2a9dbfca194efb4fd5495ff229aa44391c0'
CONFIG='sha256:03600ad4d146bf9211b7958dc677daa86cc0bf26b290576bc48adba8c29416ad'
MANIFEST='sha256:13e0a7fdb65cc9f18be74fefeebaacb9b46bfb736081194a4cf285651e0621bc'
RAW_SHA='87ddbe0a47d9aafe358bd6b7c9605473c491cb1439169894f9079b6cc722bbf3'
PLUGINS=set(json.loads((ROOT/'security/infrastructure/grafana-core-security/pins.json').read_text())['preserved_catalog_plugin_ids'])
CORE='usr/share/grafana/bin/grafana'
PREFIX='usr/share/grafana/data/plugins-bundled/'
FILES=['grafana/candidate.oci.tar','grafana/candidate.json','grafana/candidate-identity.json','grafana/compatibility/result.json','grafana/build-evidence/build-receipt.json','grafana/build-evidence/preserved-files-before.sha256','grafana/build-evidence/preserved-files-after.sha256']

def require(value,code):
 if not value:raise ValueError(code)
def sha(path):
 h=hashlib.sha256()
 with pathlib.Path(path).open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def write(path,obj):
 pathlib.Path(path).parent.mkdir(parents=True,exist_ok=True);pathlib.Path(path).write_text(json.dumps(obj,indent=2))
def command(args,directory,name,timeout=300,accepted=(0,)):
 directory.mkdir(parents=True,exist_ok=True);start=time.monotonic()
 env={k:v for k,v in os.environ.items() if k not in ('GH_TOKEN','GITHUB_TOKEN') or args[0]=='gh'}
 with (directory/(name+'.stdout')).open('wb') as out,(directory/(name+'.stderr')).open('wb') as err:
  p=subprocess.run(args,stdout=out,stderr=err,timeout=timeout,env=env)
 receipt={'argv':args,'exit_code':p.returncode,'elapsed_seconds':round(time.monotonic()-start,3),'stdout_sha256':sha(directory/(name+'.stdout')),'stderr_sha256':sha(directory/(name+'.stderr'))}
 write(directory/(name+'.command.json'),receipt);require(p.returncode in accepted,'command_failed:'+name)
 return directory/(name+'.stdout')
def selected(name):
 if name==CORE:return True
 if not name.startswith(PREFIX):return False
 tail=name[len(PREFIX):].split('/')
 return len(tail)==2 and tail[0] in PLUGINS and (tail[1] in ('plugin.json','MANIFEST.txt') or re.fullmatch(r'gpx_[A-Za-z0-9_-]+_linux_amd64',tail[1]))
def normalized(name):
 p=pathlib.PurePosixPath(name);require(not p.is_absolute() and '..' not in p.parts,'unsafe_layer_path')
 return str(p)
def whiteout_affects_selected(name):
 p=pathlib.PurePosixPath(name)
 if not p.name.startswith('.wh.'):return False
 target=str(p.parent/(p.name[4:] if p.name!='.wh..wh..opq' else ''))
 return selected(target) or CORE.startswith(target.rstrip('/')+'/') or PREFIX.startswith(target.rstrip('/')+'/') or target.startswith(PREFIX)
class HashedReader:
 def __init__(self,base):self.base=base;self.digest=hashlib.sha256()
 def read(self,n=-1):
  data=self.base.read(n);self.digest.update(data);return data

def extract_binaries(oci,destination):
 # Reuse the existing exact OCI metadata validator. No Docker and no target execution.
 spec=importlib.util.spec_from_file_location('map_infra_scan',ROOT/'scripts/scan-infrastructure-candidates.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
 identity=mod.oci_identity(oci);require(identity['manifest_digest']==MANIFEST and identity['config_digest']==CONFIG and identity['source_sha']==SOURCE,'candidate_oci_binding')
 require(identity['archive_sha256']==OCI_SHA,'candidate_oci_checksum')
 receipts=[];members_count=0;expanded=0
 with tarfile.open(oci,'r:') as outer:
  names=[m.name for m in outer];require(len(names)==len(set(names)),'duplicate_oci_member')
  def blob(digest):return outer.extractfile('blobs/sha256/'+digest.split(':')[1])
  manifest=json.load(blob(MANIFEST));config=json.load(blob(CONFIG));require(len(manifest['layers'])==len(config['rootfs']['diff_ids']),'layer_diffid_count')
  for descriptor,diffid in zip(manifest['layers'],config['rootfs']['diff_ids']):
   with blob(descriptor['digest']) as f:
    h=hashlib.sha256()
    for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
   require('sha256:'+h.hexdigest()==descriptor['digest'],'compressed_layer_checksum')
   stream=blob(descriptor['digest']);media=descriptor['mediaType']
   require(media in ('application/vnd.oci.image.layer.v1.tar+gzip','application/vnd.oci.image.layer.v1.tar','application/vnd.docker.image.rootfs.diff.tar.gzip'),'layer_media_type')
   decoded=gzip.GzipFile(fileobj=stream) if media.endswith('gzip') else stream;reader=HashedReader(decoded)
   with tarfile.open(fileobj=reader,mode='r|') as layer:
    for member in layer:
     members_count+=1;expanded+=member.size
     require(members_count<=200000 and expanded<=8*1024**3,'bounded_layers')
     name=normalized(member.name)
     require(not whiteout_affects_selected(name),'selected_file_whiteout')
     if not selected(name):continue
     require(member.isfile() and 0<member.size<=512*1024**2,'selected_regular_bounded_file')
     target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
     with layer.extractfile(member) as src,target.open('wb') as out:shutil.copyfileobj(src,out,1024*1024)
     target.chmod(0o600) # Analysis input only: never execute target binaries.
   while reader.read(1024*1024):pass
   require('sha256:'+reader.digest.hexdigest()==diffid,'uncompressed_diffid')
   decoded.close();stream.close();receipts.append({'digest':descriptor['digest'],'diff_id':diffid,'checksums_verified':True})
 write(destination.parent/'oci-layer-verification.json',{'identity':identity,'layers':receipts,'members':members_count,'expanded_bytes':expanded,'extraction_only':True})
 return identity

class FrozenDatabase:
 """Populate during module scans; replay the identical HTTP bytes for symbol scans."""
 def __init__(self,directory):
  self.directory=directory;self.rows={};self.frozen=False;self.errors=[];self.lock=threading.Lock();self.path_locks={}
 def cached(self,path):
  # govulncheck requests up to 10 modules x 10 IDs concurrently. Serialize only
  # identical keys; a global fetch lock would starve the client's TCP backlog.
  with self.lock:lock=self.path_locks.setdefault(path,threading.Lock())
  with lock:
   if path not in self.rows:
    if self.frozen:self.errors.append({'path':path,'reason':'frozen_miss'});return None
    try:
     with urllib.request.urlopen('https://vuln.go.dev'+path,timeout=30) as r:
      body=r.read(32*1024**2);headers={k:v for k,v in r.headers.items() if k.lower() in ('content-type','content-encoding','last-modified')}
      require(len(body)<32*1024**2,'bounded_go_db')
     target=self.directory/path.lstrip('/');target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(body)
     self.rows[path]={'sha256':hashlib.sha256(body).hexdigest(),'headers':headers,'bytes':len(body)}
    except Exception as e:self.errors.append({'path':path,'error_type':type(e).__name__});raise
   row=self.rows[path];body=(self.directory/path.lstrip('/')).read_bytes();require(hashlib.sha256(body).hexdigest()==row['sha256'],'cached_db_checksum')
   return row,body
 def start(self):
  owner=self
  class Handler(http.server.BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_HEAD(self):self.respond(head=True)
   def do_GET(self):self.respond(head=False)
   def respond(self,head):
    path=self.path
    if not re.fullmatch(r'/(?:index/[A-Za-z0-9_-]+|ID/GO-\d{4}-\d+)\.json(?:\.gz)?',path):self.send_error(400);return
    try:cached=owner.cached(path)
    except Exception:self.send_error(502);return
    if cached is None:self.send_error(409);return
    row,body=cached
    self.send_response(200)
    for k,v in row['headers'].items():self.send_header(k,v)
    self.send_header('Content-Length',str(len(body)));self.end_headers()
    if not head:self.wfile.write(body)
  class PoolServer(http.server.HTTPServer):
   request_queue_size=128
   def __init__(self,*args):
    self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=16);super().__init__(*args)
   def process_request(self,request,client_address):self.pool.submit(self.respond,request,client_address)
   def respond(self,request,client_address):
    try:self.finish_request(request,client_address)
    except Exception:self.handle_error(request,client_address)
    finally:self.shutdown_request(request)
   def server_close(self):
    super().server_close();self.pool.shutdown(wait=True)
  self.server=PoolServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();return 'http://127.0.0.1:'+str(self.server.server_port)
 def close(self):
  self.server.shutdown();self.thread.join();self.server.server_close();write(self.directory/'receipt.json',{'upstream':'https://vuln.go.dev','frozen_for_all_symbol_scans':self.frozen,'responses':self.rows,'errors':self.errors})

def messages(path):
 raw=path.read_text();decoder=json.JSONDecoder();values=[]
 while raw.strip():
  raw=raw.lstrip();obj,end=decoder.raw_decode(raw);values.append(obj);raw=raw[end:]
 require(bool(values),'govulncheck_json_empty');return values

def analyze(output):
 require(os.name=='posix' and pathlib.Path('/proc/meminfo').is_file(),'hosted_linux_required')
 require(not output.exists(),'fresh_output_required');output.mkdir(parents=True)
 mem=int(re.search(r'MemAvailable:\s+(\d+)',pathlib.Path('/proc/meminfo').read_text()).group(1))*1024
 free=shutil.disk_usage(output).free;write(output/'resources-before.json',{'mem_available':mem,'disk_free':free,'analysis_parallelism':1,'GOMAXPROCS':os.environ.get('GOMAXPROCS'),'GOMEMLIMIT':os.environ.get('GOMEMLIMIT')})
 require(mem>=2*1024**3 and free>=6*1024**3,'analysis_resource_floor')
 commands=output/'commands';wrapper=output.parent/'grafana-original-actions.zip'
 command(['gh','api',f'repos/we-meet-trip/map-service-infra/actions/artifacts/{ARTIFACT}'],commands,'artifact-metadata')
 metadata=json.loads((commands/'artifact-metadata.stdout').read_text());require(metadata['id']==ARTIFACT and metadata['digest']=='sha256:'+WRAPPER_SHA and metadata['size_in_bytes']==945338216,'original_artifact_metadata')
 command(['gh','api',f'repos/we-meet-trip/map-service-infra/actions/artifacts/{ARTIFACT}/zip'],commands,'original-artifact',timeout=600)
 (commands/'original-artifact.stdout').replace(wrapper);require(sha(wrapper)==WRAPPER_SHA,'full_wrapper_checksum')
 inputs=output/'inputs';inputs.mkdir()
 with zipfile.ZipFile(wrapper) as z:
  for name in FILES:
   require(z.namelist().count(name)==1,'one_exact_zip_member')
   target=inputs/name;target.parent.mkdir(parents=True,exist_ok=True)
   with z.open(name) as src,target.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
 raw=inputs/'grafana/candidate.json';require(sha(raw)==RAW_SHA,'raw_trivy_report_checksum')
 counts=collections.Counter(v['Severity'] for r in json.loads(raw.read_text())['Results'] for v in r.get('Vulnerabilities',[]) if v['Severity'] in ('HIGH','CRITICAL'))
 require(dict(counts)=={'HIGH':155,'CRITICAL':3},'original_strict_findings')
 require(sha(inputs/'grafana/candidate.oci.tar')==OCI_SHA,'input_oci_checksum')
 extraction=output.parent/'grafana-extracted';require(not extraction.exists(),'fresh_extraction_required');extract_binaries(inputs/'grafana/candidate.oci.tar',extraction)
 (extraction.parent/'oci-layer-verification.json').replace(output/'oci-layer-verification.json')
 before=(inputs/'grafana/build-evidence/preserved-files-before.sha256').read_bytes();after=(inputs/'grafana/build-evidence/preserved-files-after.sha256').read_bytes();require(before==after,'preserved_plugin_tree_receipt')
 expected={row.split(None,1)[1].lstrip('/'):row.split(None,1)[0] for row in after.decode().splitlines()}
 build=json.loads((inputs/'grafana/build-evidence/build-receipt.json').read_text());require(sha(extraction/CORE)==build['binary_sha256'],'core_binary_sha')
 binaries={'core':extraction/CORE};inventory=[]
 for plugin in sorted(PLUGINS):
  directory=extraction/PREFIX/plugin;data=json.loads((directory/'plugin.json').read_text());require(data['id']==plugin and data.get('backend'),'backend_plugin_identity')
  binary=directory/(data['executable']+'_linux_amd64');require(binary.is_file(),'signed_binary_present')
  for p in (binary,directory/'plugin.json',directory/'MANIFEST.txt'):require(sha(p)==expected[str(p.relative_to(extraction))],'preserved_plugin_file_sha')
  binaries[plugin]=binary;inventory.append({'id':plugin,'version':data['info']['version'],'binary_sha256':sha(binary),'manifest_sha256':sha(directory/'MANIFEST.txt'),'candidate_preserved_tree_match':True})
 require(len(binaries)==14,'core_and_13_backends');write(output/'inventory.json',inventory)
 toolmeta=command(['go','mod','download','-json','golang.org/x/vuln@v1.7.0'],commands,'govulncheck-module')
 tool=json.loads(toolmeta.read_text());require(tool['Sum']=='h1:4MQBuhmXbz2uepNJrf3v+aaZLGDqw1JluwYboegA1qg=' and tool['GoModSum']=='h1:Xw7zvU3e1bsCYYBXu+w4wcn2Kgn27f34WBCTw8LL5Us=','govulncheck_checksum_database_pins')
 command(['go','install','golang.org/x/vuln/cmd/govulncheck@v1.7.0'],commands,'build-analysis-tool',timeout=600)
 govuln=pathlib.Path(os.environ['GOPATH'])/'bin/govulncheck';command([str(govuln),'-version'],commands,'govulncheck-version');command(['go','version','-m',str(govuln)],commands,'govulncheck-buildinfo')
 db=FrozenDatabase(output/'database');url=db.start();summaries={}
 try:
  # Module pass primes every required database response before a frozen symbol pass.
  for mode in ('module','symbol'):
   if mode=='symbol':db.frozen=True
   for name,binary in binaries.items():
    target=output/'binaries'/name
    if mode=='module':
     command(['go','version','-m',str(binary)],target,'buildinfo')
     command([str(govuln),'-mode','extract',str(binary)],target,'extracted-symbol-blob')
    path=command([str(govuln),'-mode','binary','-scan',mode,'-json','-db',url,str(binary)],target,'govulncheck-'+mode,timeout=600)
    events=messages(path);require(any('config' in e for e in events),'govulncheck_config_required')
    findings=[e['finding'] for e in events if 'finding' in e];osvs={e['osv']['id']:e['osv'] for e in events if 'osv' in e}
    summaries.setdefault(name,{})[mode]={'finding_records':len(findings),'osv_ids':sorted({f['osv'] for f in findings}),'findings':findings,'config':[e['config'] for e in events if 'config' in e],'advisory_aliases':{k:v.get('aliases',[]) for k,v in osvs.items()}}
    print(json.dumps({'binary':name,'mode':mode,'finding_records':len(findings)}),flush=True)
 finally:db.close()
 require(not db.errors,'database_fetch_or_frozen_miss')
 write(output/'SUMMARY.json',{'schema':'map-grafana-binary-applicability-v1','source_candidate':SOURCE,'source_manifest':MANIFEST,'original_artifact':ARTIFACT,'original_raw_trivy_sha256':RAW_SHA,'original_strict_findings':dict(counts),'strict_gate':'FAIL_RETAINED','target_binaries_executed':False,'scope':'binary symbol presence, not source call graph or exploitability proof; JSON exit0 is execution success, not findings0','database_frozen_symbol_pass':True,'binaries':summaries})
 # Publish evidence only, not the ~1GiB wrapper/extracted rootfs/OCI.
 (inputs/'grafana/candidate.oci.tar').unlink() # Only this fresh, owned temporary copy; original Actions artifact remains.
 write(output/'SHA256SUMS.json',{str(p.relative_to(output)):sha(p) for p in output.rglob('*') if p.is_file() and p.name!='SHA256SUMS.json'})

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=pathlib.Path,required=True);args=parser.parse_args()
 try:analyze(args.output.resolve())
 except Exception as e:
  write(args.output/'FAILURE.json',{'error_type':type(e).__name__,'message':str(e)[:1000],'strict_gate':'FAIL_RETAINED'});raise
