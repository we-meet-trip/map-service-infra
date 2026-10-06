"""Static contract for the GCE metadata block unit and the Ops Agent allowlist."""
from pathlib import Path
import re
import shlex
import subprocess
import unittest

try:
    import yaml
except ImportError:  # The string checks below still pin the structure without it.
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / 'deploy/gcp/map-metadata-block.service'
OPS_AGENT = ROOT / 'deploy/gcp/ops-agent-config.yaml'
BACKUP_UNIT = r'"^map-prod-.*backup.*\.service$"'
ALLOWLIST = {
    'jsonPayload._SYSTEMD_UNIT = "ssh.service"',
    'jsonPayload._SYSTEMD_UNIT = "systemd-logind.service"',
    'jsonPayload._SYSTEMD_UNIT =~ ' + BACKUP_UNIT,
    'jsonPayload.UNIT =~ ' + BACKUP_UNIT,
    'jsonPayload.SYSLOG_IDENTIFIER = "sudo"',
}


def unit_sections(path):
    # systemd allows repeated keys (two ExecStart= lines), so keep every value.
    sections, current = {}, None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('['):
            current = sections.setdefault(line.strip('[]'), {})
            continue
        key, _, value = line.partition('=')
        current.setdefault(key, []).append(value)
    return sections


class MetadataBlockUnitTests(unittest.TestCase):
    def test_bound_to_docker_and_reapplied_with_it(self):
        unit = unit_sections(UNIT)
        for section, key in (('Unit', 'After'), ('Unit', 'BindsTo'), ('Unit', 'PartOf'), ('Install', 'WantedBy')):
            self.assertEqual(unit[section][key], ['docker.service'], key)
        self.assertEqual((unit['Service']['Type'], unit['Service']['RemainAfterExit']), (['oneshot'], ['yes']))
        self.assertNotIn('ExecStop', unit['Service'])

    def test_both_protocols_drop_everything_but_dns_idempotently(self):
        commands = unit_sections(UNIT)['Service']['ExecStart']
        self.assertEqual(len(commands), 2)
        for protocol, command in zip(('tcp', 'udp'), commands):
            shell, flag, script = shlex.split(command)
            self.assertEqual((shell, flag), ('/bin/sh', '-c'))
            # The checked rule and the inserted rule must be the same, or re-runs stack duplicates.
            rule = f'DOCKER-USER -d 169.254.169.254/32 -p {protocol} ! --dport 53 -j DROP'
            self.assertEqual(script, f'/usr/sbin/iptables -w -C {rule} 2>/dev/null || /usr/sbin/iptables -w -I {rule}')
            # -n only parses the script; nothing is executed.
            self.assertEqual(subprocess.run(['/bin/sh', '-n', '-c', script]).returncode, 0)


class OpsAgentConfigTests(unittest.TestCase):
    def test_ships_only_the_journald_allowlist(self):
        text = OPS_AGENT.read_text()
        rules = re.findall(r"^\s*- '(.*)'$", text, re.M)
        self.assertEqual(len(rules), 1)
        negated = re.fullmatch(r'NOT \((.*)\)', rules[0])
        self.assertIsNotNone(negated)
        terms = negated[1].split(' OR ')
        self.assertEqual((len(terms), set(terms)), (5, ALLOWLIST))
        self.assertEqual((text.count('type: systemd_journald'), text.count('type: exclude_logs')), (1, 1))
        self.assertIn('\n  receivers:\n    map_journald:\n      type: systemd_journald\n', text)
        self.assertIn('\n      default_pipeline:\n        receivers: []\n', text)
        self.assertIn('\nglobal:\n  default_self_log_file_collection: false\n', text)
        self.assertIsNone(re.search(r'^metrics:', text, re.M))
        if yaml:
            config = yaml.safe_load(text)
            self.assertEqual(set(config), {'logging', 'global'})
            logging = config['logging']
            self.assertEqual(logging['receivers'], {'map_journald': {'type': 'systemd_journald'}})
            self.assertEqual(logging['processors'], {'map_allowlist': {'type': 'exclude_logs', 'match_any': rules}})
            self.assertEqual(logging['service']['pipelines'], {
                'default_pipeline': {'receivers': []},
                'map_journald': {'receivers': ['map_journald'], 'processors': ['map_allowlist']}})
            self.assertIs(config['global']['default_self_log_file_collection'], False)

    def test_backup_pattern_selects_production_backup_services_only(self):
        pattern = re.compile(BACKUP_UNIT.strip('"'))
        # The request-log archive rides on the same pattern so its failures raise the backup alert.
        for name in ('map-prod-pg-backup-gcs.service', 'map-prod-redis-backup-gcs.service',
                     'map-prod-admin-backup-gcs.service', 'map-prod-reqlog-backup.service'):
            self.assertRegex(name, pattern)
            self.assertTrue((ROOT / 'deploy/gcp' / name).is_file(), name)
        for name in ('map-prod-serving.service', 'map-test-backup.service', 'map-prod-pg-backup-gcs.timer'):
            self.assertNotRegex(name, pattern)


if __name__ == '__main__':
    unittest.main()
