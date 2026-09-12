import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ncp_serving', ROOT / 'scripts/ncp-production-serving.py')
serving = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serving)


def config():
    return {'schema_version': 1, 'environment': 'prod', 'runtime_env_sha256': 'a' * 64,
            'public_manifest_sha256': None, 'gemini_key_sha256': hashlib.sha256(b'fixture-gemini').hexdigest()}


def environment():
    env = {'APP_ENV': 'prod', 'POSTGRES_DB': 'map_prod', 'POSTGRES_HOST': 'postgres', 'POSTGRES_PORT': '5432',
        'USER_DATABASE_USER': 'map_user_runtime', 'AGENT_DATABASE_USER': 'map_agent_runtime',
        'REDIS_HOST': 'redis', 'REDIS_PORT': '6379', 'LANGGRAPH_SCHEMA': 'langgraph',
        'KAKAO_PUBLIC_ORIGIN': serving.API, 'KAKAO_OAUTH_REDIRECT_URI': serving.API + '/api/v1/auth/kakao/callback',
        'KAKAO_APP_CALLBACK_SCHEME': 'mapauth://kakao', 'CHAT_INVITE_BASE_URL': serving.SITE + '/invite/',
        'AUTH_ENFORCED': 'true', 'TESTER_SEED_ENABLED': 'false', 'PLACES_STUB_MODE': 'false',
        'TRAINING_CAPTURE_ENABLED': 'false', 'TRAINING_EXPORT_ENABLED': 'false', 'LOCATION_ENC_ENABLED': 'true',
        'LOCATION_WIRE_ENABLED': 'true', 'APPLE_ENABLED': 'true', 'APPLE_CLIENT_ID': 'kr.mapservice.client',
        'GEMINI_API_KEY': 'fixture-gemini', 'VISION_GEMINI_API_KEY': 'fixture-gemini',
        'CORS_ALLOWED_ORIGINS': serving.SITE, 'HUB_BASE_URL': 'http://proxy:8081/hub',
        'AGENT_BASE_URL': 'http://proxy:8081/agent', 'USER_SERVICE_BASE_URL': 'http://proxy:8081/user',
        'OSRM_FOOT_BASE_URL': 'http://osrm-foot:5000', 'OSRM_BICYCLE_BASE_URL': 'http://osrm-bicycle:5000'}
    for name in ('JWT_PRIVATE_KEY', 'JWT_PUBLIC_KEY', 'LOCATION_ENC_ACTIVE_KID', 'LOCATION_ENC_KEYS',
                 'LOCATION_WIRE_KEY', 'CHECKPOINT_ENC_ACTIVE_KID', 'CHECKPOINT_ENC_KEYS', 'INTERNAL_SERVICE_TOKEN',
                 'USER_ADMIN_INTERNAL_TOKEN', 'HUB_ADMIN_INTERNAL_TOKEN', 'VISION_INTERNAL_TOKEN', 'GEMINI_MODEL',
                 'KMA_SERVICE_KEY', 'KAKAO_REST_API_KEY', 'KAKAO_OAUTH_CLIENT_ID', 'KAKAO_OAUTH_CLIENT_SECRET',
                 'APPLE_TEAM_ID', 'APPLE_KEY_ID', 'APPLE_PRIVATE_KEY_B64', 'EDGE_EMAIL'):
        env[name] = 'fixture-' + name.lower()
    passwords = {name: 'fixture-' + name.lower() for name in
        ('USER_DATABASE_PASSWORD', 'HUB_DATABASE_PASSWORD', 'AGENT_DATABASE_PASSWORD', 'REDIS_PASSWORD')}
    env.update({key: value for key, value in passwords.items() if key != 'HUB_DATABASE_PASSWORD'})
    env['HUB_DATABASE_URL'] = 'postgresql+psycopg://map_hub_runtime:' + passwords['HUB_DATABASE_PASSWORD'] + '@postgres:5432/map_prod'
    env['REDIS_URL'] = 'redis://:' + passwords['REDIS_PASSWORD'] + '@redis:6379/0'
    return env, passwords


