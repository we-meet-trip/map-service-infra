#!/usr/bin/env python3
"""Verify the reviewed Caddy transfer artifact and install it without registry pulls.

Imports an image only; never starts a serving stack or changes a running container.
The tracked manifest is the trust anchor. Replacing it requires a fresh image review.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('caddy_security', ROOT / 'scripts/verify-caddy-security.py')
security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security)
MANIFEST = ROOT / 'docker/caddy-security/install-artifact-20260906.json'


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_archive_identity(archive, manifest):
    """Prove the config ID through the reviewed OCI index and platform manifest.

    containerd exposes the index/platform manifest IDs; classic Docker exposes the
    image config digest. They are different hashes, not interchangeable assertions.
    Read bounded JSON members only and never extract the archive onto the host.
    """
    with tarfile.open(archive, 'r:') as saved:
        def document(digest):
            security.require(re.fullmatch(r'sha256:[a-f0-9]{64}', digest), 'invalid archive identity')
            path = 'blobs/' + digest.replace(':', '/')
            matches = [m for m in saved.getmembers() if m.name == path]
            security.require(len(matches) == 1 and matches[0].isfile() and matches[0].size <= 1048576,
                             'unique bounded OCI identity document required')
            raw = saved.extractfile(matches[0]).read()
            security.require(hashlib.sha256(raw).hexdigest() == digest[7:], 'OCI identity checksum mismatch')
            return json.loads(raw)

        index = document(manifest['source_image_id'])
        candidates = [entry for entry in index.get('manifests', [])
                      if entry.get('digest') == manifest['platform_image_id']]
        security.require(len(candidates) == 1 and candidates[0].get('platform') ==
                         {'architecture': 'amd64', 'os': 'linux'}, 'reviewed platform not bound to index')
        platform = document(manifest['platform_image_id'])
        config_id = platform.get('config', {}).get('digest')
        security.require(config_id == manifest['config_image_id'], 'reviewed config not bound to platform')
        config = document(config_id)
        security.require(config.get('os') == 'linux' and config.get('architecture') == 'amd64',
                         'archive config platform mismatch')


def reviewed_identity(actual, manifest):
    # metadata() also proves linux/amd64 and re-inspects through the immutable ID.
    # Both accepted identities belong to the same reviewed archive chain above.
    return actual['platform_image_id'] in {manifest['platform_image_id'], manifest['config_image_id']}


def verify_files(archive, report):
    manifest = json.loads(MANIFEST.read_text())
    security.require(manifest['schema_version'] == 1 and manifest['platform'] == 'linux/amd64', 'unsupported artifact contract')
    for path in (archive, report):
        security.require(path.is_file() and not path.is_symlink(), 'regular artifact files required')
    security.require(archive.stat().st_size == manifest['archive_bytes'], 'archive size mismatch')
    security.require(checksum(archive) == manifest['archive_sha256'], 'archive checksum mismatch')
    security.require(checksum(report) == manifest['report_sha256'], 'scan report checksum mismatch')
    security.verify_report(json.loads(report.read_text()), manifest['source_image_id'], manifest['platform_image_id'])
    verify_archive_identity(archive, manifest)
    return manifest


def install(archive, manifest, output):
    security.require(not output.exists() and not output.is_symlink(), 'compose output already exists')
    # No archive extraction in the host filesystem, mutable tag pull, or service update.
    security.run(['docker', 'image', 'load', '--input', str(archive)], timeout=300)
    actual = security.metadata(manifest['image'])
    security.require(reviewed_identity(actual, manifest), 'loaded platform image mismatch')
    security.verify_buildinfo(security.run(security.isolated(actual['id'], 'cat') + ['/usr/share/map-caddy-build/buildinfo.txt']))
    expected = security.run(security.isolated(actual['id'], 'cat') + ['/usr/share/map-caddy-build/binary.sha256']).split()[0]
    installed = security.run(security.isolated(actual['id'], 'sha256sum') + ['/usr/bin/caddy']).split()[0]
    security.require(bool(re.fullmatch('[a-f0-9]{64}', expected)) and expected == installed, 'installed binary checksum mismatch')
    runtime = security.runtime_checks(actual['id'])
    with output.open('x') as stream:
        stream.write('services:\n  edge:\n    image: ' + actual['id'] +
                     '\n    platform: linux/amd64\n    pull_policy: never\n')
    return {**actual, 'runtime_checks': runtime}


def verify_compose(output):
    security.require(output.is_file() and not output.is_symlink(), 'regular compose override required')
    content = output.read_text()
    match = re.fullmatch(r'services:\n  edge:\n    image: (sha256:[a-f0-9]{64})\n    platform: linux/amd64\n    pull_policy: never\n', content)
    security.require(match is not None, 'unexpected edge override content')
    actual = security.metadata(match[1])
    manifest = json.loads(MANIFEST.read_text())
    security.require(reviewed_identity(actual, manifest), 'edge override is not the reviewed image')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--install-compose', type=Path)
    parser.add_argument('--verify-compose', type=Path)
    args = parser.parse_args()
    try:
        if args.verify_compose:
            security.require(not args.archive and not args.report and not args.install_compose, 'conflicting operation')
            actual = verify_compose(args.verify_compose)
            print(json.dumps({'status': 'edge_override_verified', 'platform_image_id': actual['platform_image_id']}))
            return 0
        security.require(args.archive is not None and args.report is not None, 'archive and report required')
        manifest = verify_files(args.archive, args.report)
        result = {'status': 'artifact_verified', 'archive_sha256': manifest['archive_sha256'],
                  'platform_image_id': manifest['platform_image_id'], 'serving_changes': 0}
        if args.install_compose:
            actual = install(args.archive, manifest, args.install_compose)
            result.update(status='installed_and_smoke_verified', image_id=actual['id'],
                          runtime_checks=actual['runtime_checks'])
        print(json.dumps(result))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print('Caddy artifact verification/install failed; serving was not changed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
