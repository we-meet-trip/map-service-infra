#!/usr/bin/env python3
"""Plan with stdlib locally; build/execute gosu only in an explicit remote builder."""
import argparse
import base64
from datetime import datetime, timezone
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import struct
import subprocess
import time
import urllib.request

HERE = Path(__file__).resolve().parent
MAX_EVIDENCE_FILE = 16 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plan(source_sha):
    require(re.fullmatch(r'[a-f0-9]{40}', source_sha), 'exact_candidate_source_sha_required')
    pins = json.loads((HERE / 'pins.json').read_text())
    require(pins['schema_version'] == 1 and pins['candidate_security_approved'] is False,
            'candidate_only_contract')
    require(pins['platform'] == 'linux/amd64', 'reviewed_platform_only')
    require(re.fullmatch(r'[a-f0-9]{40}', pins['source']['commit']), 'immutable_upstream_commit')
    require(re.fullmatch(r'golang@sha256:[a-f0-9]{64}', pins['builder']['image']), 'immutable_builder')
    return {'source_sha': source_sha, **pins}


def require_builder():
    require(os.environ.get('MAP_GOSU_SECURITY_BUILDER') == '1'
            and platform.system() == 'Linux' and platform.machine() == 'x86_64'
            and os.geteuid() == 0, 'explicit_remote_linux_root_builder_only')
    for key, expected in {'GOTOOLCHAIN': 'local', 'GOWORK': 'off',
                          'GOPROXY': 'https://proxy.golang.org', 'GOSUMDB': 'sum.golang.org',
                          'CGO_ENABLED': '0', 'GOOS': 'linux', 'GOARCH': 'amd64', 'GOAMD64': 'v1'}.items():
        require(os.environ.get(key) == expected, 'required_builder_environment:' + key)
    require(not os.environ.get('GONOSUMDB') and not os.environ.get('GOPRIVATE'), 'sumdb_not_bypassed')


def json_stream(raw):
    decoder, result = json.JSONDecoder(), []
    while raw.strip():
        raw = raw.lstrip()
        value, index = decoder.raw_decode(raw)
        result.append(value)
        raw = raw[index:]
    return result


def verify_module(expected, actual):
    require(not actual.get('Error') and not actual.get('Replace'), 'module_download_or_replacement')
    require(actual.get('Version') == expected['version']
            and actual.get('Sum') == expected['sum']
            and actual.get('GoModSum') == expected['go_mod_sum'], 'exact_module_checksums_required')


def dearmor_key(armored):
    lines = armored.decode('ascii').splitlines()
    require(lines[0] == '-----BEGIN PGP PUBLIC KEY BLOCK-----'
            and lines[-1] == '-----END PGP PUBLIC KEY BLOCK-----', 'public_key_armor_only')
    start = lines.index('') + 1
    payload = ''.join(line for line in lines[start:-1] if not line.startswith('='))
    return base64.b64decode(payload, validate=True)


def elf_identity(path):
    data = Path(path).read_bytes()
    require(len(data) >= 64 and data[:6] == b'\x7fELF\x02\x01', 'elf64_little_endian_required')
    require(struct.unpack_from('<H', data, 18)[0] == 62, 'amd64_required')
    offset = struct.unpack_from('<Q', data, 32)[0]
    size, count = struct.unpack_from('<HH', data, 54)
    require(size == 56 and 0 < count < 256 and offset + size * count <= len(data), 'valid_program_headers')
    types = [struct.unpack_from('<I', data, offset + size * i)[0] for i in range(count)]
    require(2 not in types and 3 not in types, 'static_elf_no_dynamic_loader')
    return {'machine': 'EM_X86_64', 'class': 64, 'PT_DYNAMIC': False, 'PT_INTERP': False}