class FixtureBackend:
    """Control-plane boundary only; never starts Docker or modifies a real DB."""
    def __init__(self):
        self.events = []
        self.fail = set()
        self.binding = 'b' * 64

    def event(self, event):
        self.events.append(event)
        if event in self.fail:
            raise serving.receiver.ReceiverError('fixture_failure')

    def inputs(self, config):
        self.event('inputs')
        return {'binding': self.binding}

    def pg(self, inputs): self.event('pg')
    def rendered(self, inputs): self.event('rendered')
    def public_inputs(self, config):
        self.event('public_inputs')
        if config['public_manifest_sha256'] is None:
            raise serving.receiver.ReceiverError('ready_public_manifest_pin_required')
    def prepare(self, inputs): self.event('prepare')
    def up(self, inputs, names): self.event('edge_up' if names == ('edge',) else 'private_up')
    def private_ready(self): self.event('private_probe')
    def public_ready(self): self.event('public_probe')
    def inventory(self, inputs, names):
        self.event('inventory')
        return {name: {'container_id': 'c' * 64, 'image_id': 'sha256:' + 'd' * 64} for name in names}
    def stop(self, names=serving.PUBLIC_SERVICES):
        self.event('edge_stop' if names == ('edge',) else 'stop')


class ServingStateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = serving.receiver.private_dir(Path(tmp.name).resolve() / 'deploy')
        self.backend = FixtureBackend()
        self.config = config()

    def run_action(self, action, **kwargs):
        return serving.execute(action, self.config, state=self.state, backend=self.backend, **kwargs)

    def record(self):
        return json.loads((self.state / serving.STATE_NAME).read_text())

    def published(self):
        self.run_action('start-private')
        self.config['public_manifest_sha256'] = 'e' * 64
        self.run_action('publish')

    def test_private_start_and_resume_allow_unfinished_public_policies(self):
        self.assertEqual(self.run_action('start-private')['public_serving'], 'HOLD')
        self.assertNotIn('public_inputs', self.backend.events)
        self.run_action('resume')
        self.assertNotIn('edge_up', self.backend.events)
        self.assertNotIn('public_inputs', self.backend.events)

    def test_publication_requires_private_receipt_before_opening_edge(self):
        self.config['public_manifest_sha256'] = 'e' * 64
        with self.assertRaisesRegex(serving.receiver.ReceiverError, 'private_readiness_receipt'):
            self.run_action('publish')
        self.assertNotIn('edge_up', self.backend.events)
        self.assertEqual(self.backend.events[-1], 'stop')

    def test_unfinished_public_inputs_fail_before_edge_and_close_admission(self):
        self.run_action('start-private')
        with self.assertRaisesRegex(serving.receiver.ReceiverError, 'ready_public_manifest'):
            self.run_action('publish')
        self.assertNotIn('edge_up', self.backend.events)
        self.assertEqual(self.record()['public_serving'], 'HOLD')

    def test_systemd_stop_preserves_publication_for_restart(self):
        self.published()
        self.run_action('stop-public', keep_resume_intent=True)
        self.assertTrue(self.record()['resume_public'])
        self.assertEqual(self.record()['public_serving'], 'HOLD')
        self.assertEqual(self.run_action('resume')['public_serving'], 'OPEN')

    def test_explicit_stop_cancels_publication_intent(self):
        self.published()
        self.run_action('stop-public')
        self.assertFalse(self.record()['resume_public'])
        self.backend.events.clear()
        self.run_action('resume')
        self.assertNotIn('public_inputs', self.backend.events)
        self.assertNotIn('edge_up', self.backend.events)

    def test_stop_does_not_require_config_or_runtime_or_public_data(self):
        self.backend.fail.add('inputs')
        result = serving.execute('stop-public', {'invalid': 'config'}, backend=self.backend, state=self.state)
        self.assertEqual(result['public_serving'], 'HOLD')
        self.assertEqual(self.backend.events, ['stop'])

    def test_resume_configuration_failure_closes_preexisting_public_admission(self):
        self.published()
        self.backend.fail.add('inputs')
        with self.assertRaises(serving.receiver.ReceiverError):
            self.run_action('resume')
        self.assertEqual(self.backend.events[-1], 'stop')
        self.assertEqual(self.record()['status'], 'SERVING_FAILED')

    def test_failed_close_reports_unknown_instead_of_successful_hold(self):
        self.published()
        self.backend.fail.update(('public_probe', 'stop'))
        with self.assertRaisesRegex(serving.receiver.ReceiverError, 'public_stop_failed'):
            self.run_action('resume')
        self.assertEqual(self.record()['status'], 'PUBLIC_STOP_FAILED')
        self.assertEqual(self.record()['public_serving'], 'UNKNOWN')

    def test_reboot_wont_open_replaced_public_bundle(self):
        self.published()
        self.config['public_manifest_sha256'] = 'f' * 64
        self.backend.events.clear()
        with self.assertRaisesRegex(serving.receiver.ReceiverError, 'published_bundle_changed'):
            self.run_action('resume')
        self.assertNotIn('edge_up', self.backend.events)

    def test_verify_is_read_only_and_keeps_current_public_status(self):
        self.published()
        before = (self.state / serving.STATE_NAME).read_bytes()
        self.backend.events.clear()
        self.assertEqual(self.run_action('verify')['public_serving'], 'OPEN')
        self.assertEqual(before, (self.state / serving.STATE_NAME).read_bytes())
        self.assertEqual(self.backend.events, ['inputs', 'pg', 'rendered'])

    def test_deploy_lock_prevents_conflicting_backup_or_deployment(self):
        with serving.receiver.writer(self.state):
            with self.assertRaises(serving.receiver.migration.JobError):
                self.run_action('start-private')
        self.assertEqual(self.backend.events, [])

    def test_new_controller_cannot_publish_or_resume_prior_private_readiness(self):
        first = {'schema_version': 1, 'source_ref': 'master', 'controller_source_sha': 'a' * 40,
                 'database_source_sha': 'b' * 40}
        second = {**first, 'controller_source_sha': 'c' * 40}
        for action in ('publish', 'resume'):
            self.backend.binding = serving.runtime_binding({'fixture': 'same-db'}, self.config, first)
            self.run_action('start-private')
            previous_binding = self.record()['binding']
            self.backend.binding = serving.runtime_binding({'fixture': 'same-db'}, self.config, second)
            self.assertNotEqual(previous_binding, self.backend.binding)
            self.backend.events.clear()
            with self.subTest(action=action), self.assertRaises(serving.receiver.ReceiverError):
                self.run_action(action)
            self.assertNotIn('private_up', self.backend.events)
            self.assertNotIn('edge_up', self.backend.events)
            self.assertEqual(self.backend.events[-1], 'stop')
            self.assertEqual(self.record()['public_serving'], 'HOLD')


