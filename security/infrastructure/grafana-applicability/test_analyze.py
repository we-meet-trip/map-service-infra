import concurrent.futures,io,threading,time,unittest.mock
import importlib.util,json,pathlib,tempfile,unittest,hashlib,urllib.request,urllib.error
SPEC=importlib.util.spec_from_file_location('applicability',pathlib.Path(__file__).with_name('analyze.py'));m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m)
class EvidenceBoundaryTests(unittest.TestCase):
 def test_report_decoder_keeps_zero_exit_findings(self):
  # govulncheck JSON exits0 even for vulnerability findings; data decides count.
  with tempfile.TemporaryDirectory() as tmp:
   p=pathlib.Path(tmp)/'scan';p.write_text('{"config":{"scanMode":"binary"}}\n{"finding":{"osv":"GO-2026-6303","trace":[{"function":"NewServerConn"}]}}')
   rows=m.messages(p);self.assertEqual(len([r for r in rows if 'finding' in r]),1)
 def test_malformed_or_empty_output_is_not_zero_findings(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=pathlib.Path(tmp)/'scan'
   for s in ('','{broken'):
    p.write_text(s)
    with self.assertRaises(ValueError):m.messages(p)
 def test_path_escape_and_selected_whiteouts_fail(self):
  for name in ('../grafana','/usr/share/grafana/bin/grafana','x/../../y'):
   with self.assertRaises(ValueError):m.normalized(name)
  for name in ('usr/share/grafana/bin/.wh.grafana','usr/share/grafana/.wh.data','usr/share/grafana/data/.wh..wh..opq','usr/share/grafana/data/plugins-bundled/prometheus/.wh.MANIFEST.txt'):
   self.assertTrue(m.whiteout_affects_selected(name),name)
  self.assertFalse(m.whiteout_affects_selected('var/cache/.wh.apk'))
 def test_extraction_does_not_select_foreign_platform_or_extra_plugin(self):
  self.assertTrue(m.selected(m.CORE))
  self.assertTrue(m.selected(m.PREFIX+'prometheus/gpx_grafana-prometheus-datasource_linux_amd64'))
  for name in (m.PREFIX+'prometheus/gpx_grafana-prometheus-datasource_windows_amd64',m.PREFIX+'prometheus/gpx_grafana-prometheus-datasource_linux_arm64',m.PREFIX+'unknown/plugin.json',m.PREFIX+'prometheus/node_modules/evil'):
   self.assertFalse(m.selected(name),name)
 def test_frozen_database_supports_official_head_probe_and_refuses_miss(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);path='/index/modules.json.gz';body=b'fixed-public-db-bytes'
   (root/'index').mkdir();(root/path.lstrip('/')).write_bytes(body)
   db=m.FrozenDatabase(root);db.rows[path]={'sha256':hashlib.sha256(body).hexdigest(),'headers':{'Content-Type':'application/octet-stream'},'bytes':len(body)};db.frozen=True
   url=db.start()
   try:
    with urllib.request.urlopen(urllib.request.Request(url+path,method='HEAD'),timeout=2) as response:
     self.assertEqual(response.status,200);self.assertEqual(response.read(),b'');self.assertEqual(int(response.headers['Content-Length']),len(body))
    with urllib.request.urlopen(url+path,timeout=2) as response:self.assertEqual(response.read(),body)
    with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(url+'/ID/GO-2026-6303.json.gz',timeout=2)
    self.assertEqual(error.exception.code,409);self.assertEqual(db.errors[0]['reason'],'frozen_miss')
   finally:db.close()
 def test_concurrent_requests_and_duplicate_keys_keep_one_frozen_value(self):
  # Reproduce the client's nested parallel fetches using a real local HTTP server.
  # A sequential HTTPServer never reaches this four-request barrier.
  with tempfile.TemporaryDirectory() as tmp:
   db=m.FrozenDatabase(pathlib.Path(tmp));url=db.start();barrier=threading.Barrier(4);counts={};lock=threading.Lock();real_urlopen=urllib.request.urlopen
   def fake_upstream(request,*args,**kwargs):
    if not isinstance(request,str) or not request.startswith('https://vuln.go.dev'):return real_urlopen(request,*args,**kwargs)
    with lock:counts[request]=counts.get(request,0)+1
    barrier.wait(timeout=3)
    body=io.BytesIO(request.encode());body.headers={'Content-Type':'application/json'};return body
   try:
    paths=['/ID/GO-2026-'+str(1000+i)+'.json.gz' for i in range(4)]
    def fetch(path):
     with real_urlopen(url+path,timeout=5) as response:return response.read()
    with unittest.mock.patch.object(m.urllib.request,'urlopen',side_effect=fake_upstream),concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
     results=list(pool.map(fetch,paths+paths))
    self.assertEqual(results,[('https://vuln.go.dev'+p).encode() for p in paths+paths]);self.assertEqual(list(counts.values()),[1]*4)
    db.frozen=True
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:self.assertEqual(list(pool.map(fetch,paths+paths)),results)
    self.assertEqual(db.errors,[])
   finally:db.close()
 def test_stripped_symbol_fallback_is_explicit_not_function_evidence(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=pathlib.Path(tmp)/'extract'
   for symbols,expected in (([],True),([{'Pkg':'main','Name':'main'}],False)):
    p.write_text(json.dumps({'name':'govulncheck-extract','version':'0.1.0'})+'\n'+json.dumps({'goos':'linux','goarch':'amd64','pkgSymbols':symbols}))
    self.assertEqual(m.extraction_precision(p)['module_fallback'],expected)
   p.write_text('{}\n{}')
   with self.assertRaises(ValueError):m.extraction_precision(p)
 def test_module_and_symbol_metadata_are_not_interchangeable(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=pathlib.Path(tmp)/'report';p.write_text(json.dumps({'finding':{'osv':'GO-test','trace':[{'module':'x','version':'v1'}]}}))
   f=m.messages(p)[0]['finding'];self.assertNotIn('function',f['trace'][0])
if __name__=='__main__':unittest.main()
