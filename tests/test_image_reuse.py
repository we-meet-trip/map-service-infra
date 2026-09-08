import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('image_reuse', ROOT / 'scripts/prepare-image-reuse.py')
reuse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reuse)


class ImageReuseTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, 4, tzinfo=timezone.utc)
        self.data = {'created_at': (self.now - timedelta(hours=1)).isoformat(),
                     'release_tag': '2026-09-07-123-1', 'github_run_id': '123',
                     'services': {s: {'source_sha': 'a' * 40,
                        'image': 'ghcr.io/we-meet-trip/map-service-' + s,
                        'digest': 'sha256:' + 'b' * 64} for s in reuse.release.SERVICES}}

    def git(self, *args):
        if 'rev-parse' in args:
            return 'c' * 40 if args[2].endswith('map-service-user') else 'a' * 40
        return ''

    def test_changed_user_rebuilds_while_other_five_verify_registry_and_reuse(self):
        with patch.object(reuse.release, 'verify_bundle', return_value=self.data) as bundle, \
                patch.object(reuse.release, 'run', side_effect=self.git), \
                patch.object(reuse.release, 'verify_registry_image') as registry:
            result = reuse.plan(Path('/verified'), Path('/src'), now=self.now)
        self.assertEqual(set(result), set(reuse.release.SERVICES) - {'user'})
        self.assertEqual(registry.call_count, 5)
        bundle.assert_called_once_with(Path('/verified'))
        self.assertTrue(all(v['reference'].endswith('@sha256:' + 'b' * 64) for v in result.values()))

    def test_expired_future_or_naive_bundle_stops_before_registry_or_source_access(self):
        for stamp in ((self.now - timedelta(seconds=86401)).isoformat(),
                      (self.now + timedelta(seconds=1)).isoformat(), '2026-09-07T03:00:00'):
            with self.subTest(stamp=stamp), patch.object(reuse.release, 'verify_bundle',
                    return_value={**self.data, 'created_at': stamp}), \
                    patch.object(reuse.release, 'run') as git:
                with self.assertRaises(ValueError): reuse.plan(Path('/b'), Path('/s'), now=self.now)
                git.assert_not_called()

    def test_invalid_bundle_never_becomes_build_input(self):
        with patch.object(reuse.release, 'verify_bundle', side_effect=ValueError('checksum mismatch')), \
                patch.object(reuse.release, 'run') as git:
            with self.assertRaises(ValueError): reuse.plan(Path('/b'), Path('/s'), now=self.now)
            git.assert_not_called()

    def test_registry_platform_or_label_failure_rejects_reuse(self):
        with patch.object(reuse.release, 'verify_bundle', return_value=self.data), \
                patch.object(reuse.release, 'run', side_effect=self.git), \
                patch.object(reuse.release, 'verify_registry_image', side_effect=ValueError('OCI revision mismatch')):
            with self.assertRaises(ValueError): reuse.plan(Path('/b'), Path('/s'), now=self.now)

    def test_dirty_tracked_source_rejects_reuse(self):
        def git(*args):
            if 'diff' in args: raise ValueError('dirty')
            return self.git(*args)
        with patch.object(reuse.release, 'verify_bundle', return_value=self.data), \
                patch.object(reuse.release, 'run', side_effect=git), \
                patch.object(reuse.release, 'verify_registry_image') as registry:
            with self.assertRaises(ValueError): reuse.plan(Path('/b'), Path('/s'), now=self.now)
            registry.assert_not_called()

    def test_untracked_source_rejects_reuse(self):
        def git(*args): return 'injected.py' if 'ls-files' in args else self.git(*args)
        with patch.object(reuse.release, 'verify_bundle', return_value=self.data), \
                patch.object(reuse.release, 'run', side_effect=git), \
                patch.object(reuse.release, 'verify_registry_image') as registry:
            with self.assertRaises(ValueError): reuse.plan(Path('/b'), Path('/s'), now=self.now)
            registry.assert_not_called()

    def test_explicit_reuse_flow_keeps_scan_and_provenance_gates(self):
        workflow = (ROOT / '.github/workflows/image-release.yml').read_text()
        self.assertIn('deploy-gcp.py prepare --run-id "$REUSE_RUN_ID"', workflow)
        self.assertLess(workflow.index('verify optional immutable image reuse bundle'), workflow.index('build and push six images'))
        self.assertLess(workflow.index('build and push six images'), workflow.index('scan six exact registry images'))
        self.assertIn('enforce critical vulnerability gate', workflow)