class ServingInputTests(unittest.TestCase):
    def test_production_key_and_runtime_roles_match_existing_secret_files(self):
        env, passwords = environment()
        self.assertEqual(serving.validate_environment(env, config(), passwords), env)
        for key, replacement in (('VISION_GEMINI_API_KEY', 'old-test-key'),
                                 ('USER_DATABASE_USER', 'postgres'),
                                 ('USER_DATABASE_PASSWORD', 'wrong-password'),
                                 ('CHAT_INVITE_BASE_URL', serving.SITE + '/invite')):
            with self.subTest(key=key), self.assertRaises(serving.receiver.ReceiverError):
                serving.validate_environment({**env, key: replacement}, config(), passwords)

    def test_dotenv_is_literal_and_rejects_operator_credentials(self):
        self.assertEqual(serving.parse_environment(b'GEMINI_API_KEY=a$literal=value\n'),
                         {'GEMINI_API_KEY': 'a$literal=value'})
        for value in (b'POSTGRES_PASSWORD=x', b'USER_MIGRATION_PASSWORD=x', b'GEMINI_API_KEY="quoted"',
                      b'GEMINI_API_KEY=a\nGEMINI_API_KEY=b', b'DOCKER_HOST=remote'):
            with self.subTest(value=value), self.assertRaises(serving.receiver.ReceiverError):
                serving.parse_environment(value)

    def test_postgres_identity_drift_is_rejected_without_create_or_start(self):
        backend = serving.Backend()
        request = {'postgres': {'container_id': 'a' * 64, 'image_id': 'sha256:' + 'b' * 64}}
        item = {'Id': 'c' * 64, 'Image': request['postgres']['image_id'],
                'Config': {'Labels': {}}, 'State': {'Running': True}, 'HostConfig': {}, 'Mounts': []}
        with patch.object(backend, 'docker', return_value=json.dumps([item])) as docker:
            with self.assertRaisesRegex(serving.receiver.ReceiverError, 'postgres_identity_changed'):
                backend.pg({'request': request}, start=True)
        self.assertEqual(docker.call_count, 1)
        self.assertEqual(docker.call_args.args[0][0], 'inspect')

    def test_compose_stop_uses_fixed_labels_without_environment_or_images(self):
        backend = serving.Backend()
        with patch.object(backend, 'docker', side_effect=['a' * 64, '', '', '', '']) as docker:
            backend.stop()
        calls = [call.args[0] for call in docker.call_args_list]
        self.assertEqual(calls[1], ['stop', '--time', '60', 'a' * 64])
        self.assertTrue(all('label=com.docker.compose.project=map-prod' in call for call in calls if call[0] == 'ps'))
        self.assertNotIn('postgres', str(calls))

    def test_inventory_accepts_unset_optional_environment_and_rejects_required_drift(self):
        backend = serving.Backend()
        cid, image = 'a' * 64, 'sha256:' + 'b' * 64
        inputs = {'images': {'yolo': image}, 'rendered': {'services': {'yolo': {
            'environment': {'LOG_LEVEL': None, 'GEMINI_API_KEY': 'fixture=key', 'EMPTY': ''}}}},
            'request': {'postgres': {'container_id': 'c' * 64, 'image_id': 'sha256:' + 'd' * 64}}}
        for key, expected_pass in (('GEMINI_API_KEY=fixture=key', True),
                                   ('GEMINI_API_KEY=wrong', False), ('GEMINI_API_KEY', False)):
            item = {'image': image, 'running': True, 'oom': False,
                    'environment': ['LOG_LEVEL', 'EMPTY=', key]}
            with self.subTest(key=key), patch.object(backend, 'docker', side_effect=[cid, json.dumps(item)]), \
                 patch.object(backend, 'postgres') as postgres:
                if expected_pass:
                    self.assertEqual(backend.inventory(inputs, ('yolo',)),
                                     {'yolo': {'container_id': cid, 'image_id': image}})
                    postgres.assert_called_once_with('c' * 64, 'sha256:' + 'd' * 64)
                else:
                    with self.assertRaisesRegex(serving.receiver.ReceiverError, 'environment_drift'):
                        backend.inventory(inputs, ('yolo',))
                    postgres.assert_not_called()


