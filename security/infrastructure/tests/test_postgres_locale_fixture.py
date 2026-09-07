"""Safety and failure-path checks for the hosted-only synthetic PG fixture."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('pg_locale_fixture', Path(__file__).resolve().parents[1] / 'fixtures/postgres_restore.py')
pg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pg)
OLD = 'postgis/postgis@sha256:' + 'a' * 64
NEW = 'sha256:' + 'b' * 64


def metadata(stage='old'):
    version = pg.OLD_POSTGIS if stage == 'old' else pg.NEW_POSTGIS
    return {'server': str(pg.OLD_SERVER if stage == 'old' else pg.NEW_SERVER), 'postgis': version,
            'pgcrypto_sha256': pg.hashlib.sha256(b'map-synthetic-pgcrypto').hexdigest(),
            'extensions': {'plpgsql': '1.0', 'pgcrypto': '1.3', 'postgis': version, 'postgis_topology': version},
            'encoding': 'UTF8', 'collation': pg.LOCALE, 'ctype': pg.LOCALE, 'provider': 'c',
            'declared_collation_version': '2.31' if stage == 'old' else '2.36',
            'actual_collation_version': '2.31' if stage == 'old' else '2.36',
            'glibc': 'glibc 2.31' if stage == 'old' else 'glibc 2.36',
            'process_uid': [999] * 4, 'process_gid': [999] * 4, 'account_uid': '999', 'account_gid': '999',
            'owners': {p: '999:999:700' for p in pg.OWNER_PATHS}}


def corpus(new=False):
    result = [{'id': i, 'value': value, 'stage': 'old', 'utf8': value.encode().hex()} for i, value in enumerate(pg.OLD_TEXT, 1)]
    if new:
        result.extend({'id': i, 'value': value, 'stage': 'new', 'utf8': value.encode().hex()} for i, value in enumerate(pg.NEW_TEXT, 101))
    return result


def collation(rows):
    # This is a deterministic mock, not a claim about actual en_US glibc ordering.
    rows = [{k: r[k] for k in ('id', 'value', 'stage')} for r in rows]
    rows.sort(key=lambda r: r['value'])
    return {'queries': {'order': {'rows': rows},
                        'range': {'rows': [r for r in rows if 'A' <= r['value'] < 'z']},
                        'equality': {'rows': [r for r in rows if r['value'] == 'é']}}}


class RuntimeContractTest(unittest.TestCase):
    def test_exact_pinned_boundaries_are_accepted(self):
        for stage in ('old', 'new'):
            pg.validate_runtime(metadata(stage), stage)

    def test_locale_encoding_and_provider_changes_fail_closed(self):
        for key, value in [('collation', 'C'), ('ctype', 'C'), ('encoding', 'SQL_ASCII'), ('provider', 'i'),
                           ('actual_collation_version', '2.40'), ('declared_collation_version', None),
                           ('glibc', 'musl 1.2.5')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                meta = metadata(); meta[key] = value
                pg.validate_runtime(meta, 'old')

    def test_wrong_old_and_new_patch_or_catalog_is_rejected(self):
        for stage in ('old', 'new'):
            for key, value in [('server', '170012'), ('postgis', '3.6.0')]:
                with self.subTest(stage=stage, key=key), self.assertRaises(ValueError):
                    meta = metadata(stage); meta[key] = value
                    pg.validate_runtime(meta, stage)
            with self.assertRaisesRegex(ValueError, 'catalog_library'):
                meta = metadata(stage); meta['extensions']['postgis'] = '3.4.0'
                pg.validate_runtime(meta, stage)
        with self.assertRaisesRegex(ValueError, 'pgcrypto_runtime_digest'):
            meta = metadata(); meta['pgcrypto_sha256'] = '0' * 64
            pg.validate_runtime(meta, 'old')

    def test_account_uid_does_not_hide_root_process_or_data_owner(self):
        for key, value in [('process_uid', [0, 999, 999, 999]), ('process_gid', [999, 0, 999, 999]), ('account_uid', '70')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                meta = metadata(); meta[key] = value
                pg.validate_runtime(meta, 'old')
        with self.assertRaisesRegex(ValueError, 'owner_uid_gid'):
            meta = metadata(); meta['owners'][pg.OWNER_PATHS[0]] = '0:0:700'
            pg.validate_runtime(meta, 'old')
        with self.assertRaisesRegex(ValueError, 'paths_missing'):
            meta = metadata(); meta['owners'].pop(pg.OWNER_PATHS[-1])
            pg.validate_runtime(meta, 'old')

    def test_only_fixed_synthetic_corpus_is_accepted(self):
        self.assertIn('한글', pg.text_insert(pg.OLD_TEXT, 'old', 0))
        self.assertNotEqual('é'.encode(), 'e\u0301'.encode())
        with self.assertRaisesRegex(ValueError, 'synthetic_corpus_required'):
            pg.text_insert(['external-input'], 'old', 0)


class CollationEvidenceTest(unittest.TestCase):
    def test_index_result_and_executed_plan_are_both_required(self):
        rows = [{'id': 1, 'value': '가', 'stage': 'old'}]
        sequential = {'Node Type': 'Sort', 'Plans': [{'Node Type': 'Seq Scan', 'Actual Loops': 1}]}
        indexed = {'Node Type': 'Index Only Scan', 'Index Name': 'fixture_text_value_key', 'Actual Loops': 1}
        pg.validate_query_pair(rows, rows, sequential, indexed)
        with self.assertRaisesRegex(ValueError, 'query_mismatch'):
            pg.validate_query_pair(rows, [], sequential, indexed)
        for bad in [{'Node Type': 'Index Only Scan', 'Index Name': 'fixture_text_value_key'},
                    {'Node Type': 'Seq Scan', 'Actual Loops': 1},
                    {'Node Type': 'Index Scan', 'Index Name': 'collation_fixture_pkey', 'Actual Loops': 1}]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'expected_btree_plan'):
                pg.validate_query_pair(rows, rows, sequential, bad)

    def test_order_changes_are_distinct_from_data_or_range_changes(self):
        before = collation(corpus())
        after = deepcopy(before); after['queries']['order']['rows'].reverse()
        result = pg.compare_collation(before, after)
        self.assertTrue(result['same_corpus'])
        self.assertTrue(result['ordering_changed'])
        self.assertFalse(result['range_membership_changed'])
        after = deepcopy(before); after['queries']['range']['rows'].pop()
        result = pg.compare_collation(before, after)
        self.assertFalse(result['ordering_changed'])
        self.assertTrue(result['range_membership_changed'])

    def test_new_rows_do_not_disguise_or_false_flag_old_cohort_change(self):
        before, after = collation(corpus()), collation(corpus(True))
        self.assertFalse(pg.compare_collation(before, after)['same_corpus'])
        self.assertTrue(pg.compare_collation(before, after, old_cohort=True)['same_corpus'])
        self.assertFalse(pg.compare_collation(before, after, old_cohort=True)['ordering_changed'])
        old_rows = after['queries']['order']['rows']
        old_indices = [i for i, r in enumerate(old_rows) if r['stage'] == 'old']
        i, j = old_indices[:2]; old_rows[i], old_rows[j] = old_rows[j], old_rows[i]
        self.assertTrue(pg.compare_collation(before, after, old_cohort=True)['ordering_changed'])

    def test_semantic_failure_is_recorded_without_aborting_rollback(self):
        result = {'checks': {}}
        before = collation(corpus()); after = deepcopy(before)
        after['queries']['equality']['rows'] = []
        pg.record_comparison(result, 'compare', before, after)
        self.assertEqual(result['checks']['compare']['status'], 'FAIL')
        self.assertTrue(result['collation_comparisons']['compare']['equality_membership_changed'])


class FailureSafetyTest(unittest.TestCase):
    def test_local_execution_and_mutable_tags_never_start_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pg.subprocess, 'run') as run:
            target = Path(tmp) / 'not-created'
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'remote_hosted_ci_only'):
                pg.execute(OLD, NEW, target)
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted'}), patch.object(pg.sys, 'platform', 'linux'):
                with self.assertRaisesRegex(ValueError, 'immutable_old_image_required'):
                    pg.execute('postgis/postgis:17-3.5', NEW, target)
            run.assert_not_called()
            self.assertFalse(target.exists())

    def test_cleanup_refuses_foreign_container_and_continues_owned_cleanup(self):
        fixture = pg.Fixture(Path('/unused')); fixture.containers = ['owned', 'foreign']
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[1] == 'inspect':
                label = fixture.token if args[2] == 'owned' else 'another-session'
                return subprocess.CompletedProcess(args, 0, json.dumps([{'Config': {'Labels': {'map.infra.fixture': label}}}]).encode(), b'')
            return subprocess.CompletedProcess(args, 0, b'', b'')
        with patch.object(fixture, 'run', side_effect=run):
            outcomes = fixture.cleanup()
        self.assertTrue(outcomes['foreign'].startswith('FAIL'))
        self.assertEqual(outcomes['owned'], 'PASS')
        self.assertNotIn(['docker', 'rm', '-f', '-v', 'foreign'], calls)
        self.assertIn(['docker', 'rm', '-f', '-v', 'owned'], calls)

    def test_timeout_preserves_bounded_synthetic_diagnostic(self):
        fixture = pg.Fixture(Path('/unused'))
        timeout = subprocess.TimeoutExpired(['docker', 'exec'], 1, output=b'x' * 5000, stderr=b'synthetic timeout')
        result = {'checks': {}}
        with patch.object(pg.subprocess, 'run', side_effect=timeout):
            pg.checked(result, fixture, 'restore', lambda: fixture.run(['docker', 'exec']))
        self.assertEqual(result['checks']['restore']['status'], 'FAIL')
        self.assertEqual(fixture.failures[0]['returncode'], 'timeout')
        self.assertEqual(len(fixture.failures[0]['synthetic_stdout']), 4096)

    def test_probe_retains_individual_failure_and_not_run_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = pg.Fixture(Path(tmp)); result = {}
            snap = {'runtime': pg.EXPECTED_ACL, 'rows': [{}], 'text': corpus()}
            with patch.object(fixture, 'metadata', return_value=metadata()), patch.object(fixture, 'snapshot', return_value=snap), \
                 patch.object(fixture, 'denied', side_effect=ValueError('expected_sqlstate_42501_missing')):
                with self.assertRaises(ValueError):
                    fixture.probe('synthetic', 'old', 'baseline', result)
            checks = result['observations']['baseline']['checks']
            self.assertEqual(checks['metadata'], 'PASS')
            self.assertEqual(checks['runtime_acl'], 'PASS')
            self.assertEqual(checks['runtime_ddl_denial'], 'FAIL')
            self.assertEqual(checks['unique_constraint'], 'NOT_RUN')


class FakeFixture(pg.Fixture):
    """Orchestration-only double; never pretends to execute PostgreSQL or glibc."""
    fail_restore = None
    fail_dump = None
    change_order = False
    instances = []

    def __init__(self, output):
        super().__init__(output)
        self.states, self.events = {}, []
        self.__class__.instances.append(self)

    def start(self, image, label):
        self.events.append(('start', label)); self.containers.append(label)
        self.states[label] = False
        return label

    def sql(self, name, sql, **kwargs):
        if kwargs.get('user') == 'map_runtime': self.states[name] = True
        return subprocess.CompletedProcess([], 0, b'', b'')

    def stop(self, name): self.events.append(('stop', name))
    def run(self, args, **kwargs): return subprocess.CompletedProcess(args, 0, b'', b'')
    def ready(self, *args): pass

    def probe(self, name, stage, label, result):
        new = self.states[name]
        snap = {'rows': [{'id': 1}] + ([{'id': 2}] if new else []), 'text': corpus(new), 'runtime': pg.EXPECTED_ACL}
        col = collation(snap['text'])
        if self.change_order and stage == 'new': col['queries']['order']['rows'].reverse()
        return snap, col, metadata(stage)

    def dump(self, name, label):
        if label == self.fail_dump: raise pg.CommandFailure(['docker', 'exec'], 1, stderr=b'synthetic dump failure')
        data = b'PGDMP' + (b'new' if self.states[name] else b'old')
        path = self.output / (label + '.dump'); path.write_bytes(data)
        self.backups[label] = {'path': path.name, 'bytes': len(data), 'sha256': pg.hashlib.sha256(data).hexdigest(), 'retained': True}
        return data

    def restore(self, name, dump):
        self.events.append(('restore', name))
        if name == self.fail_restore: raise pg.CommandFailure(['docker', 'exec'], 1, stderr=b'synthetic restore failure')
        self.states[name] = dump.endswith(b'new')

    def cleanup(self): self.events.append(('cleanup',)); return {name: 'PASS' for name in self.containers}
    def diagnostics(self): (self.output / 'synthetic-diagnostics.json').write_text(json.dumps(self.failures))


class RecoveryOrchestrationTest(unittest.TestCase):
    def run_fixture(self, path, fail=None, change_order=False, fail_dump=None):
        with patch.object(pg, 'Fixture', FakeFixture), patch.object(FakeFixture, 'fail_restore', fail), \
             patch.object(FakeFixture, 'fail_dump', fail_dump), \
             patch.object(FakeFixture, 'change_order', change_order), \
             patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted'}), patch.object(pg.sys, 'platform', 'linux'):
            return pg.execute(OLD, NEW, path), FakeFixture.instances[-1]

    def test_complete_mock_flow_is_not_physical_or_production_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, fixture = self.run_fixture(Path(tmp) / 'result')
            self.assertEqual(result['status'], 'PASS')
            self.assertFalse(result['physical_existing_volume_reuse_tested'])
            self.assertFalse(result['production_collation_compatibility_verified'])
            self.assertIn(('restore', 'new-data-old'), fixture.events)
            self.assertEqual(set(result['retained_backups']), {'pre-upgrade', 'post-upgrade'})

    def test_forward_failure_still_recovers_old_backup_and_retains_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result'; result, fixture = self.run_fixture(path, fail='new')
            self.assertEqual(result['status'], 'FAIL')
            self.assertEqual(result['checks']['old_pre_upgrade_backup_restore']['status'], 'PASS')
            self.assertEqual(result['checks']['new_writes']['status'], 'NOT_RUN')
            self.assertLess(fixture.events.index(('stop', 'new')), fixture.events.index(('start', 'old-backup')))
            self.assertTrue((path / 'pre-upgrade.dump').read_bytes().startswith(b'PGDMP'))
            self.assertIn('synthetic restore failure', (path / 'synthetic-diagnostics.json').read_text())
            self.assertEqual(json.loads((path / 'result.json').read_text())['status'], 'FAIL')

    def test_post_new_restore_failure_does_not_erase_either_backup_or_earlier_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result'; result, _ = self.run_fixture(path, fail='new-data-old')
            self.assertEqual(result['status'], 'FAIL')
            self.assertEqual(result['checks']['old_pre_upgrade_backup_restore']['status'], 'PASS')
            self.assertEqual(result['checks']['candidate_restart']['status'], 'PASS')
            self.assertEqual(result['checks']['old_post_upgrade_logical_restore']['status'], 'FAIL')
            for label in ('pre-upgrade', 'post-upgrade'):
                self.assertTrue((path / (label + '.dump')).exists())
            self.assertEqual(result['checks']['retained_backup_integrity']['status'], 'PASS')

    def test_candidate_dump_failure_preserves_prior_backup_and_attempts_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result'; result, fixture = self.run_fixture(path, fail_dump='post-upgrade')
            self.assertEqual(result['status'], 'FAIL')
            self.assertEqual(result['checks']['post_upgrade_backup']['status'], 'FAIL')
            self.assertEqual(result['checks']['old_pre_upgrade_backup_restore']['status'], 'PASS')
            self.assertEqual(result['checks']['old_post_upgrade_logical_restore']['status'], 'NOT_RUN')
            self.assertTrue((path / 'pre-upgrade.dump').exists())
            self.assertFalse((path / 'post-upgrade.dump').exists())
            self.assertIn(('restore', 'old-backup'), fixture.events)

    def test_ordering_difference_fails_compatibility_but_runs_both_recoveries(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, fixture = self.run_fixture(Path(tmp) / 'result', change_order=True)
            self.assertEqual(result['status'], 'FAIL')
            self.assertEqual(result['checks']['forward_restore']['status'], 'PASS')
            self.assertEqual(result['checks']['forward_collation_unchanged']['status'], 'FAIL')
            self.assertTrue(result['collation_comparisons']['forward_collation_unchanged']['same_corpus'])
            self.assertIn(('restore', 'old-backup'), fixture.events)
            self.assertIn(('restore', 'new-data-old'), fixture.events)


if __name__ == '__main__':
    unittest.main()