class Runner:
    def __init__(self, evidence, cwd):
        self.evidence, self.cwd, self.commands = evidence, cwd, []

    def __call__(self, argv, name, timeout=180, expected=0):
        print('gosu_security_step:' + name, flush=True)
        started = time.monotonic()
        timed_out = False
        try:
            result = subprocess.run(argv, cwd=self.cwd, capture_output=True, timeout=timeout)
            stdout, stderr, code = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as error:
            stdout, stderr, code = error.stdout or b'', error.stderr or b'', None
            timed_out = True
        for suffix, output in [('stdout', stdout), ('stderr', stderr)]:
            # Fail on oversize rather than silently claiming complete truncated evidence.
            require(len(output) < MAX_EVIDENCE_FILE, 'oversize_command_output:' + name)
            (self.evidence / (name + '.' + suffix)).write_bytes(output)
        self.commands.append({'phase': name, 'argv': argv, 'returncode': code,
                              'expected_returncode': expected, 'timeout_seconds': timeout,
                              'timed_out': timed_out, 'elapsed_seconds': round(time.monotonic() - started, 3)})
        (self.evidence / 'commands.json').write_text(json.dumps(self.commands, indent=2) + '\n')
        if timed_out or code != expected:
            print(stdout[-12000:].decode(errors='replace'), flush=True)
            print(stderr[-12000:].decode(errors='replace'), flush=True)
        require(not timed_out and code == expected, 'command_failed_or_timeout:' + name)
        return stdout.decode()


def fetch_verified(spec, destination):
    require(spec['url'].startswith('https://'), 'https_public_source_only')
    with urllib.request.urlopen(spec['url'], timeout=45) as response:
        raw = response.read(spec['size'] + 1)
    require(len(raw) == spec['size'] and hashlib.sha256(raw).hexdigest() == spec['sha256'],
            'public_provenance_hash_mismatch:' + spec['name'])
    destination.write_bytes(raw)
    return raw


