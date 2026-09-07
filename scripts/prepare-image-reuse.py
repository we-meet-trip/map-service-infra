#!/usr/bin/env python3
"""Reuse recently verified immutable runtime layers only for unchanged source SHAs."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import release_manifest as release


def plan(bundle, sources, *, now=None):
    data = release.verify_bundle(bundle)
    created = datetime.fromisoformat(data['created_at'].replace('Z', '+00:00'))
    now = now or datetime.now(timezone.utc)
    release.require(created.tzinfo is not None and 0 <= (now - created).total_seconds() <= 86400,
                    'reuse release must be no older than 24 hours')
    result = {}
    for service in release.SERVICES:
        repo = 'admin' if service == 'admin-web' else service
        folder = sources / ('map-service-' + repo)
        sha = release.run('git', '-C', str(folder), 'rev-parse', 'HEAD')
        release.require(release.SHA.fullmatch(sha), 'source SHA required')
        release.run('git', '-C', str(folder), 'diff', '--exit-code', 'HEAD', '--')
        release.require(not release.run('git', '-C', str(folder), 'ls-files', '--others', '--exclude-standard'),
                        'untracked source files forbidden for image reuse')
        entry = data['services'][service]
        if sha != entry['source_sha']:
            continue
        # Revalidate the registry descriptor/platform and original OCI labels;
        # an artifact alone is not proof of the image currently addressed.
        release.verify_registry_image(entry, data['release_tag'])
        result[service] = {'reference': entry['image'] + '@' + entry['digest'],
                           'source_sha': sha, 'origin_run': data['github_run_id']}
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--source-root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    try:
        entries = plan(args.bundle, args.source_root)
        args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        for service, entry in entries.items():
            directory = args.output / service
            directory.mkdir(mode=0o700)
            (directory / 'Dockerfile').write_text('FROM ' + entry['reference'] + '\n')
        (args.output / 'reuse.json').write_text(json.dumps(entries, indent=2) + '\n')
        print(json.dumps({'unchanged_source_images': sorted(entries), 'new_images_require_exact_rescan': True}))
    except (ValueError, OSError, TypeError, KeyError, subprocess.TimeoutExpired) as error:
        print('image reuse rejected (' + type(error).__name__ + ')', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
