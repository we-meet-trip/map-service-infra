"""Explicit variant isolation and independent SFCGAL answer checks (no Docker)."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

c = load('variant_scanner', ROOT / 'scripts/scan-infrastructure-candidates.py')
p = load('variant_pg_fixture', ROOT / 'security/infrastructure/fixtures/postgres_restore.py')


class VariantWiringTests(unittest.TestCase):
    def test_default_stays_bookworm_and_alternative_changes_only_pg_recipe(self):
        default = c.load_spec()
        alternative = c.load_spec('trixie')
        self.assertEqual('bookworm', default['selected_postgres_variant'])
        for old, new in zip(default['services'], alternative['services']):
            if old['service'] != 'postgres':
                self.assertEqual(old, new)
                continue
            self.assertEqual('postgres-debian', old['build'])
            self.assertEqual('postgres-trixie', new['build'])
            self.assertEqual(c.POSTGRES_TRIXIE_BASE, new['candidate_selector'])
            self.assertEqual({k:v for k,v in old.items() if k not in ('build','candidate_selector')},
                             {k:v for k,v in new.items() if k not in ('build','candidate_selector')})
        self.assertFalse(alternative['candidate_security_approved'])
        self.assertFalse(alternative['production_data_migration_approved'])

    def test_tampered_or_extra_alternative_cannot_select_unreviewed_base(self):
        original = json.loads(c.SPEC.read_text())
        variants = []
        data = deepcopy(original); data['postgres_alternatives']['trixie']['candidate_selector']='postgres:17'; variants.append(data)
        data = deepcopy(original); data['postgres_alternatives']['sid'] = data['postgres_alternatives']['trixie']; variants.append(data)
        for data in variants:
            with self.subTest(data=data['postgres_alternatives']), patch.object(Path,'read_text',return_value=json.dumps(data)):
                with self.assertRaisesRegex(ValueError,'pinned_postgres_alternatives_contract'):
                    c.load_spec('trixie')
        with self.assertRaisesRegex(ValueError,'postgres_variant_allowlist'):
            c.load_spec('sid')

    def test_trixie_requires_explicit_postgres_only_selection_before_execution(self):
        with patch('sys.argv', ['scan', '--run', '--postgres-variant', 'trixie']), patch.object(c, 'execute') as execute:
            with self.assertRaisesRegex(ValueError, 'trixie_comparison_postgres_only'):
                c.main()
            execute.assert_not_called()
        with patch('sys.argv', ['scan', '--run', '--postgres-variant', 'trixie', '--services', 'postgres']), patch.object(c, 'execute', return_value=0) as execute:
            self.assertEqual(0, c.main())
            self.assertEqual({'postgres'}, execute.call_args.args[2])
            self.assertEqual('trixie', execute.call_args.args[0]['selected_postgres_variant'])


class SfcgalAnswersTests(unittest.TestCase):
    def test_both_library_major_versions_use_identical_independent_geometry_answers(self):
        for version in ('1.4.1', '2.0.0'):
            p.validate_sfcgal({'sfcgal_version': version, **p.SFCGAL_ANSWERS})

    def test_missing_nan_reordered_endpoint_dimension_or_incorrect_volume_fails(self):
        baseline = {'sfcgal_version': '2.0.0', **p.SFCGAL_ANSWERS}
        for key, value in [('solid_volume', 0), ('solid_volume', float('nan')),
                           ('intersection_dimension', 2), ('intersection_min_x', 2),
                           ('intersection_length', True), ('sfcgal_version', '')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                p.validate_sfcgal(baseline | {key: value})
        with self.assertRaisesRegex(ValueError, 'sfcgal_probe_fields'):
            p.validate_sfcgal({'sfcgal_version': '2.0.0'})


if __name__ == '__main__': unittest.main()