def execute(recipe):
    require_builder()  # Before filesystem writes, network, Go or synthetic privilege operations.
    source, output = Path('/gosu-build/source'), Path('/out')
    source.mkdir(parents=True, exist_ok=False)
    evidence = output / 'evidence'
    evidence.mkdir(parents=True, exist_ok=False)
    run = Runner(evidence, source)
    (evidence / 'recipe.json').write_text(json.dumps(recipe, indent=2) + '\n')
    shutil.copyfile(HERE / 'pins.json', evidence / 'pins.json')
    require(run(['go', 'env', 'GOVERSION'], 'go-version').strip() == recipe['builder']['go_version'], 'exact_toolchain')
    run(['go', 'env', '-json'], 'go-environment')
    run(['dpkg-query', '-W', '-f=${Package}\t${Version}\n'], 'builder-packages')
    run(['gpgv', '--version'], 'gpgv-version')

    provenance = Path('/gosu-build/original-provenance')
    provenance.mkdir(mode=0o700)
    original = recipe['upstream_binary_provenance']
    for name, spec in original['files'].items():
        fetch_verified(spec, provenance / name)
    require(sha(provenance / 'upstream-gosu-amd64') == recipe['base_binary']['sha256'], 'original_base_binary_binding')
    (provenance / 'signing-key.gpg').write_bytes(dearmor_key((provenance / 'gosu-signing-key.asc').read_bytes()))
    signature = run(['gpgv', '--homedir', str(provenance), '--keyring', str(provenance / 'signing-key.gpg'),
                     '--status-fd', '1', str(provenance / 'gosu-amd64.asc'),
                     str(provenance / 'upstream-gosu-amd64')], 'original-signature')
    require('[GNUPG:] VALIDSIG ' + original['fingerprint'] + ' ' in signature, 'exact_upstream_signer')
    (evidence / 'original-signature-provenance.json').write_text(json.dumps({
        **original, 'verified': True, 'base_binary_sha256': recipe['base_binary']['sha256'],
        'rebuilt_binary_upstream_signed': False}, indent=2) + '\n')

    spec = recipe['source']
    run(['git', 'init', '.'], 'source-init')
    run(['git', 'remote', 'add', 'origin', spec['repository']], 'source-origin')
    run(['git', 'fetch', '--depth=1', 'origin', spec['commit']], 'source-fetch')
    run(['git', 'checkout', '--detach', 'FETCH_HEAD'], 'source-checkout')
    require(run(['git', 'rev-parse', 'HEAD'], 'source-head').strip() == spec['commit'], 'exact_source_commit')
    def source_hashes():
        actual = {name: sha(source / name) for name in spec['files']}
        require(actual == spec['files'], 'unchanged_upstream_source_and_module_files')
        return actual
    source_hashes()
    downloads = json_stream(run(['go', 'mod', 'download', '-json'], 'module-download', timeout=300))
    resolved = {row['Path']: row for row in downloads}
    require(set(resolved) == set(recipe['modules']), 'only_original_pinned_modules')
    for name, pin in recipe['modules'].items():
        verify_module(pin, resolved[name])
    run(['go', 'mod', 'verify'], 'module-verify')
    modules = json_stream(run(['go', 'list', '-mod=readonly', '-m', '-json', 'all'], 'effective-modules'))
    require({row['Path']: row.get('Version') for row in modules if not row.get('Main')}
            == {name: pin['version'] for name, pin in recipe['modules'].items()}, 'effective_graph_unchanged')
    (evidence / 'effective-modules.json').write_text(json.dumps(modules, indent=2) + '\n')
    # Meaningful upstream dependency parser unit tests; gosu itself has no Go *_test.go suite.
    run(['go', 'test', '-mod=readonly', '-short', '-count=1', '-p=2', '-timeout=3m',
         'github.com/moby/sys/user'], 'user-parser-unit-tests', timeout=240)
    binary = output / 'gosu'
    run(['go', 'build', '-mod=readonly', '-v', '-trimpath', '-ldflags=-d -w',
         '-buildvcs=true', '-o', str(binary), '.'], 'build-gosu', timeout=600)
    buildinfo = run(['go', 'version', '-m', str(binary)], 'binary-buildinfo')
    require(buildinfo.splitlines()[0].endswith(recipe['builder']['go_version']), 'binary_toolchain')
    for expected in ['CGO_ENABLED=0', 'GOOS=linux', 'GOARCH=amd64', 'GOAMD64=v1',
                     'vcs.revision=' + spec['commit'], 'vcs.modified=false']:
        require(expected in buildinfo, 'binary_build_setting:' + expected)
    for name, pin in recipe['modules'].items():
        require('\tdep\t' + name + '\t' + pin['version'] + '\t' + pin['sum'] in buildinfo,
                'binary_module_pin:' + name)
    symbols = run(['go', 'tool', 'nm', str(binary)], 'binary-symbols')
    require(' main.main' in symbols and ' main.SetupUser' in symbols, 'unstripped_runtime_symbols')
    identity = elf_identity(binary)
    version = run([str(binary), '--version'], 'binary-version').strip()
    require(version == '1.19 (' + recipe['builder']['go_version'] + ' on linux/amd64; gc)', 'gosu_source_version')

    # These names/IDs exist only in this disposable builder, never a host or DB volume.
    require(not any(u.pw_uid in (46001, 46003) or u.pw_name == 'map_gosu_fixture'
                    for u in pwd.getpwall()), 'synthetic_uid_collision')
    require(not any(g.gr_gid in (46001, 46002, 46004)
                    or g.gr_name in ('map_gosu_primary', 'map_gosu_extra')
                    for g in grp.getgrall()), 'synthetic_gid_collision')
    run(['groupadd', '-g', '46001', 'map_gosu_primary'], 'fixture-primary-group')
    run(['groupadd', '-g', '46002', 'map_gosu_extra'], 'fixture-extra-group')
    run(['useradd', '-M', '-u', '46001', '-g', '46001', '-G', '46002',
         '-d', '/tmp/map-gosu-home', '-s', '/bin/sh', 'map_gosu_fixture'], 'fixture-user')
    cases = []
    for label, user_spec, uid, gid, groups, home in [
        ('named', 'map_gosu_fixture', '46001', '46001', {'46001', '46002'}, '/tmp/map-gosu-home'),
        ('explicit-group', '46001:46002', '46001', '46002', {'46002'}, '/tmp/map-gosu-home'),
        ('unmapped-numeric', '46003:46004', '46003', '46004', {'46004'}, '/')]:
        text = run([str(binary), user_spec, '/bin/sh', '-c',
                    'id -u; id -g; id -G; printf "%s\\n" "$HOME"'], 'smoke-' + label).splitlines()
        require(len(text) == 4 and text[0] == uid and text[1] == gid
                and set(text[2].split()) == groups and text[3] == home, 'identity_groups_home:' + label)
        cases.append({'case': label, 'uid': uid, 'gid': gid, 'supplementary_groups': sorted(groups), 'home': home})
    for index, bad in enumerate(['map_gosu_unknown', '0day', '0:map_gosu_unknown', '0:0day']):
        run([str(binary), bad, '/bin/true'], 'smoke-reject-user-' + str(index), expected=1)
    run([str(binary), 'map_gosu_fixture', '/bin/sh', '-c', 'exit 37'], 'smoke-child-exit-code', expected=37)
    run([str(binary), 'map_gosu_fixture', '/does-not-exist'], 'smoke-missing-command', expected=1)
    started = time.monotonic()
    proc = subprocess.Popen([str(binary), 'map_gosu_fixture', '/bin/sh', '-c', 'printf "%s\\n" "$$"'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill(); stdout, stderr = proc.communicate()
        print((stdout + stderr)[-12000:].decode(errors='replace'), flush=True)
        raise ValueError('process_replacement_timeout')
    require(proc.returncode == 0 and stdout.strip() == str(proc.pid).encode(), 'exec_preserves_child_pid')
    cases.append({'case': 'exec_replaces_gosu', 'pid_equal': True, 'exit_code': proc.returncode,
                  'elapsed_seconds': round(time.monotonic() - started, 3)})
    # Preserve upstream refusal of unsafe setuid/setgid installs. Copies never enter /out.
    for mode, word in [(0o4755, 'setuid'), (0o2755, 'setgid')]:
        guard_copy = Path('/gosu-build') / ('guard-' + word)
        shutil.copyfile(binary, guard_copy); guard_copy.chmod(mode)
        run([str(guard_copy), '--version'], 'smoke-refuse-' + word, expected=1)
        require(word in (evidence / ('smoke-refuse-' + word + '.stderr')).read_text(), 'mode_guard_reason')
    source_hashes()
    require(run(['git', 'status', '--porcelain'], 'final-source-status').strip() == '', 'no_source_or_lock_mutation')
    patch = run(['git', 'diff', '--binary', spec['commit']], 'source-diff')
    require(patch == '', 'source_patch_must_be_empty_toolchain_only')
    (evidence / 'source.patch').write_text(patch)
    (evidence / 'source-sha256.json').write_text(json.dumps(source_hashes(), indent=2) + '\n')
    binary.chmod(0o755)
    summary = {'schema_version': 1, 'built_at_utc': datetime.now(timezone.utc).isoformat(),
               'candidate_source_sha': recipe['source_sha'], 'upstream_source_sha': spec['commit'],
               'builder': recipe['builder'], 'binary': {'sha256': sha(binary), 'bytes': binary.stat().st_size,
               'mode': '0755', 'version': version, **identity}, 'synthetic_runtime_checks': cases,
               'original_signature_verified': True, 'rebuilt_binary_upstream_signed': False,
               'module_or_source_changes': [], 'strict_scan_executed_here': False,
               'candidate_security_approved': False, 'gcp_deployed': False}
    (evidence / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    paths = list(evidence.iterdir())
    require(all(p.is_file() and not p.is_symlink() and p.stat().st_size < MAX_EVIDENCE_FILE for p in paths),
            'bounded_regular_evidence_only')
    require(sum(p.stat().st_size for p in paths) < 64 * 1024 * 1024, 'bounded_total_evidence')
    (evidence / 'SHA256SUMS.json').write_text(json.dumps({p.name: sha(p) for p in sorted(paths)}, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--execute-builder', action='store_true')
    args = parser.parse_args()
    recipe = plan(args.source_sha)
    if args.execute_builder:
        execute(recipe)
    else:
        print(json.dumps(recipe, indent=2))


if __name__ == '__main__':
    main()
