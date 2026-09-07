"""Offline contracts for a separate, untested Trixie alternative."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / 'postgres-trixie'
DOCKERFILE = ROOT / 'Dockerfile.postgres-trixie'


def digest(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


class PostgresTrixieContractTests(unittest.TestCase):
    def test_immutable_index_child_and_config_bytes_are_bound(self):
        proof = json.loads((PROOF / 'registry-proof.json').read_text())
        index = json.loads((PROOF / 'index.json').read_text())
        manifest = json.loads((PROOF / 'manifest.json').read_text())
        config = json.loads((PROOF / 'config.json').read_text())
        self.assertEqual(digest(PROOF / 'index.json'), proof['index_digest'])
        self.assertEqual(digest(PROOF / 'manifest.json'), proof['manifest_digest'])
        self.assertEqual(digest(PROOF / 'config.json'), proof['config_digest'])
        children = [row for row in index['manifests']
                    if row['platform'] == {'architecture': 'amd64', 'os': 'linux'}]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['digest'], proof['manifest_digest'])
        self.assertEqual(manifest['config']['digest'], proof['config_digest'])
        self.assertEqual((config['architecture'], config['os']), ('amd64', 'linux'))
        self.assertIn('postgres@' + proof['manifest_digest'], DOCKERFILE.read_text())

    def test_base_runtime_and_observed_small_layer_contracts(self):
        proof = json.loads((PROOF / 'registry-proof.json').read_text())
        config = json.loads((PROOF / 'config.json').read_text())['config']
        env = dict(row.split('=', 1) for row in config['Env'])
        self.assertEqual(env['PG_VERSION'], '17.11-1.pgdg13+2')
        self.assertEqual(env['PG_MAJOR'], '17')
        self.assertEqual(env['LANG'], 'en_US.utf8')
        self.assertEqual(env['PGDATA'], '/var/lib/postgresql/data')
        self.assertEqual(config['Entrypoint'], ['docker-entrypoint.sh'])
        self.assertEqual(config['Cmd'], ['postgres'])
        self.assertEqual(config['StopSignal'], 'SIGINT')
        self.assertEqual(config['Volumes'], {'/var/lib/postgresql/data': {}})
        layers = {row['digest'] for row in json.loads((PROOF / 'manifest.json').read_text())['layers']}
        observations = {row['path']: row for row in proof['bounded_layer_observations']}
        self.assertTrue(all(row['layer'] in layers for row in observations.values()))
        self.assertEqual(observations['var/lib/postgresql/data']['uid'], 999)
        self.assertEqual(observations['var/lib/postgresql/data']['gid'], 999)
        self.assertEqual(observations['usr/local/bin/gosu']['sha256'],
                         '52c8749d0142edd234e9d6bd5237dff2d81e71f43537e2f4f66f75dd4b243dd0')
        self.assertFalse(proof['complete_rootfs_reconstructed'])
        self.assertFalse(proof['image_or_binary_execution'])

    def test_reviewed_gosu_builder_is_preserved_byte_for_byte(self):
        text = DOCKERFILE.read_text()
        proof = json.loads((PROOF / 'registry-proof.json').read_text())
        stage = text[text.index('FROM golang@'):text.index('FROM ${BASE} AS common')]
        self.assertEqual(hashlib.sha256(stage.encode()).hexdigest(),
                         proof['preserved_gosu_builder_stage_sha256'])
        self.assertIn('COPY --from=gosu_builder --chown=0:0 --chmod=0755 /out/gosu /usr/local/bin/gosu', text)
        self.assertIn("'1.19 (go1.26.8 on linux/amd64; gc)'", text)

    def test_official_package_metadata_covers_runtime_names_and_exact_pg_dev(self):
        data = json.loads((PROOF / 'package-dependencies.json').read_text())
        indices = data['indices']
        for index in indices.values():
            release = index['release_file']
            # The two independent collectors named the same check differently.
            self.assertIs(release.get('package_hash_and_size_match',
                                      release.get('index_sha256_and_size_match')), True)
            self.assertTrue(release['signature_not_verified_in_this_readonly_review'])
            if 'package_entry' in release:
                self.assertEqual(release['package_entry'][:2],
                                 [index['compressed_sha256'], str(index['compressed_bytes'])])
        pgdg = indices['pgdg']['selected_packages']
        main = indices['debian-main']['selected_packages']
        self.assertTrue(any(row['Package'] == 'postgresql-server-dev-17'
                            and row['Version'] == '17.11-1.pgdg13+2' for row in pgdg))
        for packages, dev, runtime in [
            (pgdg, 'libgdal-dev', 'libgdal39'), (pgdg, 'libgeos-dev', 'libgeos-c1t64'),
            (pgdg, 'libproj-dev', 'libproj25'), (main, 'libsfcgal-dev', 'libsfcgal2'),
            (main, 'libprotobuf-c-dev', 'libprotobuf-c1'), (main, 'libjson-c-dev', 'libjson-c5'),
            (main, 'libpcre2-dev', 'libpcre2-8-0'), (main, 'libxml2-dev', 'libxml2'),
        ]:
            with self.subTest(dev=dev):
                self.assertTrue(any(row['Package'] == dev and runtime + ' (=' in row['Depends']
                                    for row in packages))
                self.assertIn(runtime, data['effective_direct_runtime_names'])
        self.assertFalse(data['actual_solver_build_fixture_scan_executed'])

    def test_actual_trixie_tuple_parser_accepts_concrete_sfcgal2_and_rejects_old_name(self):
        runs = [row[4:] for row in DOCKERFILE.read_text().replace('\\\n', '').splitlines()
                if row.startswith('RUN ')]
        parser = next(row for row in runs if 'pg_runtime_seen=' in row).split('    apt-get update;', 1)[0]
        self.assertNotIn('apt-get', parser)
        self.assertNotIn('rm -rf', parser)
        packages = ['libgdal39', 'libgeos-c1t64', 'libjson-c5', 'libpcre2-8-0',
                    'libproj25', 'libprotobuf-c1', 'libsfcgal2', 'libxml2']
        for bad in (False, True):
            with self.subTest(obsolete_sfcgal=bad), tempfile.TemporaryDirectory() as directory:
                rows = [name + '\tamd64\t1.2.3-1\tinstalled\n' for name in packages]
                if bad:
                    rows[6] = 'libsfcgal1\t\t\tnot-installed\n'
                path = Path(directory) / 'synthetic.tsv'
                path.write_text(''.join(rows))
                script = parser.replace('/usr/share/map-candidate/postgis-build-runtime-packages.tsv', str(path))
                script += '\n printf "%s\\n" "$@"\n'
                for shell in ('/bin/sh', '/bin/dash'):
                    if not Path(shell).exists():
                        continue
                    result = subprocess.run([shell, '-c', script], capture_output=True, text=True,
                                            timeout=10, env={**os.environ, 'LC_ALL': 'C'})
                    if bad:
                        self.assertEqual(result.returncode, 1)
                        self.assertIn('postgis_runtime_tuple_invalid:package', result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.splitlines(),
                                         [name + ':amd64=1.2.3-1' for name in packages])

    def test_all_actual_runs_parse_and_full_features_strict_guards_remain(self):
        text = DOCKERFILE.read_text()
        runs = [row[4:] for row in text.replace('\\\n', '').splitlines() if row.startswith('RUN ')]
        for index, run in enumerate(runs):
            for shell in ('/bin/sh', '/bin/dash'):
                if not Path(shell).exists():
                    continue
                with self.subTest(run=index, shell=shell):
                    result = subprocess.run([shell, '-n', '-c', run], capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--with-raster --with-topology --with-protobuf --with-address-standardizer', text)
        self.assertIn('--with-sfcgal=/usr/bin/sfcgal-config', text)
        self.assertIn('postgis--3.5.2--ANY.sql', text)
        self.assertIn('postgis--ANY--3.5.7.sql', text)
        self.assertIn('if ! cmp -s ', text)
        self.assertIn("! ldd \"$module\" | grep -q 'not found'", text)
        self.assertIn('--no-install-recommends --no-remove "$@"', text)
        self.assertNotIn('--allow-downgrades', text)
        self.assertNotIn('--allow-unauthenticated', text)
        self.assertTrue(text.rstrip().endswith('USER 999:999'))

    def test_prior_success_is_not_attributed_to_alternative(self):
        baseline = json.loads((PROOF / 'bookworm-grouped-baseline.json').read_text())
        proof = json.loads((PROOF / 'registry-proof.json').read_text())
        self.assertEqual(baseline['source_sha'], '11e4c140c291536dd24cd6860e753c3165120422')
        self.assertEqual(baseline['counts'], {'CRITICAL': 16, 'HIGH': 108})
        self.assertTrue(baseline['all_fixed_version_null'])
        self.assertEqual(proof['candidate_status'], 'UNTESTED_SEPARATE_ALTERNATIVE_NOT_A_SUCCESSOR')


if __name__ == '__main__':
    unittest.main()
