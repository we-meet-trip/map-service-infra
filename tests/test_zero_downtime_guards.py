import http.server
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


gate = module('migration_expand_gate', 'migration-expand-gate.py')
probe = module('zd_probe', 'zd-probe.py')

ADDITIVE = """
CREATE TABLE user_service.external_ai_consents (
    id varchar(64) PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES user_service.users(id) ON DELETE CASCADE,
    accepted boolean NOT NULL
);
CREATE INDEX external_ai_consents_user_idx ON user_service.external_ai_consents(user_id);
ALTER TABLE user_service.schedules ADD COLUMN note text;
"""


class ExpandGateTests(unittest.TestCase):
    def check(self, body, name='V030__change.sql'):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / name
            path.write_text(body)
            return gate.inspect(path)

    def test_a_new_table_with_its_own_required_columns_is_additive(self):
        # NOT NULL inside a brand new table cannot break a running version.
        self.assertEqual([f['rule'] for f in self.check(ADDITIVE)], [])

    def test_every_shape_that_breaks_the_running_version_is_named(self):
        cases = {
            'drop_table': 'DROP TABLE user_service.old_places;',
            'drop_column': 'ALTER TABLE user_service.users DROP COLUMN nickname;',
            'rename': 'ALTER TABLE user_service.users RENAME COLUMN nickname TO handle;',
            'drop_schema_or_type': 'DROP INDEX user_service.users_email_idx;',
            'set_not_null': 'ALTER TABLE user_service.users ALTER COLUMN phone SET NOT NULL;',
            'narrowing_type': 'ALTER TABLE user_service.users ALTER COLUMN nickname TYPE varchar(8);',
            'destructive_dml': 'DELETE FROM user_service.chat_messages;',
        }
        for rule, body in cases.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, [f['rule'] for f in self.check(body)])

    def test_a_required_column_added_to_an_existing_table_is_blocked_without_a_default(self):
        blocked = self.check('ALTER TABLE user_service.users ADD COLUMN locale text NOT NULL;')
        self.assertIn('add_required_column', [f['rule'] for f in blocked])
        allowed = self.check("ALTER TABLE user_service.users ADD COLUMN locale text NOT NULL DEFAULT 'ko';")
        self.assertNotIn('add_required_column', [f['rule'] for f in allowed])

    def test_a_rule_never_fires_on_a_comment_or_a_value(self):
        self.assertEqual(self.check("-- DROP TABLE users\nSELECT 1;"), [])
        self.assertEqual(self.check("INSERT INTO t(note) VALUES ('DROP TABLE users');"), [])
        self.assertEqual(self.check("DO $body$ BEGIN RAISE NOTICE 'DROP TABLE x'; END $body$;"), [])

    def test_an_exception_needs_a_written_reason(self):
        without = self.check('-- expand-gate: allow drop_table\nDROP TABLE user_service.gone;')
        self.assertIsNone(without[0]['allowed_because'])
        with_reason = self.check('-- expand-gate: allow drop_table replaced by V029, no reader remains\n'
                                 'DROP TABLE user_service.gone;')
        self.assertEqual(with_reason[0]['allowed_because'],
                         'replaced by V029, no reader remains')

    def test_the_shipped_user_consent_migration_shape_passes(self):
        self.assertEqual(self.check(ADDITIVE, 'V029__external_ai_consents.sql'), [])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = self.server.routes.get(self.path, 404)
        self.send_response(code)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, *args):
        pass


class ProbeTests(unittest.TestCase):
    def serve(self, routes):
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        server.routes = routes
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:%d' % server.server_address[1]

    def test_expected_answers_are_a_pass_and_a_wrong_status_is_a_failure(self):
        origin = self.serve({'/healthz': 200, '/healthz/app': 200,
                             '/api/v1/users/me': 401, '/actuator': 404})
        checks = probe.default_checks(origin, '0' * 32)
        runner = probe.Probe(checks, [])
        runner.once()
        report, failures = runner.summary()
        self.assertEqual(failures, 0, report)
        self.assertEqual(report['edge_healthz']['requests'], 1)

        broken = self.serve({'/healthz': 200, '/healthz/app': 502,
                             '/api/v1/users/me': 401, '/actuator': 404})
        runner = probe.Probe(probe.default_checks(broken, '0' * 32), [])
        runner.once()
        report, failures = runner.summary()
        self.assertEqual(failures, 1)
        self.assertEqual(report['application_health']['unexpected_statuses'], [502])

    def test_a_refused_connection_counts_as_a_failure_rather_than_an_error(self):
        runner = probe.Probe([('closed', ('http://127.0.0.1:1/healthz', 200))], [])
        runner.once()
        report, failures = runner.summary()
        self.assertEqual(failures, 1)
        self.assertEqual(report['closed']['unexpected_statuses'], [0])
        self.assertTrue(report['closed']['failure_kinds'])

    def test_the_probe_only_reads(self):
        origin = self.serve({'/healthz': 200, '/healthz/app': 200,
                             '/api/v1/users/me': 401, '/actuator': 404})
        for _, (url, _) in probe.default_checks(origin, '0' * 32):
            self.assertNotIn('?', url)
        self.assertTrue(all(name.startswith(('edge', 'application', 'authenticated',
                                             'unknown', 'actuator'))
                            for name, _ in probe.default_checks(origin, '0' * 32)))


if __name__ == '__main__':
    unittest.main()


class RolloverOrderTests(unittest.TestCase):
    """The replacement order has to follow the direction of internal calls.

    A service keeps the address it was started with, so a service that is
    replaced before the ones calling it loses those calls for as long as its
    canonical container is being recreated.
    """

    # caller -> the services it opens connections to inside the network
    CALLS = {'yolo': {'user'}, 'user': {'agent', 'hub'}, 'agent': {'hub'}, 'hub': set()}

    def order(self):
        body = (ROOT / 'scripts/cloud-up.sh').read_text()
        start = body.index('rollover_up() {')
        block = body[start:body.index('\n}\n', start)]
        import re
        sequence = []
        for line in block.splitlines():
            found = re.search(r'\b(?:local )?services(?:\+)?=\(([^)]*)\)', line)
            if found:
                sequence += found.group(1).split()
        return sequence

    def test_every_service_is_replaced_exactly_once(self):
        sequence = self.order()
        self.assertEqual(sorted(sequence), sorted(self.CALLS))

    def test_a_caller_is_replaced_before_anything_it_calls(self):
        position = {name: index for index, name in enumerate(self.order())}
        for caller, callees in self.CALLS.items():
            for callee in callees:
                with self.subTest(caller=caller, callee=callee):
                    self.assertLess(position[caller], position[callee])

    def test_the_replacement_directory_has_no_default(self):
        body = (ROOT / 'scripts/cloud-up.sh').read_text()
        self.assertIn('PROXY_UPSTREAMS_DIR:?', body)
        self.assertNotIn('PROXY_UPSTREAMS_DIR:-', body)
