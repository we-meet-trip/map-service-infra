#!/usr/bin/env python3
"""Plan locally; rebuild pinned Go sources only inside the remote image builder."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import urllib.parse

HERE = Path(__file__).resolve().parent


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_pins():
    pins = json.loads((HERE / 'pins.json').read_text())
    require(pins['schema_version'] == 1 and pins['candidate_security_approved'] is False, 'candidate_contract')
    require(pins['platform'] == 'linux/amd64', 'target_platform')
    for service in pins['services'].values():
        require(re.fullmatch(r'[0-9a-f]{40}', service['commit']), 'immutable_upstream_commit')
        require(re.fullmatch(r'[a-z0-9/_-]+@sha256:[0-9a-f]{64}', service['runtime_base']), 'immutable_runtime')
    return pins


def plan(service, base, source_sha):
    pins = read_pins()
    require(service in pins['services'], 'unknown_service')
    spec = pins['services'][service]
    require(base == spec['runtime_base'], 'runtime_base_must_match_reviewed_digest')
    require(re.fullmatch(r'[0-9a-f]{40}', source_sha), 'source_sha_required')
    return {'service': service, 'source_sha': source_sha, 'builder': pins['builder'],
            'spec': spec, 'module_pins': {name: pins['module_pins'][name] for name in spec['module_pins']},
            'candidate_security_approved': False}


def require_builder():
    require(os.environ.get('MAP_GO_SECURITY_BUILDER') == '1' and platform.system() == 'Linux'
            and platform.machine() == 'x86_64', 'remote_linux_builder_only_no_local_go_execution')
    require(os.environ.get('GOTOOLCHAIN') == 'local' and os.environ.get('GOWORK') == 'off', 'toolchain_workspace_contract')
    require(os.environ.get('GOSUMDB') == 'sum.golang.org'
            and os.environ.get('GOPROXY') == 'https://proxy.golang.org', 'verified_public_module_sources_required')
    require(not os.environ.get('GONOSUMDB') and not os.environ.get('GOPRIVATE'), 'sumdb_must_not_be_bypassed')


def json_stream(raw):
    decoder, rows = json.JSONDecoder(), []
    raw = raw.strip()
    while raw:
        row, offset = decoder.raw_decode(raw)
        rows.append(row)
        raw = raw[offset:].lstrip()
    return rows


def module_changes(before, after):
    old = {row['Path']: row for row in before if not row.get('Main')}
    new = {row['Path']: row for row in after if not row.get('Main')}
    changes = []
    for name in sorted(old.keys() | new.keys()):
        fields = lambda row: {key: row.get(key) for key in ['Version', 'Replace']}
        a, b = fields(old.get(name, {})), fields(new.get(name, {}))
        if a != b:
            changes.append({'module': name, 'before': a, 'after': b})
    return changes


def verify_download(pinned, downloaded):
    require(downloaded.get('Version') == pinned['version'], 'module_version_mismatch')
    require(downloaded.get('Sum') == pinned['sum'] and downloaded.get('GoModSum') == pinned['go_mod_sum'],
            'module_sumdb_checksum_mismatch')
    require(not downloaded.get('Error'), 'module_download_failed')


def static_elf(path):
    with Path(path).open('rb') as stream:
        header = stream.read(64)
        require(len(header) == 64 and header[:6] == b'\x7fELF\x02\x01', 'linux_amd64_elf_required')
        require(struct.unpack_from('<H', header, 18)[0] == 62, 'amd64_machine_required')
        offset = struct.unpack_from('<Q', header, 32)[0]
        size, count = struct.unpack_from('<HH', header, 54)
        require(size == 56 and 0 < count <= 256, 'bounded_elf_program_headers')
        stream.seek(offset)
        entries = stream.read(size * count)
        require(len(entries) == size * count, 'complete_elf_program_headers')
        types = [struct.unpack_from('<I', entries, i * size)[0] for i in range(count)]
        require(2 not in types and 3 not in types, 'static_binary_without_dynamic_loader_required')
    return {'class': 64, 'machine': 'EM_X86_64', 'PT_INTERP': False, 'PT_DYNAMIC': False}


def unpack_ui(archive, destination):
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        require(len(members) <= 10000 and sum(m.size for m in members) < 128 * 1024 * 1024, 'bounded_ui_archive')
        for member in members:
            p = PurePosixPath(member.name)
            require(p.parts and p.parts[0] == 'static' and not p.is_absolute() and '..' not in p.parts
                    and (member.isfile() or member.isdir()), 'safe_official_ui_member')
        require(len({m.name for m in members}) == len(members), 'unique_ui_archive_paths')
        # Only regular files/directories, using exclusive file creation. Compatible
        # with the Debian builder's stdlib without relying on newer tarfile filters.
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
    require((destination / 'static/mantine-ui/index.html').is_file()
            and (destination / 'static/react-app/index.html').is_file(), 'both_upstream_ui_variants_required')


def execute(recipe):
    require_builder()  # Before any filesystem mutation, Git, Go or network operation.
    source, output = Path('/build/source'), Path('/out')
    source.mkdir(parents=True, exist_ok=False)
    evidence, binaries = output / 'evidence', output / 'bin'
    evidence.mkdir(parents=True, exist_ok=False)
    binaries.mkdir()
    spec, commands = recipe['spec'], []
    (evidence / 'recipe.json').write_text(json.dumps(recipe, indent=2) + '\n')
    shutil.copyfile(HERE / 'pins.json', evidence / 'pins.json')

    def run(args, name, timeout=600, cwd=source):
        print('go_security_step:' + name, flush=True)
        started = time.monotonic()
        try:
            result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            stdout, stderr = error.stdout or b'', error.stderr or b''
            (evidence / (name + '.stdout')).write_bytes(stdout)
            (evidence / (name + '.stderr')).write_bytes(stderr)
            commands.append({'argv': args, 'returncode': None, 'timeout_seconds': timeout,
                             'elapsed_seconds': round(time.monotonic() - started, 3)})
            (evidence / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
            print(stdout[-12000:].decode(errors='replace'), flush=True)
            print(stderr[-12000:].decode(errors='replace'), flush=True)
            raise ValueError('command_timeout:' + name) from error
        (evidence / (name + '.stdout')).write_bytes(result.stdout)
        (evidence / (name + '.stderr')).write_bytes(result.stderr)
        commands.append({'argv': args, 'returncode': result.returncode, 'elapsed_seconds': round(time.monotonic() - started, 3)})
        (evidence / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        if result.returncode:
            # Only public source/build output exists in this isolated builder.
            print(result.stdout[-12000:].decode(errors='replace'), flush=True)
            print(result.stderr[-12000:].decode(errors='replace'), flush=True)
        require(result.returncode == 0, 'command_failed:' + name)
        return result.stdout.decode()

    require(run(['go', 'env', 'GOVERSION'], 'go-version').strip() == recipe['builder']['go_version'], 'exact_go_version')
    run(['go', 'env', '-json'], 'go-environment')
    run(['dpkg-query', '-W', '-f=${Package}\t${Version}\n'], 'builder-packages')
    run(['git', 'init', '.'], 'source-init')
    run(['git', 'remote', 'add', 'origin', spec['repository']], 'source-origin')
    run(['git', 'fetch', '--depth=1', 'origin', spec['commit']], 'source-fetch', timeout=180)
    run(['git', 'checkout', '--detach', 'FETCH_HEAD'], 'source-checkout')
    require(run(['git', 'rev-parse', 'HEAD'], 'upstream-commit').strip() == spec['commit'], 'source_checkout_identity')
    for name, expected in spec['upstream_file_sha256'].items():
        require(sha(source / name) == expected, 'upstream_source_file_digest:' + name)
    for name in ['go.mod', 'go.sum']:
        shutil.copyfile(source / name, evidence / ('upstream.' + name))
    before = json_stream(run(['go', 'list', '-mod=readonly', '-m', '-json', 'all'], 'modules-before'))
    requested = [name + '@' + pin['version'] for name, pin in recipe['module_pins'].items()]
    # No -u, @latest, replace override, disabled checksum DB or automatic toolchain download.
    run(['go', 'get', *requested], 'pinned-module-update', timeout=600)
    after = json_stream(run(['go', 'list', '-mod=readonly', '-m', '-json', 'all'], 'modules-after'))
    resolved = {row['Path']: row for row in after}
    for name, pinned in recipe['module_pins'].items():
        require(resolved.get(name, {}).get('Version') == pinned['version'] and not resolved[name].get('Replace'),
                'exact_resolved_security_module:' + name)
        downloaded = json.loads(run(['go', 'mod', 'download', '-json', name + '@' + pinned['version']],
                                   'checksum-' + name.replace('/', '_')))
        verify_download(pinned, downloaded)
    run(['go', 'mod', 'verify'], 'module-verify')
    (evidence / 'module-changes.json').write_text(json.dumps(module_changes(before, after), indent=2) + '\n')
    run(['git', 'diff', '--', 'go.mod', 'go.sum'], 'module-patch')
    for name in ['go.mod', 'go.sum']:
        shutil.copyfile(source / name, evidence / ('resolved.' + name))
    if 'ui_asset' in spec:
        asset = spec['ui_asset']
        archive = Path('/build/prometheus-web-ui.tar.gz')
        with urllib.request.urlopen(asset['url'], timeout=60) as response, archive.open('xb') as stream:
            stream.write(response.read(asset['size_bytes'] + 1))
        require(archive.stat().st_size == asset['size_bytes'] and sha(archive) == asset['sha256'], 'official_ui_archive_digest')
        unpack_ui(archive, source / 'web/ui')
        run(['bash', 'scripts/compress_assets.sh'], 'compress-official-ui')
        files = sorted(p for p in (source / 'web/ui/static').rglob('*') if p.is_file())
        (evidence / 'ui-assets.json').write_text(json.dumps({'archive': asset, 'files': [
            {'path': str(p.relative_to(source)), 'sha256': sha(p), 'size_bytes': p.stat().st_size} for p in files
        ]}, indent=2) + '\n')
    tags = ['-tags', ','.join(spec['build_tags'])] if spec['build_tags'] else []
    run(['go', 'test', '-mod=readonly', '-short', '-count=1', '-p', '2', '-timeout', '8m', *tags,
         *spec['test_packages']], 'upstream-unit-tests', timeout=900)
    version = spec['version'] + '+map.security.1'
    ldflags = ' '.join(['-s', '-w',
                       '-X github.com/prometheus/common/version.Version=' + version,
                       '-X github.com/prometheus/common/version.Revision=' + spec['commit'] + '.map.' + recipe['source_sha'],
                       '-X github.com/prometheus/common/version.Branch=map-security',
                       '-X github.com/prometheus/common/version.BuildUser=map-remote-ci'])
    binary_records = []
    for name, path in spec['binaries'].items():
        binary = binaries / name
        run(['go', 'build', '-mod=readonly', '-trimpath', '-buildvcs=false', *tags,
             '-ldflags', ldflags, '-o', str(binary), path], 'build-' + name, timeout=1500)
        info = run(['go', 'version', '-m', str(binary)], 'buildinfo-' + name)
        require(recipe['builder']['go_version'] in info and 'CGO_ENABLED=0' in info, 'static_go_build_contract')
        for line in info.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0] == 'dep' and fields[1] in recipe['module_pins']:
                require(fields[2] == recipe['module_pins'][fields[1]]['version'], 'binary_module_version')
        run([str(binary), '--version'], 'version-smoke-' + name)
        binary_records.append({'path': '/bin/' + name, 'sha256': sha(binary), 'size_bytes': binary.stat().st_size,
                               'elf': static_elf(binary)})
    smoke = http_smoke(recipe, binaries, evidence, run)
    run(['go', 'mod', 'verify'], 'module-verify-after-build')
    result = {'schema_version': 1, 'status': 'PASS', 'source_sha': recipe['source_sha'],
              'upstream_commit': spec['commit'], 'service': recipe['service'],
              'recorded_utc': datetime.now(timezone.utc).isoformat(), 'binaries': binary_records,
              'runtime_base': spec['runtime_base'], 'smoke': smoke,
              'module_source_sha256': {name: sha(evidence / name) for name in
                                       ['upstream.go.mod', 'upstream.go.sum', 'resolved.go.mod', 'resolved.go.sum']},
              'tests_scope': 'Selected upstream unit tests and loopback binary/UI/config/rule smoke; parent fixtures/strict scans still required',
              'candidate_security_approved': False}
    (evidence / 'build-result.json').write_text(json.dumps(result, indent=2) + '\n')
    runtime_evidence = output / 'runtime-evidence'
    runtime_evidence.mkdir()
    # Carry complete command/test/checksum/source-patch receipts. The full source
    # lock files are build inputs, not runtime sources; their hashes plus the
    # complete patch reconstruct them from the pinned upstream source commit.
    source_files = {'upstream.go.mod', 'upstream.go.sum', 'resolved.go.mod', 'resolved.go.sum'}
    for path in evidence.iterdir():
        if path.name not in source_files:
            shutil.copyfile(path, runtime_evidence / path.name)


def http_smoke(recipe, binaries, evidence, run):
    service = recipe['service']
    spec = recipe['spec']
    with tempfile.TemporaryDirectory(prefix='map-go-smoke-') as directory:
        root = Path(directory)
        args = [str(binaries / next(iter(spec['binaries']))), '--web.listen-address=127.0.0.1:19190']
        if service == 'prometheus':
            config = root / 'prometheus.yml'
            config.write_text('global:\n  scrape_interval: 1s\nscrape_configs: []\n')
            args += ['--config.file=' + str(config), '--storage.tsdb.path=' + str(root / 'tsdb')]
            run([str(binaries / 'promtool'), 'check', 'config', str(config)], 'promtool-config-smoke')
            rules = root / 'rules.yml'
            rules.write_text('rule_files: []\nevaluation_interval: 1m\ntests:\n'
                             '- interval: 1m\n  input_series:\n  - series: fixture_metric\n    values: "1 2 3"\n'
                             '  promql_expr_test:\n  - expr: fixture_metric\n    eval_time: 2m\n'
                             '    exp_samples:\n    - labels: fixture_metric\n      value: 3\n')
            run([str(binaries / 'promtool'), 'test', 'rules', str(rules)], 'promtool-rule-smoke')
        env = os.environ.copy()
        if service == 'postgres-exporter':
            env['DATA_SOURCE_NAME'] = 'postgresql://fixture:fixture@127.0.0.1:1/fixture?sslmode=disable&connect_timeout=1'
        checks = {'external_database_used': False}
        variants = [('default', [], '/query')]
        if service == 'prometheus':
            variants.append(('old-ui', ['--enable-feature=old-ui'], '/graph'))
        for variant, flags, route in variants:
            logs = evidence / ('loopback-server-' + variant + '.log')
            with logs.open('wb') as log:
                process = subprocess.Popen(args + flags, stdout=log, stderr=subprocess.STDOUT, env=env)
                try:
                    body = b''
                    for _ in range(60):
                        require(process.poll() is None, 'smoke_server_exited')
                        try:
                            if service == 'prometheus':
                                urllib.request.urlopen('http://127.0.0.1:19190/-/ready', timeout=4).close()
                            body = urllib.request.urlopen('http://127.0.0.1:19190/metrics', timeout=4).read()
                            break
                        except (OSError, ValueError):
                            time.sleep(0.25)
                    metric = {'prometheus': b'prometheus_build_info', 'node-exporter': b'node_exporter_build_info',
                              'postgres-exporter': b'postgres_exporter_build_info'}[service]
                    require(metric in body, 'binary_metrics_smoke')
                    checks['loopback_metrics'] = True
                    if service == 'prometheus':
                        html = urllib.request.urlopen('http://127.0.0.1:19190' + route, timeout=5).read().decode()
                        require('<html' in html.lower() and '<script' in html.lower(), 'embedded_ui_html:' + route)
                        scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
                        require(scripts, 'embedded_ui_scripts:' + route)
                        for script in scripts:
                            require(not script.startswith(('http:', 'https:', '//')), 'no_external_ui_script')
                            url = urllib.parse.urljoin('http://127.0.0.1:19190' + route, script)
                            with urllib.request.urlopen(url, timeout=5) as resource:
                                require('javascript' in resource.headers.get('Content-Type', '')
                                        and len(resource.read()) > 100, 'embedded_ui_asset')
                        checks[variant + '_ui_and_scripts'] = True
                        checks.update(promtool_config=True, promtool_rules=True)
                except Exception:
                    log.flush()
                    sys.stderr.write(logs.read_bytes()[-12000:].decode(errors='replace'))
                    raise
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--execute-builder', action='store_true')
    args = parser.parse_args()
    recipe = plan(args.service, args.base, args.source_sha)
    if args.execute_builder:
        execute(recipe)
    else:
        print(json.dumps(recipe, indent=2))


if __name__ == '__main__':
    main()