class ServingPromotionTests(unittest.TestCase):
    def test_reviewed_caddy_identity_reaches_compose_without_rewriting_contract(self):
        backend = serving.Backend()
        index = 'sha256:' + 'a' * 64
        contract = {'images': {name: {'image': index if name == 'edge' else
                    'example.invalid/' + name + '@sha256:' + 'b' * 64} for name in serving.SERVICES},
                    'caddy': {'archive': 'caddy.tar', 'report': 'scan.json'}}
        original = json.dumps(contract, sort_keys=True)
        # Classic Docker exposes the config digest; containerd can expose the index.
        for actual_id in ('sha256:' + 'c' * 64, index):
            with self.subTest(actual_id=actual_id):
                installer = SimpleNamespace(verify_compose=lambda *args: {'id': actual_id})
                runtime = {'release_name': 'prod-fixture'}
                with patch.object(serving.receiver.artifacts, 'caddy', installer):
                    images = backend.resolved_images(runtime, contract)
                self.assertEqual(images['edge'], actual_id)
                self.assertEqual(json.dumps(contract, sort_keys=True), original)
                inputs = {'runtime': runtime, 'environment': {'GEMINI_API_KEY': 'fixture'},
                          'resolved_images': images, 'contract': contract}
                rendered = {'name': 'map-prod', 'services': {name: {'image': image, 'restart': 'no',
                            'environment': {'GEMINI_API_KEY': 'fixture'}} for name, image in images.items()}}

                def compose(command, **kwargs):
                    self.assertEqual(kwargs['env']['PROD_EDGE_IMAGE'], actual_id)
                    self.assertEqual(kwargs['cwd'], serving.ROOT)
                    self.assertIn(str(serving.ROOT / 'docker-compose.prod.yml'), command)
                    return SimpleNamespace(returncode=0, stdout=json.dumps(rendered))

                with patch.object(serving.subprocess, 'run', side_effect=compose):
                    backend.rendered(inputs)
                    if actual_id != index:
                        rendered['services']['edge']['image'] = index
                        with self.assertRaisesRegex(serving.receiver.ReceiverError, 'image_or_restart_mismatch'):
                            backend.rendered(inputs)

    def test_unverified_caddy_never_becomes_a_runtime_image(self):
        backend = serving.Backend()
        contract = {'images': {name: {'image': 'sha256:' + 'a' * 64} for name in serving.SERVICES},
                    'caddy': {'archive': 'caddy.tar', 'report': 'scan.json'}}
        from unittest.mock import Mock
        installer = Mock()
        installer.verify_compose.side_effect = ValueError('unreviewed image')
        with patch.object(serving.receiver.artifacts, 'caddy', installer):
            with self.assertRaisesRegex(serving.receiver.ReceiverError, 'caddy_identity_unverified'):
                backend.resolved_images({'release_name': 'prod-fixture'}, contract)
            installer.verify_compose.side_effect = None
            installer.verify_compose.return_value = {'id': 'mutable-tag:latest'}
            with self.assertRaisesRegex(serving.receiver.ReceiverError, 'caddy_identity_unverified'):
                backend.resolved_images({'release_name': 'prod-fixture'}, contract)

    def test_promoted_controller_requires_clean_master_pin_and_preserves_database_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            database, controller = base / 'database', base / 'controller'
            def git(root, *args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                               stderr=subprocess.DEVNULL).strip()
            for root in (database, controller):
                (root / 'scripts').mkdir(parents=True)
                (root / 'scripts/ncp-production-serving.py').write_text('# fixture\n')
                git(root, 'init', '-q', '-b', 'master')
                git(root, 'add', '.')
                git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
            database_sha, controller_sha = (git(root, 'rev-parse', 'HEAD') for root in (database, controller))
            pin_path = base / 'pin.json'
            pin = {'schema_version': 1, 'source_ref': 'master', 'controller_source_sha': controller_sha,
                   'database_source_sha': database_sha}
            pin_path.write_text(json.dumps(pin)); pin_path.chmod(0o600)
            with patch.multiple(serving, ROOT=database, CONTROLLER_ROOT=controller,
                                PROMOTED_ROOT=controller, CONTROLLER_CONFIG=pin_path,
                                __file__=str(controller / 'scripts/ncp-production-serving.py')), \
                 patch.object(serving.receiver, 'ROOT', database), \
                 patch.object(serving.receiver, 'source_ownership'):
                backend = serving.Backend()
                backend.controller()
                git(controller, 'checkout', '-qb', 'develop')
                with self.assertRaisesRegex(serving.receiver.ReceiverError, 'source_mismatch'):
                    backend.controller()
                git(controller, 'checkout', '-q', '--detach')
                with self.assertRaises(serving.receiver.ReceiverError):
                    backend.controller()
                git(controller, 'checkout', '-q', 'master')
                with patch.object(serving, '__file__', str(controller / 'untracked-copy.py')):
                    with self.assertRaisesRegex(serving.receiver.ReceiverError, 'script_mismatch'):
                        backend.controller()
                for key, value in (('source_ref', 'develop'), ('controller_source_sha', 'a' * 40),
                                   ('database_source_sha', 'b' * 40)):
                    pin_path.write_text(json.dumps({**pin, key: value}))
                    with self.subTest(key=key), self.assertRaises(serving.receiver.ReceiverError):
                        backend.controller()
                pin_path.write_text(json.dumps(pin))
                (controller / 'scripts/ncp-production-serving.py').write_text('# changed\n')
                with self.assertRaisesRegex(serving.receiver.ReceiverError, 'source_mismatch'):
                    backend.controller()
                pin_path.unlink()
                with self.assertRaises(Exception):
                    backend.controller()
                self.assertEqual(git(database, 'rev-parse', 'HEAD'), database_sha)
                self.assertEqual(git(database, 'status', '--porcelain'), '')


class PublicBundleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'public'
        self.root.mkdir()
        self.manifest_path = self.base / 'manifest.json'
        self.artifacts = {'index.html': b'MAP', 'invite/index.html': b'Invite',
            **{'legal/' + name + '.html': b'Policy' for name in ('privacy', 'terms', 'location-terms', 'support', 'delete-account')}}
        values = {'app_config.json': {'environment': 'prod', 'api_base_url': serving.API},
            'invite-environment.json': {'app_environment': 'prod', 'api_allowed_origins': [serving.API],
                'invite_origin': serving.SITE, 'public_site_origin': serving.SITE,
                'app_config_url': serving.SITE + '/app_config.json', 'android_package': 'kr.mapservice.client',
                'invite_scheme': 'mapservice'},
            '.well-known/apple-app-site-association': {'applinks': {'details': [
                {'appID': 'ABCDEFGHIJ.kr.mapservice.client', 'paths': ['/invite/*']}]}},
            '.well-known/assetlinks.json': [{'relation': ['delegate_permission/common.handle_all_urls'],
                'target': {'namespace': 'android_app', 'package_name': 'kr.mapservice.client',
                    'sha256_cert_fingerprints': [':'.join(['AB'] * 32)]}}]}
        self.artifacts.update({name: json.dumps(value).encode() for name, value in values.items()})
        for name, body in self.artifacts.items():
            path = self.root / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_bytes(body)
        self.manifest = {'schema_version': 1, 'status': 'READY_FOR_PUBLICATION', 'environment': 'prod',
            'source_ref': 'master', 'source_sha': 'a' * 40, 'blockers': [], 'public_site_origin': serving.SITE,
            'api_origin': serving.API, 'android_package': 'kr.mapservice.client', 'apple_app_id_prefix': 'ABCDEFGHIJ',
            'files': {name: hashlib.sha256(body).hexdigest() for name, body in self.artifacts.items()}}

    def verify(self):
        self.manifest_path.write_text(json.dumps(self.manifest))
        cfg = {**config(), 'public_manifest_sha256': hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()}
        return serving.verify_public(cfg, root=self.root, manifest_path=self.manifest_path)

    def test_release_ready_native_bundle_matches_both_domains(self):
        self.assertEqual(self.verify()['status'], 'READY_FOR_PUBLICATION')

    def test_draft_or_develop_never_passes_public_admission(self):
        for key, value in (('status', 'DRAFT_NOT_SUBMITTABLE'), ('source_ref', 'develop')):
            old = self.manifest[key]
            self.manifest[key] = value
            with self.assertRaisesRegex(serving.receiver.ReceiverError, 'release_ready_public_bundle'):
                self.verify()
            self.manifest[key] = old

    def test_changed_policy_file_or_unlisted_file_rejects_publication(self):
        (self.root / 'legal/privacy.html').write_text('Changed')
        with self.assertRaisesRegex(serving.receiver.ReceiverError, 'public_file_hash'):
            self.verify()
        (self.root / 'legal/privacy.html').write_bytes(b'Policy')
        (self.root / 'unreviewed.html').write_text('Extra')
        with self.assertRaisesRegex(serving.receiver.ReceiverError, 'public_file_hash'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
