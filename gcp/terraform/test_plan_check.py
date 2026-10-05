import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import plan_check


def change(kind, actions, before, after, name='this'):
    return {'address': f'{kind}.{name}', 'type': kind, 'change': {'actions': actions, 'before': before, 'after': after}}


def plan(*changes):
    return {'format_version': '1.2', 'planned_values': {}, 'resource_changes': list(changes)}


class PlanCheckTests(unittest.TestCase):
    def test_replacing_any_protected_resource_is_blocked(self):
        for kind in ('google_compute_address', 'google_compute_disk', 'google_compute_instance',
                     'google_storage_bucket', 'google_logging_project_bucket_config',
                     'google_iam_workload_identity_pool', 'google_iam_workload_identity_pool_provider'):
            with self.subTest(kind=kind):
                self.assertEqual(plan_check.violations(plan(change(kind, ['delete', 'create'], {}, {}))),
                                 [f'{kind}.this: delete/create would delete it'])

    def test_disabling_existing_web_public_is_blocked(self):
        rule = change('google_compute_firewall', ['update'],
                      {'name': 'map-prod-web-public', 'disabled': False}, {'name': 'map-prod-web-public', 'disabled': True})
        self.assertEqual(plan_check.violations(plan(rule)),
                         ['google_compute_firewall.this: web-public would stop serving'])

    def test_switching_off_or_deleting_an_enabled_alert_policy_is_blocked(self):
        for actions, after in ((['update'], {'enabled': False}), (['delete'], None), (['delete', 'create'], {'enabled': True})):
            with self.subTest(actions=actions):
                policy = change('google_monitoring_alert_policy', actions, {'display_name': 'map-prod cpu', 'enabled': True}, after)
                self.assertEqual(plan_check.violations(plan(policy)),
                                 ['google_monitoring_alert_policy.this: alert policy would stop alerting'])

    def test_first_creation_disabled_and_ordinary_updates_pass(self):
        clean = plan(
            change('google_compute_firewall', ['create'], None, {'name': 'map-prod-web-public', 'disabled': True}),
            change('google_monitoring_alert_policy', ['create'], None, {'enabled': False}),
            change('google_monitoring_alert_policy', ['update'], {'enabled': False}, {'enabled': True}, name='cpu'),
            change('google_compute_firewall', ['update'],
                   {'name': 'map-prod-web-owner', 'disabled': False}, {'name': 'map-prod-web-owner', 'disabled': True}, name='owner'),
            change('google_storage_bucket', ['update'], {'name': 'map-prod-backups'}, {'name': 'map-prod-backups'}),
            change('google_compute_instance', ['no-op'], {'name': 'map-prod'}, {'name': 'map-prod'}),
        )
        self.assertEqual(plan_check.violations(clean), [])

    def test_frozen_also_refuses_switching_existing_rules_on(self):
        switched = plan(
            change('google_compute_firewall', ['update'],
                   {'name': 'map-prod-web-public', 'disabled': True}, {'name': 'map-prod-web-public', 'disabled': False}),
            change('google_monitoring_alert_policy', ['update'], {'enabled': False}, {'enabled': True}, name='backup_absent'),
            change('google_monitoring_alert_policy', ['delete'], {'enabled': False}, None, name='cpu'),
            change('google_monitoring_alert_policy', ['update'],
                   {'enabled': True, 'display_name': 'old'}, {'enabled': True, 'display_name': 'new'}, name='uptime'),
        )
        self.assertEqual(plan_check.violations(switched), [])
        self.assertEqual(plan_check.violations(switched, frozen=True), [
            'google_compute_firewall.this: web-public would start serving while frozen',
            'google_monitoring_alert_policy.backup_absent: alert policy would change while frozen',
            'google_monitoring_alert_policy.cpu: alert policy would change while frozen',
        ])

    def test_main_prints_actions_only_and_sets_exit_status(self):
        secret = 'owner@example.com'
        blocked = plan(
            change('google_monitoring_notification_channel', ['create'], None, {'labels': {'email_address': secret}}),
            change('google_storage_bucket', ['delete'], {'name': 'map-prod-backups'}, None),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'plan.json'
            path.write_text(json.dumps(blocked), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                status = plan_check.main(['plan_check.py', str(path)])
            self.assertEqual(status, 1)
            self.assertNotIn(secret, output.getvalue())
            self.assertIn('create google_monitoring_notification_channel.this', output.getvalue())
            self.assertIn('BLOCK google_storage_bucket.this: delete would delete it', output.getvalue())
            path.write_text(json.dumps(plan(change('google_monitoring_alert_policy', ['update'], {'enabled': False}, {'enabled': True}))),
                            encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(plan_check.main(['plan_check.py', str(path)]), 0)
                self.assertEqual(plan_check.main(['plan_check.py', '--frozen', str(path)]), 1)
            path.write_text(json.dumps({'format_version': '1.0', 'values': {}}), encoding='utf-8')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(plan_check.main(['plan_check.py', str(path)]), 2)


class ReadmeProcedureTests(unittest.TestCase):
    def test_shell_steps_keep_private_values_off_screen(self):
        readme = (Path(__file__).parent / 'README.md').read_text(encoding='utf-8')
        code, inside = [], False
        for line in readme.splitlines():
            if line.startswith('```'):
                inside = not inside
            elif inside:
                code.append(line.split('#', 1)[0].strip())
        tofu = [line for line in code if line.startswith('tofu ')]
        self.assertEqual({line.split()[1] for line in tofu}, {'init', 'plan', 'show', 'apply'})
        for line in tofu:
            with self.subTest(line=line):
                # The human-readable output carries emails, IPs and IDs; only plan_check's summary reaches the screen.
                self.assertRegex(line, r'> \$T/\S+ 2>&1$' if line.split()[1] != 'show' else r'> \$T/\S+\.json$')
        self.assertEqual([line for line in code if 'TEAM_GROUP' in line], [])
        self.assertIn('-var prod_public_web=true -var prod_vm_alerts=true', readme)
        self.assertIn('plan_check.py --frozen', readme)


if __name__ == '__main__':
    unittest.main()
