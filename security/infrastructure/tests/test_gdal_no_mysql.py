"""Driver/dependency removal safety and actual fixture answer validation, offline."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import tarfile
import io

ROOT = Path(__file__).resolve().parents[3]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

g = load('gdal_builder_contract', ROOT / 'security/infrastructure/gdal-no-mysql/build.py')
p = load('gdal_raster_fixture', ROOT / 'security/infrastructure/fixtures/postgres_restore.py')
s = load('gdal_scanner', ROOT / 'scripts/scan-infrastructure-candidates.py')


def raster_answer():
    return {'gdal_version': 'GDAL 3.13.2', 'formats': [
        dict(format=fmt, encoded_bytes=500, width=2, height=2, bands=1, count=4,
             pixel_min=128, pixel_max=128, srid=4326, upper_left_x=0, upper_left_y=0,
             scale_x=0.5, scale_y=-0.5) for fmt in ('GTiff', 'JPEG', 'PNG')]}


class GdalSafetyTests(unittest.TestCase):
    def test_dependent_option_internal_off_requires_no_mysql_target(self):
        for kind in ('BOOL', 'INTERNAL'):
            cache='GDAL_USE_MYSQL:BOOL=OFF\nOGR_ENABLE_DRIVER_MYSQL:'+kind+'=OFF\n'
            self.assertEqual(g.validate_mysql_configuration(cache, 'ogr_PG: phony')['OGR_ENABLE_DRIVER_MYSQL']['value'], 'OFF')
            for bad_cache, targets in ((cache.replace('=OFF','=ON',1),''),
                                      (cache.replace(kind, 'UNINITIALIZED'),''),
                                      (cache,'ogr_MySQL: phony'),
                                      (cache,'ogr/ogrsf_frmts/mysql/driver.o: CXX_COMPILER')):
                with self.subTest(kind=kind, targets=targets), self.assertRaises(ValueError):
                    g.validate_mysql_configuration(bad_cache, targets)

    def test_normal_host_cannot_execute_builder_or_download(self):
        with patch.dict(g.os.environ, {}, clear=True), patch.object(g.subprocess, 'run') as run, patch.object(g, 'download') as download:
            with self.assertRaisesRegex(ValueError, 'disposable_linux_builder_only'):
                g.build('a' * 40)
            run.assert_not_called(); download.assert_not_called()

    def test_driver_delta_allows_only_mysql_and_rejects_other_losses_or_expansion(self):
        before = g.REQUIRED_DRIVERS | {'MySQL', 'PDF'}
        self.assertEqual(g.verify_driver_delta(before, before-{'MySQL'})['removed'], ['MySQL'])
        for after in (before, before-{'MySQL','GTiff'}, before-{'MySQL'}|{'Unexpected'}):
            with self.subTest(after=after), self.assertRaises(ValueError):
                g.verify_driver_delta(before, after)

    def test_parser_preserves_multiword_driver_names_and_rejects_truncated_inventory(self):
        names = g.REQUIRED_DRIVERS | {'MySQL'} | {'Synthetic'+str(i) for i in range(50)}
        raw = 'Supported Formats:\n' + '\n'.join('  '+n+' -raster,vector- (rw+vs): Description' for n in names)
        self.assertEqual(g.drivers(raw), names)
        with self.assertRaisesRegex(ValueError, 'gdal_driver_inventory_incomplete'):
            g.drivers('  GTiff -raster- (rw+vs): GeoTIFF')

    def test_source_and_test_paths_reject_escape_links_duplicates(self):
        for path in ('', '.', '/tmp/a', '../x', 'ok/../x', 'x\\y', 'x\ny'):
            with self.subTest(path=path), self.assertRaises(ValueError): g.relative(path)
        for bad in ('../escape', 'gdalautotest-3.13.2/../escape'):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); source=root/'source.tar'; tests=root/'tests.zip'
                with tarfile.open(source,'w') as archive:
                    entry=tarfile.TarInfo('gdal-3.13.2/README');entry.size=1;archive.addfile(entry,io.BytesIO(b'x'))
                with zipfile.ZipFile(tests,'w') as archive:
                    archive.writestr('gdalautotest-3.13.2/cpp/CMakeLists.txt','x'); archive.writestr(bad,'x')
                with self.assertRaises(ValueError): g.unpack(source,tests,root)

    def test_new_variant_is_explicit_postgres_only_and_default_is_preserved(self):
        old = s.load_spec(); new = s.load_spec('trixie-no-mysql')
        for a,b in zip(old['services'],new['services']):
            if a['service']=='postgres':
                self.assertEqual(a['build'],'postgres-debian');self.assertEqual(b['build'],'postgres-trixie-no-mysql')
                self.assertEqual(b['candidate_selector'],s.POSTGRES_TRIXIE_BASE)
            else:self.assertEqual(a,b)
        with patch('sys.argv',['scan','--run','--postgres-variant','trixie-no-mysql']), patch.object(s,'execute') as execute:
            with self.assertRaisesRegex(ValueError,'trixie_comparison_postgres_only'):s.main()
            execute.assert_not_called()

    def test_recipe_preserves_gis_and_package_runtime_contracts(self):
        text=(ROOT/'security/infrastructure/Dockerfile.postgres-trixie-no-mysql').read_text()
        for run in [x[4:] for x in text.replace('\\\n','').splitlines() if x.startswith('RUN ')]:
            result=subprocess.run(['/bin/sh','-n','-c',run],capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--with-raster --with-topology --with-protobuf --with-address-standardizer',text)
        self.assertIn('--with-sfcgal=/usr/bin/sfcgal-config',text)
        self.assertIn('--with-gdalconfig=/usr/local/bin/gdal-config',text)
        self.assertIn('apt-mark hold libgdal39',text)
        self.assertIn('unexpected_mysql_runtime_package',text)
        self.assertIn('--no-install-recommends --no-remove "$@"',text)
        self.assertNotIn('--allow-',text)


class RasterAnswersTests(unittest.TestCase):
    def test_lossless_pixel_georeference_and_explicit_jpeg_tolerance(self):
        answer=raster_answer();p.validate_raster(answer)
        answer['formats'][1]['pixel_min']=127;p.validate_raster(answer)

    def test_missing_format_empty_bytes_corrupted_pixels_or_georeference_fail(self):
        cases=[]
        answer=raster_answer();answer['formats'].pop();cases.append(answer)
        for key,value in [('encoded_bytes',0),('width',True),('count',3),('pixel_min',0),('pixel_max',float('nan')),('srid',0),('scale_y',0.5)]:
            answer=raster_answer();answer['formats'][0][key]=value;cases.append(answer)
        answer=raster_answer();answer['formats'][1]['pixel_min']=126;cases.append(answer)
        for answer in cases:
            with self.subTest(answer=answer),self.assertRaises(ValueError):p.validate_raster(answer)

if __name__=='__main__': unittest.main()
