#!/usr/bin/env python3
"""Small offline checks for archive trust boundaries and exact identities."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import scan

class Checks(unittest.TestCase):
    def tar(self,path,members):
        with tarfile.open(path,'w') as t:
            for name,raw in members.items():
                m=tarfile.TarInfo(name);m.size=len(raw);t.addfile(m,io.BytesIO(raw))
    def test_traversal_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.tar(p/'a.tar',{'../escaped':b'bad'})
            with self.assertRaisesRegex(ValueError,'unsafe tar'):scan.extract_oci(p/'a.tar',p/'out')
            self.assertFalse((p/'escaped').exists())
    def test_blob_tamper_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.tar(p/'a.tar',{'blobs/sha256/'+'0'*64:b'bad'})
            with self.assertRaisesRegex(ValueError,'SHA mismatch'):scan.extract_oci(p/'a.tar',p/'out')
    def test_exact_config_selected_from_verified_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);cfg=b'{"os":"linux","architecture":"amd64"}'
            cd=hashlib.sha256(cfg).hexdigest();manifest=json.dumps({'config':{'digest':'sha256:'+cd},'layers':[]}).encode();md=hashlib.sha256(manifest).hexdigest()
            self.tar(p/'a.tar',{'oci-layout':b'{"imageLayoutVersion":"1.0.0"}',
               'index.json':json.dumps({'manifests':[{'digest':'sha256:'+md}]}).encode(),
               'blobs/sha256/'+cd:cfg,'blobs/sha256/'+md:manifest})
            inp,n=scan.extract_oci(p/'a.tar',p/'out');self.assertEqual(n,2)
            self.assertEqual(scan.layout_config(inp,'example@sha256:'+md),'sha256:'+cd)
            with self.assertRaisesRegex(ValueError,'pinned reference blob absent'):
                scan.layout_config(inp,'example@sha256:'+'0'*64)
    def test_classic_docker_save_supported(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);cfg=b'{"os":"linux","architecture":"amd64"}';cd=hashlib.sha256(cfg).hexdigest()
            self.tar(p/'a.tar',{'manifest.json':json.dumps([{'Config':cd+'.json','Layers':[]}]).encode(),cd+'.json':cfg})
            inp,n=scan.extract_oci(p/'a.tar',p/'out');self.assertEqual(inp,p/'a.tar')
            self.assertEqual(scan.layout_config(p/'out'),'sha256:'+cd)
    def test_remote_reader_bounds_and_chunking(self):
        r=scan.RemoteZip({'size_in_bytes':10*1024*1024,'id':1});calls=[]
        def chunk(start,n):calls.append((start,n));return b'x'*n
        r.chunk=chunk;self.assertEqual(len(r.read(9*1024*1024)),9*1024*1024)
        self.assertEqual(len(calls),3);self.assertLessEqual(max(n for _,n in calls),4*1024*1024)
        with self.assertRaises(ValueError):r.seek(-1)
    def test_ignored_http_range_refused_after_bounded_read(self):
        r=scan.RemoteZip({'size_in_bytes':100000,'id':1})
        process=mock.MagicMock();process.stdout.read.return_value=b'x'*101
        process.poll.return_value=0
        with mock.patch.object(scan.subprocess,'Popen') as popen:
            popen.return_value.__enter__.return_value=process
            with self.assertRaisesRegex(ValueError,'range ignored/truncated'):r.chunk(0,100)
        process.stdout.read.assert_called_once_with(101)
        process.terminate.assert_called_once()
    def test_plan_exact_inputs_and_frozen_limits(self):
        p=scan.read(scan.HERE/'plan.json');self.assertEqual(len(p['targets']),9)
        self.assertEqual({r['service'] for r in p['targets']},{'proxy','dns','redis-exporter','prometheus','node-exporter','postgres-exporter','redis','osrm','caddy'})
        for row in p['targets']:
            if row['kind']=='registry_exact':self.assertRegex(row['reference'],r'@sha256:[0-9a-f]{64}$')
            else:self.assertRegex(row['sha256'],r'^[0-9a-f]{64}$')

if __name__=='__main__':unittest.main(verbosity=2)
