import importlib.util,json,pathlib,tempfile,unittest
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
 def test_module_and_symbol_metadata_are_not_interchangeable(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=pathlib.Path(tmp)/'report';p.write_text(json.dumps({'finding':{'osv':'GO-test','trace':[{'module':'x','version':'v1'}]}}))
   f=m.messages(p)[0]['finding'];self.assertNotIn('function',f['trace'][0])
if __name__=='__main__':unittest.main()
