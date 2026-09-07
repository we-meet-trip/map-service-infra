#!/usr/bin/env python3
"""Build only Grafana's core executable in a remote Linux candidate builder."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PINS = Path(__file__).with_name('pins.json')


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def patch_module_text(text, pins):
    """Change exactly the two pinned existing requirements, preserving comments."""
    for module, patch in pins['module_patches'].items():
        pattern = re.compile(r'^(\s*' + re.escape(module) + r'\s+)'
                             + re.escape(pins['original_versions'][module])
                             + r'(?=\s|$)', re.MULTILINE)
        text, count = pattern.subn(lambda match: match[1] + patch['version'], text)
        require(count == 1, 'exact_original_requirement:' + module)
    return text


def json_stream(raw):
    decoder = json.JSONDecoder()
    values = []
    while raw.strip():
        raw = raw.lstrip()
        value, end = decoder.raw_decode(raw)
        values.append(value)
        raw = raw[end:]
    return values


def verify_effective_modules(modules, pins):
    for name, expected in pins['module_patches'].items():
        found = [value for value in modules if value.get('Path') == name]
        require(len(found) == 1, 'single_effective_module:' + name)
        require(found[0].get('Version') == expected['version']
                and not found[0].get('Replace'), 'effective_module_version:' + name)


def build(source, output, candidate_sha, pins):
    require(sys.platform == 'linux', 'remote_linux_builder_required')
    require(re.fullmatch(r'[a-f0-9]{40}', candidate_sha), 'candidate_source_sha')
    require(not source.exists() and not output.exists(), 'fresh_builder_paths_required')
    source.mkdir(parents=True)
    output.mkdir(parents=True)
    evidence = output / 'evidence'
    evidence.mkdir()
    # Keep Go's authenticated module checks and prohibit implicit toolchain changes.
    env = os.environ.copy()
    env.update(GOTOOLCHAIN='local', GOSUMDB='sum.golang.org',
               GOPROXY='https://proxy.golang.org', GONOSUMDB='', GOPRIVATE='',
               GOFLAGS='-p=2', GOMAXPROCS='2', CGO_ENABLED='0',
               GOOS='linux', GOARCH='amd64', GOWORK='off')

    def run(args, *, timeout=1800, log=None):
        print(json.dumps({'phase': args[:2]}), flush=True)
        if log:
            with (evidence / log).open('wb') as stream:
                result = subprocess.run(args, cwd=source, env=env, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=timeout)
            if result.returncode:
                tail = (evidence / log).read_text(errors='replace').splitlines()[-60:]
                print('\n'.join(tail), file=sys.stderr)
            require(result.returncode == 0, 'build_command_failed:' + args[0])
            return b''
        result = subprocess.run(args, cwd=source, env=env, capture_output=True,
                                timeout=timeout)
        if result.returncode:
            print(result.stderr.decode(errors='replace')[-8000:], file=sys.stderr)
        require(result.returncode == 0, 'build_command_failed:' + args[0])
        return result.stdout

    require(run(['go', 'version']).decode().strip()
            == 'go version go' + pins['go_version'] + ' linux/amd64', 'pinned_go_version')
    run(['git', 'init', '--quiet'])
    run(['git', 'fetch', '--depth=1', pins['source_repository'], pins['source_commit']])
    run(['git', 'checkout', '--detach', '--quiet', 'FETCH_HEAD'])
    require(run(['git', 'rev-parse', 'HEAD']).decode().strip()
            == pins['source_commit'], 'pinned_grafana_source')
    require(digest(source / 'go.mod') == pins['original_go_mod_sha256'],
            'original_go_mod_checksum')
    env['GOWORK'] = str(source / 'go.work')
    source_epoch = run(['git', 'show', '-s', '--format=%ct', 'HEAD']).decode().strip()
    require(source_epoch.isdigit(), 'source_date_epoch')
    text = (source / 'go.mod').read_text()
    (source / 'go.mod').write_text(patch_module_text(text, pins))

    receipts = {}
    for module, expected in pins['module_patches'].items():
        data = json.loads(run(['go', 'mod', 'download', '-json',
                               module + '@' + expected['version']]))
        require(data.get('Sum') == expected['sum']
                and data.get('GoModSum') == expected['go_mod_sum'],
                'authenticated_module_checksum:' + module)
        receipts[module] = {key: data.get(key) for key in
                            ('Path', 'Version', 'Sum', 'GoModSum', 'Origin')}
    run(['go', 'mod', 'download'], log='module-download.log')
    modules_raw = run(['go', 'list', '-m', '-json', 'all']).decode()
    modules = json_stream(modules_raw)
    verify_effective_modules(modules, pins)
    (evidence / 'effective-modules.json').write_text(json.dumps(modules, indent=2) + '\n')
    (evidence / 'patch-module-receipts.json').write_text(json.dumps(receipts, indent=2) + '\n')
    run(['make', 'build-go', 'GO_BUILD_TAGS=oss', 'WIRE_TAGS=oss',
         'CGO_ENABLED=0', 'OS=linux', 'ARCH=amd64', 'BUILD_VERSION=' + pins['version'],
         'COMMIT_SHA=' + pins['source_commit'], 'BUILD_BRANCH=map-security-core',
         'SOURCE_DATE_EPOCH=' + source_epoch], timeout=3600, log='core-build.log')
    binary = source / 'bin/linux/amd64/grafana'
    require(binary.is_file(), 'core_binary_missing')
    build_info = run(['go', 'version', '-m', str(binary)]).decode()
    for name, expected in pins['module_patches'].items():
        require(re.search(r'^\s*dep\s+' + re.escape(name) + r'\s+'
                          + re.escape(expected['version']) + r'\s', build_info,
                          re.MULTILINE), 'binary_module_version:' + name)
    (evidence / 'core-go-buildinfo.txt').write_text(build_info)
    # Record the complete resulting source change; no dependencies are erased to hide CVEs.
    (evidence / 'source.patch').write_bytes(run(['git', 'diff', '--no-ext-diff']))
    target = output / 'grafana'
    shutil.copyfile(binary, target)
    target.chmod(0o755)
    receipt = {'candidate_source_sha': candidate_sha, 'upstream_source_sha': pins['source_commit'],
               'version': pins['version'], 'source_date_epoch': source_epoch,
               'go_version': pins['go_version'], 'go_builder': pins['go_builder'],
               'binary_sha256': digest(target), 'cgo_enabled': False,
               'catalog_plugins_rebuilt': 0, 'security_approved': False,
               'source_dependency_file_sha256': {
                   name: digest(source / name)
                   for name in ('go.mod', 'go.sum', 'go.work', 'go.work.sum')
                   if (source / name).is_file()},
               'evidence_sha256': {str(p.relative_to(evidence)): digest(p)
                                  for p in evidence.iterdir() if p.is_file()}}
    (evidence / 'build-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--source-dir', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--candidate-source-sha')
    args = parser.parse_args()
    pins = json.loads(PINS.read_text())
    if not args.build:
        print(json.dumps({'status': 'PLAN_ONLY', 'network_calls': 0, 'pins': pins}, indent=2))
        return
    require(args.source_dir and args.output and args.candidate_source_sha, 'explicit_build_arguments')
    build(args.source_dir.resolve(), args.output.resolve(), args.candidate_source_sha, pins)


if __name__ == '__main__':
    main()
