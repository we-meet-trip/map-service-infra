"""Exercise actual scanner evidence export with synthetic tar and Docker mocks.

For a read-only review of an integration worktree, set MAP_SCANNER_REVIEW_PATH
explicitly to its scanner file. By default this imports this repository's scanner;
no copied function or simulated implementation is used.
"""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
SCANNER_PATH = Path(os.environ.get('MAP_SCANNER_REVIEW_PATH', str(REPO / 'scripts/scan-infrastructure-candidates.py'))).resolve()
SPEC = importlib.util.spec_from_file_location('reviewed_build_evidence_scanner', SCANNER_PATH)
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)
IMAGE = 'sha256:' + '1' * 64
IDENTITY = {'runtime_image_id': IMAGE, 'config_digest': 'sha256:' + '2' * 64,
            'source_sha': '3' * 40, 'loaded_config_bytes_verified': True}


def make_tar(entries, mode='w'):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode=mode) as archive:
        for path, payload, kind in entries:
            entry = tarfile.TarInfo(path)
            entry.type = kind
            entry.mtime = 0
            if kind == tarfile.REGTYPE:
                entry.size = len(payload)
                archive.addfile(entry, io.BytesIO(payload))
            else:
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE): entry.linkname = '../outside'
                archive.addfile(entry)
    return output.getvalue()


class DockerDouble:
    """Only create/inspect/cp/rm are supported; starting any process fails."""
    def __init__(self, archive, mutate=None, missing_cleanup=False):
        self.archive, self.mutate, self.missing_cleanup = archive, mutate, missing_cleanup
        self.calls, self.inspect_count = [], 0
        self.name, self.token = None, None

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:2] == ['docker', 'create']:
            self.name = args[args.index('--name') + 1]
            self.token = args[args.index('--label') + 1].split('=', 1)[1]
            if any(flag in args for flag in ('--mount', '-v', '--volume')):
                raise AssertionError('Evidence collection must not attach volumes')
            return subprocess.CompletedProcess(args, 0, self.name.encode(), b'')
        if args[:2] == ['docker', 'inspect']:
            self.inspect_count += 1
            if self.missing_cleanup and self.inspect_count > 1:
                error = b'Cannot connect to Docker daemon' if self.missing_cleanup == 'daemon' else b'Error: No such object: ' + self.name.encode()
                return subprocess.CompletedProcess(args, 1, b'', error)
            meta = {'Image': IMAGE, 'Config': {'Labels': {'map.build.evidence': self.token}},
                    'State': {'Status': 'created', 'Running': False, 'StartedAt': '0001-01-01T00:00:00Z'}}
            if self.mutate: self.mutate(meta, self.inspect_count)
            return subprocess.CompletedProcess(args, 0, json.dumps([meta]).encode(), b'')
        if args[:2] == ['docker', 'cp']:
            return subprocess.CompletedProcess(args, 0, self.archive, b'')
        if args[:2] == ['docker', 'rm']:
            if '-f' in args: raise AssertionError('Never force-remove a running or foreign container')
            return subprocess.CompletedProcess(args, 0, self.name.encode(), b'')
        raise AssertionError('Unexpected external action: ' + ' '.join(args[:2]))

    def operations(self):
        return [args[1] for args in self.calls]


