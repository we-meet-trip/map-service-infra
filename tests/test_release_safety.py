import contextlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


pg = module("pg_backup", "pg_backup.py")
audit = module("runtime_audit", "runtime-audit.py")


class MigrationTests(unittest.TestCase):
    def migration(self, failure="", actual="abc123", heads="abc123 (head)"):
        script = '''
set -euo pipefail
db_user=fake; db_name=fake
dc() {
  case "$*" in
    *upgrade*) echo UPGRADE_ATTEMPTED ;;
    *"hub heads") printf '%s\\n' "$STUB_HEADS" ;;
    *"select version_num"*) [ "$STUB_FAIL" != yes ] && printf '%s\\n' "$STUB_ACTUAL" ;;
    *) return 99 ;;
  esac
}
source scripts/lib/migrations.sh
verify_hub_revision
echo START_APPLICATIONS
'''
        return subprocess.run(["bash"], cwd=ROOT, input=script, capture_output=True,
                              text=True, env={**os.environ, "STUB_FAIL": failure,
                                              "STUB_ACTUAL": actual, "STUB_HEADS": heads})

    def test_matching_revision_starts(self):
        self.assertEqual(self.migration().returncode, 0)

    def test_the_check_never_upgrades_because_the_isolated_job_owns_that(self):
        # Only the separate bounded job holds the migrator credential.
        self.assertNotIn("UPGRADE_ATTEMPTED", self.migration().stdout)

    def test_unreadable_database_revision_blocks_existing_schema(self):
        result = self.migration(failure="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("START_APPLICATIONS", result.stdout)

    def test_success_exit_with_wrong_revision_blocks(self):
        self.assertNotEqual(self.migration(actual="old").returncode, 0)

    def test_multiple_heads_block(self):
        self.assertNotEqual(self.migration(heads="abc (head)\ndef (head)").returncode, 0)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def bundle(self):
        calls = []

        def dump(args, path):
            calls.append(args)
            with gzip.open(path, "wb") as stream:
                stream.write(b"CREATE ROLE postgres;\nALTER ROLE postgres NOLOGIN;\nGRANT owner TO migrator WITH INHERIT FALSE GRANTED BY postgres;\n" if "pg_dumpall" in args else b"SELECT 1;\n")

        with patch.object(pg, "compose", return_value=(["docker", "compose"], "map", "map_test")), \
                patch.object(pg, "dump_gzip", side_effect=dump), \
                patch.object(pg, "bootstrap_role", return_value="postgres"), \
                patch.dict(os.environ, {"BACKUP_DIR": str(self.directory), "BACKUP_REMOTE": "",
                                        "BACKUP_REQUIRE_REMOTE": "0"}), \
                contextlib.redirect_stdout(io.StringIO()):
            manifest = pg.backup("test")
        return manifest, calls

    def test_bundle_has_roles_without_passwords_and_data(self):
        manifest, calls = self.bundle()
        meta, paths = pg.verified_bundle(manifest, "test")
        self.assertEqual(meta["environment"], "test")
        self.assertIn("--roles-only", calls[0])
        self.assertIn("--no-role-passwords", calls[0])
        self.assertEqual(len(paths), 2)

    def test_application_counts_exclude_extension_partial_copy(self):
        data = self.directory / 'rows.sql.gz'
        with gzip.open(data, 'wt') as stream:
            stream.write('COPY public.spatial_ref_sys (srid) FROM stdin;\n\\.\n'
                         'COPY user_service.users (id, nickname) FROM stdin;\n'
                         '1\tname\\nsecond line\n2\tother\n\\.\n'
                         'COPY hub_data.places (id) FROM stdin;\n\\.\n')
        self.assertEqual(pg.dump_row_counts(data),
                         {'user_service.users': 2, 'hub_data.places': 0})

    def test_wrong_environment_rejected_before_docker(self):
        manifest, _ = self.bundle()
        with patch.object(pg, "run") as runner, self.assertRaises(pg.BackupError):
            pg.restore("prod", manifest)
        runner.assert_not_called()

    def test_corrupt_data_rejected(self):
        manifest, _ = self.bundle()
        _, paths = pg.verified_bundle(manifest, "test")
        paths[1].write_bytes(b"corrupt")
        with self.assertRaises(pg.BackupError):
            pg.verified_bundle(manifest, "test")

    def test_artifact_path_traversal_rejected(self):
        manifest, _ = self.bundle()
        meta = json.loads(manifest.read_text())
        meta["files"][0]["name"] = "../map-roles.sql.gz"
        manifest.write_text(json.dumps(meta))
        with self.assertRaises(pg.BackupError):
            pg.verified_bundle(manifest, "test")

    def test_remote_copy_failure_propagates(self):
        manifest, _ = self.bundle()
        _, paths = pg.verified_bundle(manifest, "test")
        with patch.object(pg, "run", side_effect=pg.BackupError("copy failed")), \
                self.assertRaises(pg.BackupError):
            pg.upload(paths + [manifest], "backup@example.invalid:/backups")

    def test_scp_checksum_mismatch_fails(self):
        manifest, _ = self.bundle()
        with patch.object(pg, "run", return_value=subprocess.CompletedProcess([], 0, "bad  file")), \
                self.assertRaises(pg.BackupError):
            pg.upload([manifest], "backup@example.invalid:/backups")

    def test_s3_endpoint_and_size_verification(self):
        manifest, _ = self.bundle()
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, str(manifest.stat().st_size))

        import io
        from types import SimpleNamespace
        download = SimpleNamespace(stdout=io.BytesIO(manifest.read_bytes()),
                                   wait=lambda: 0, poll=lambda: 0)
        with patch.object(pg, "run", side_effect=runner), patch.object(
                pg.subprocess, "Popen", return_value=download):
            pg.upload([manifest], "s3://map-backup/test", "https://kr.object.ncloudstorage.com")
        self.assertIn("head-object", calls[1])
        with self.assertRaises(pg.BackupError):
            pg.upload([manifest], "s3://map-backup/test", "http://insecure.invalid")

    def test_explicit_environment_required(self):
        result = subprocess.run(["python3", "scripts/pg_backup.py", "backup"], cwd=ROOT,
                                capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_documented_restore_cli_accepts_option_before_manifest(self):
        result = subprocess.run(["bash", "scripts/pg-restore-check.sh", "--test",
                                 str(self.directory / "missing.manifest.json")],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("FileNotFoundError", result.stderr)
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_restore_uses_only_isolated_new_container(self):
        manifest, _ = self.bundle()
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            if "run" in args:
                return subprocess.CompletedProcess(args, 0)
            if "SELECT rolname FROM pg_roles WHERE oid=10" in args:
                return subprocess.CompletedProcess(args, 0, "postgres\n")
            if "-Atc" in args:
                return subprocess.CompletedProcess(args, 0, "hub_data:3\nuser_service:4\n")
            return subprocess.CompletedProcess(args, 0)

        replayed = []
        class Input(io.BytesIO):
            def close(self):
                replayed.append(self.getvalue())
                super().close()

        class Process:
            def __init__(self, *args, **kwargs):
                self.stdin = Input()

            def wait(self):
                return 0

            def poll(self):
                return 0

        with patch.object(pg, "run", side_effect=runner), \
                patch.object(pg.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(pg.subprocess, "Popen", Process), contextlib.redirect_stdout(io.StringIO()):
            pg.restore("test", manifest)
        creation, cleanup = calls[0], calls[-1]
        self.assertEqual(creation[creation.index("--network") + 1], "none")
        self.assertNotIn("-v", creation)
        self.assertNotIn("-p", creation)
        self.assertIn("POSTGRES_USER=postgres", creation)
        self.assertEqual(replayed[0], b"\nALTER ROLE postgres NOLOGIN;\nGRANT owner TO migrator WITH INHERIT FALSE GRANTED BY postgres;\n")
        self.assertTrue(any('SUPERUSER LOGIN' in str(call) for call in calls))
        name = creation[creation.index("--name") + 1]
        self.assertTrue(name.startswith("map-restore-check-"))
        self.assertEqual(cleanup, ["docker", "rm", "-f", "-v", name])

    def test_legacy_or_ambiguous_bootstrap_is_rejected_before_docker(self):
        manifest, _ = self.bundle()
        metadata = json.loads(manifest.read_text())
        for value in (None, "bad;role", "absent"):
            changed = {**metadata, "source_bootstrap_role": value}
            manifest.write_text(json.dumps(changed))
            pg.verified_bundle(manifest, "test")  # Legacy retention/integrity remains usable.
            with self.subTest(value=value), patch.object(pg, "run") as run, self.assertRaises(pg.BackupError):
                pg.restore("test", manifest)
            run.assert_not_called()
        metadata["source_bootstrap_role"] = "postgres"
        path = manifest.parent / metadata["files"][0]["name"]
        with gzip.open(path, "wb") as stream:
            stream.write(b'CREATE ROLE postgres;\nCREATE ROLE "postgres";\n')
        metadata["files"][0]["sha256"] = pg.checksum(path)
        manifest.write_text(json.dumps(metadata))
        with patch.object(pg, "run") as run, self.assertRaisesRegex(pg.BackupError, "exactly one"):
            pg.restore("test", manifest)
        run.assert_not_called()


class AuditTests(unittest.TestCase):
    def test_info_message_error_word_is_not_error(self):
        self.assertEqual(audit.severity("2026-09-06T10:00:00Z INFO 123 --- [stats] transport error 0"), "INFO")

    def test_structured_info_and_nginx_error(self):
        summary = audit.log_summary('2026-09-06T10:00:00Z {"level":"info","msg":"error"}\n'
                                    '2026-09-06T10:00:01Z [error] limiting requests, client: PRIVATE\n')
        self.assertEqual(summary["error_signatures"], {"rate_limit": 1})
        self.assertNotIn("PRIVATE", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