class BuildEvidenceExportTest(unittest.TestCase):
    def invoke(self, docker, output, build='go-security'):
        with patch.object(scanner, 'run', side_effect=docker.run), \
             patch.object(scanner.subprocess, 'run', side_effect=AssertionError('Actual subprocess calls forbidden')):
            scanner.export_build_evidence({'build': build}, IMAGE, deepcopy(IDENTITY), output)

    def test_receipt_binds_exact_source_config_archive_and_extracted_file_hashes(self):
        payloads = {'build-result.json': b'{"synthetic":true}\n', 'inventory/go.sum': b'public synthetic checksums\n'}
        raw = make_tar([(path, body, tarfile.REGTYPE) for path, body in payloads.items()])
        docker = DockerDouble(raw)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            self.invoke(docker, output)
            receipt = json.loads((output / 'build-evidence-receipt.json').read_text())
            self.assertEqual(receipt['runtime_image_id'], IMAGE)
            self.assertEqual(receipt['config_digest'], IDENTITY['config_digest'])
            self.assertNotEqual(receipt['runtime_image_id'], receipt['config_digest'])
            self.assertEqual(receipt['source_sha'], IDENTITY['source_sha'])
            self.assertEqual(receipt['archive_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(receipt['files'], {p: hashlib.sha256(v).hexdigest() for p, v in payloads.items()})
            for path, value in payloads.items(): self.assertEqual((output / 'build-evidence' / path).read_bytes(), value)
            self.assertIs(receipt['container_started'], False)
        self.assertEqual(docker.operations(), ['create', 'inspect', 'cp', 'inspect', 'rm'])
        self.assertIn(docker.name + ':/usr/share/map-security/go/.', next(c for c in docker.calls if c[1] == 'cp'))

    def test_source_recipe_paths_are_explicit_and_other_recipes_do_nothing(self):
        for build, path in [('postgres-debian', '/usr/share/map-candidate'), ('grafana-security', '/usr/share/map-security/grafana-core')]:
            with self.subTest(build=build), tempfile.TemporaryDirectory() as temp:
                docker = DockerDouble(make_tar([('proof', b'synthetic', tarfile.REGTYPE)]))
                self.invoke(docker, Path(temp), build)
                self.assertIn(docker.name + ':' + path + '/.', next(c for c in docker.calls if c[1] == 'cp'))
        with tempfile.TemporaryDirectory() as temp:
            docker = DockerDouble(b'')
            self.invoke(docker, Path(temp), 'preserve')
            self.assertEqual(docker.calls, [])
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_traversal_absolute_and_links_are_rejected_without_escaping_or_receipt(self):
        for path, kind in [('../outside', tarfile.REGTYPE), ('safe/../../outside', tarfile.REGTYPE),
                           ('/outside', tarfile.REGTYPE), ('link', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE),
                           ('device', tarfile.CHRTYPE), ('pipe', tarfile.FIFOTYPE)]:
            with self.subTest(path=path, kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); output = root / 'evidence'; output.mkdir()
                docker = DockerDouble(make_tar([(path, b'bad', kind)]))
                with self.assertRaises(ValueError): self.invoke(docker, output)
                self.assertFalse((root / 'outside').exists())
                self.assertFalse((output / 'build-evidence-receipt.json').exists())
                self.assertEqual(docker.operations()[-2:], ['inspect', 'rm'])
                self.assertNotIn('start', docker.operations())

    def test_compressed_archive_is_rejected_instead_of_expanding(self):
        docker = DockerDouble(make_tar([('proof', b'synthetic', tarfile.REGTYPE)], 'w:gz'))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(tarfile.ReadError): self.invoke(docker, Path(temp))
            self.assertFalse((Path(temp) / 'build-evidence-receipt.json').exists())
        self.assertEqual(docker.operations()[-2:], ['inspect', 'rm'])

    def test_empty_or_duplicate_files_never_produce_a_success_receipt(self):
        for entries in [[], [('same', b'one', tarfile.REGTYPE), ('./same', b'two', tarfile.REGTYPE)]]:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temp:
                docker = DockerDouble(make_tar(entries)); output = Path(temp)
                with self.assertRaises((ValueError, FileExistsError)): self.invoke(docker, output)
                self.assertFalse((output / 'build-evidence-receipt.json').exists())
                self.assertEqual(docker.operations()[-2:], ['inspect', 'rm'])

    def test_unowned_wrong_image_or_started_container_is_never_copied_or_removed(self):
        def foreign(meta, _): meta['Config']['Labels']['map.build.evidence'] = 'another-session'
        def wrong_image(meta, _): meta['Image'] = 'sha256:' + '9' * 64
        def running(meta, _): meta['State'].update(Status='running', Running=True)
        def exited(meta, _): meta['State'].update(Status='exited', Running=False, StartedAt='2026-09-07T00:00:00Z')
        def formerly_started(meta, _): meta['State']['StartedAt'] = '2026-09-07T00:00:00Z'
        for mutate, error in [(foreign, 'build_evidence_owner'), (wrong_image, 'build_evidence_image'),
                              (running, 'build_evidence_not_unstarted'), (exited, 'build_evidence_not_unstarted'),
                              (formerly_started, 'build_evidence_not_unstarted')]:
            with self.subTest(error=error, mutate=mutate.__name__), tempfile.TemporaryDirectory() as temp:
                docker = DockerDouble(make_tar([('proof', b'synthetic', tarfile.REGTYPE)]), mutate)
                with self.assertRaisesRegex(ValueError, error): self.invoke(docker, Path(temp))
                self.assertNotIn('cp', docker.operations())
                self.assertNotIn('rm', docker.operations())

    def test_cleanup_rechecks_ownership_image_and_unstarted_state_after_copy(self):
        for change in ('owner', 'image', 'started'):
            def mutate(meta, count):
                if count == 1: return
                if change == 'owner': meta['Config']['Labels']['map.build.evidence'] = 'other-session'
                elif change == 'image': meta['Image'] = 'sha256:' + '8' * 64
                else: meta['State'].update(Status='exited', StartedAt='2026-09-07T00:00:00Z')
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                docker = DockerDouble(make_tar([('proof', b'synthetic', tarfile.REGTYPE)]), mutate)
                with self.assertRaises(ValueError): self.invoke(docker, Path(temp))
                self.assertIn('cp', docker.operations())
                self.assertNotIn('rm', docker.operations())

    def test_limits_reject_excess_member_count_expanded_total_and_single_file_before_read(self):
        class MetadataArchive:
            def __init__(self, count, size, kind): self.count, self.size, self.kind = count, size, kind
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def __iter__(self):
                for n in range(self.count):
                    entry = tarfile.TarInfo('synthetic-' + str(n)); entry.type = self.kind; entry.size = self.size
                    yield entry
            def extractfile(self, member): raise AssertionError('Oversized evidence must not be read')
        cases = [(20001, 0, tarfile.DIRTYPE, 'bounded_build_evidence_expanded'),
                 (1, 64 * 1024 * 1024, tarfile.DIRTYPE, 'bounded_build_evidence_expanded'),
                 (1, 16 * 1024 * 1024, tarfile.REGTYPE, 'bounded_build_evidence_file')]
        for count, size, kind, reason in cases:
            with self.subTest(count=count, size=size), tempfile.TemporaryDirectory() as temp:
                docker = DockerDouble(b'synthetic archive metadata double')
                with patch.object(scanner.tarfile, 'open', return_value=MetadataArchive(count, size, kind)):
                    with self.assertRaisesRegex(ValueError, reason): self.invoke(docker, Path(temp))
                self.assertFalse((Path(temp) / 'build-evidence-receipt.json').exists())
                self.assertEqual(docker.operations()[-2:], ['inspect', 'rm'])

    def test_cleanup_distinguishes_absent_container_from_daemon_failure(self):
        raw = make_tar([('proof', b'synthetic', tarfile.REGTYPE)])
        with tempfile.TemporaryDirectory() as temp:
            docker = DockerDouble(raw, missing_cleanup=True)
            self.invoke(docker, Path(temp))
            self.assertNotIn('rm', docker.operations())
        with tempfile.TemporaryDirectory() as temp:
            docker = DockerDouble(raw, missing_cleanup='daemon')
            with self.assertRaisesRegex(ValueError, 'build_evidence_cleanup_inspect_failed'):
                self.invoke(docker, Path(temp))
            self.assertNotIn('rm', docker.operations())


if __name__ == '__main__':
    unittest.main()
