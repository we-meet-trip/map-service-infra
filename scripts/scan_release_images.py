#!/usr/bin/env python3
"""Scan six verified registry digests; preserve evidence and block CRITICAL releases.

The scanner runs without a Docker socket/source mount. Raw HIGH/CRITICAL reports,
image sizes, scanner DB metadata and checksums are retained even for a failed gate.
HIGH findings allow GCP testing, never automatic NCP residual-risk acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import release_manifest as release

SCANNER = 'aquasec/trivy@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969'
VERSION = '0.74.0'
MAX_REPORT = 64 * 1024 * 1024
require = release.require


def invalid_constant(_):
    raise ValueError('non-finite JSON number')


def read_json(path, maximum=MAX_REPORT):
    require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= maximum,
            'invalid audit file')
    return json.loads(path.read_text(), object_pairs_hook=release.unique_object,
                      parse_constant=invalid_constant)


def reference(entry):
    # Called only for validated bundle entries; also safe as a standalone function.
    require(isinstance(entry, dict) and entry.get('image') in
            [f'{release.REGISTRY}/map-service-{s}' for s in release.SERVICES], 'unexpected scan repository')
    digest = entry.get('digest')
    require(isinstance(digest, str) and release.DIGEST.fullmatch(digest), 'immutable digest required')
    return entry['image'] + '@' + digest


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        raise ValueError('invalid scan time') from None
    require(parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0, 'scan time must use UTC')
    return parsed


def parse_report(document, entry, tag, *, started=None):
    expected = reference(entry)
    require(isinstance(document, dict) and type(document.get('SchemaVersion')) is int and document['SchemaVersion'] == 2,
            'unsupported Trivy schema')
    require(document.get('Trivy', {}).get('Version') == VERSION, 'unexpected scanner version')
    require(document.get('ArtifactType') == 'container_image' and document.get('ArtifactName') == expected,
            'scan artifact identity mismatch')
    metadata = document.get('Metadata', {})
    require(isinstance(metadata, dict) and metadata.get('Reference') == expected
            and metadata.get('RepoDigests') == [expected], 'registry reference mismatch')
    config = metadata.get('ImageConfig', {})
    require(config.get('os') == 'linux' and config.get('architecture') == 'amd64', 'scan platform mismatch')
    labels = config.get('config', {}).get('Labels', {})
    require(labels.get('org.opencontainers.image.revision') == entry['source_sha'], 'scan source SHA mismatch')
    require(labels.get('org.opencontainers.image.source') in
            (f"https://github.com/{entry['source_repo']}", f"https://github.com/{entry['source_repo']}.git"),
            'scan source repository mismatch')
    require(labels.get('org.opencontainers.image.version') == tag, 'scan release tag mismatch')
    image_id = metadata.get('ImageID')
    require(isinstance(image_id, str) and release.DIGEST.fullmatch(image_id), 'invalid image config ID')
    size = metadata.get('Size')
    require(type(size) is int and 0 < size <= 100 * 1024**3, 'invalid image size')
    created = timestamp(document.get('CreatedAt'))
    if started is not None:
        require(started - timedelta(seconds=60) <= created <= datetime.now(timezone.utc) + timedelta(minutes=5),
                'stale or future scan report')
    results = document.get('Results')
    require(isinstance(results, list) and results, 'missing scanner results')
    require(any(isinstance(r, dict) and r.get('Class') == 'os-pkgs' for r in results), 'OS package scan missing')
    language = 'jar' if entry['image'].endswith('/map-service-user') else 'python-pkg'
    if not entry['image'].endswith('/map-service-admin-web'):
        require(any(r.get('Class') == 'lang-pkgs' and r.get('Type') == language for r in results if isinstance(r, dict)),
                'language package scan missing')
    counts = {'HIGH': 0, 'CRITICAL': 0}
    cves = set()
    for result in results:
        require(isinstance(result, dict) and result.get('Class') in ('os-pkgs', 'lang-pkgs'), 'unexpected scan class')
        vulnerabilities = result.get('Vulnerabilities', [])
        require(isinstance(vulnerabilities, list), 'invalid vulnerability list')
        for finding in vulnerabilities:
            require(isinstance(finding, dict) and finding.get('Severity') in counts, 'unexpected finding severity')
            for field in ('VulnerabilityID', 'PkgName', 'InstalledVersion'):
                require(isinstance(finding.get(field), str) and bool(finding[field]), 'invalid vulnerability record')
            counts[finding['Severity']] += 1
            cves.add(finding['VulnerabilityID'])
    return {'reference': expected, 'source_sha': entry['source_sha'], 'image_config_id': image_id,
            'trivy_metadata_size_bytes': size, 'scan_created_at': document['CreatedAt'],
            'counts': counts, 'unique_vulnerability_ids': sorted(cves)}


def command(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=960, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('scanner command failed') from None
    # Registry credentials, scanner stderr and arbitrary findings are never echoed.
    require(p.returncode == 0, 'scanner command failed')


def docker_scan_args(ref, output, cache, service):
    require(service in release.SERVICES, 'unexpected scan service')
    image, marker, digest = ref.partition('@')
    require(marker == '@' and image == f'{release.REGISTRY}/map-service-{service}'
            and reference({'image': image, 'digest': digest}) == ref, 'invalid scanner input')
    return ['docker', 'run', '--rm', '--platform', 'linux/amd64', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true', '--memory', '1536m', '--cpus', '2',
            '--user', f'{os.getuid()}:{os.getgid()}',
            '-v', str((cache / 'scratch').resolve()) + ':/tmp',
            '-e', 'TRIVY_USERNAME', '-e', 'TRIVY_PASSWORD',
            '-v', str(cache.resolve()) + ':/cache', '-v', str(output.resolve()) + ':/reports',
            SCANNER, 'image', '--cache-dir', '/cache', '--image-src', 'remote', '--platform', 'linux/amd64',
            '--timeout', '15m', '--no-progress', '--scanners', 'vuln', '--severity', 'HIGH,CRITICAL',
            '--ignore-unfixed=false', '--ignorefile', '/dev/null', '--list-all-pkgs', '--format', 'json',
            '--output', '/reports/' + service + '.json', ref]


def write_json(path, data):
    require(not path.exists() and not path.is_symlink(), 'audit output already exists')
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def checksums(output):
    names = sorted(p.name for p in output.iterdir() if p.name != 'SHA256SUMS')
    require(names and all((output / n).is_file() and not (output / n).is_symlink() for n in names), 'invalid audit files')
    return ''.join(f'{hashlib.sha256((output / n).read_bytes()).hexdigest()}  {n}\n' for n in names)


def summarize(data, output, services, started, completed):
    counts = {level: sum(row['counts'][level] for row in services.values()) for level in ('HIGH', 'CRITICAL')}
    status = ('SCAN_INCOMPLETE' if not completed else 'BLOCK_CRITICAL' if counts['CRITICAL'] else
              'FINDINGS_REQUIRE_REVIEW' if counts['HIGH'] else 'NO_HIGH_CRITICAL_IN_SCAN_SCOPE')
    return {'schema_version': 1, 'completed': completed, 'github_run_id': data['github_run_id'],
            'release_manifest_sha256': hashlib.sha256((output / 'scanned-release.json').read_bytes()).hexdigest(),
            'scanner': SCANNER, 'scanner_version': VERSION, 'image_source': 'remote', 'platform': 'linux/amd64',
            'scanners': ['vuln'], 'severities': ['HIGH', 'CRITICAL'], 'ignore_unfixed': False,
            'started_at': started.isoformat(), 'services': services, 'counts': counts, 'security_status': status,
            'gcp_test_gate': completed and counts['CRITICAL'] == 0,
            'ncp_residual_risk_accepted': False, 'store_release_approved': False}


def scan_bundle(bundle, output, cache, github_summary=None):
    data = release.verify_bundle(bundle)
    require(not output.exists() and not output.is_symlink(), 'fresh audit directory required')
    require(not cache.is_symlink() and cache.resolve() != output.resolve(), 'invalid cache path')
    output.mkdir(mode=0o700, parents=False)
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    (cache / 'scratch').mkdir(mode=0o700, exist_ok=True)
    (output / 'scanned-release.json').write_bytes((bundle / 'release.json').read_bytes())
    started = datetime.now(timezone.utc)
    services = {}
    completed = False
    try:
        for service in release.SERVICES:
            entry = data['services'][service]
            command(docker_scan_args(reference(entry), output, cache, service))
            raw = output / (service + '.json')
            row = parse_report(read_json(raw), entry, data['release_tag'], started=started)
            row['report_sha256'] = hashlib.sha256(raw.read_bytes()).hexdigest()
            services[service] = row
        db = read_json(cache / 'db/metadata.json', 65536)
        require(type(db.get('Version')) is int and db['Version'] > 0, 'invalid scanner DB metadata')
        timestamp(db.get('UpdatedAt'))
        write_json(output / 'scanner-db-metadata.json', db)
        completed = True
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        # Persist partial reports with an explicit incomplete gate, never success.
        raise ValueError('exact registry image scan incomplete') from None
    finally:
        summary = summarize(data, output, services, started, completed)
        write_json(output / 'summary.json', summary)
        (output / 'SHA256SUMS').write_text(checksums(output))
        if github_summary:
            with github_summary.open('a') as stream:
                stream.write('Exact registry image vulnerability audit\n\n```json\n' + json.dumps({
                    'counts': summary['counts'], 'security_status': summary['security_status'],
                    'gcp_test_gate': summary['gcp_test_gate'], 'ncp_residual_risk_accepted': False}, indent=2) +
                    '\n```\n\nCI/build success is not NCP or store security approval.\n')
    return summary


def enforce(bundle, output):
    data = release.verify_bundle(bundle)
    required_files = {s + '.json' for s in release.SERVICES} | {
        'scanned-release.json', 'summary.json', 'scanner-db-metadata.json', 'SHA256SUMS'}
    require({p.name for p in output.iterdir()} == required_files, 'incomplete audit file inventory')
    summary = read_json(output / 'summary.json', 1024 * 1024)
    require(summary.get('completed') is True, 'scan incomplete')
    sums = output / 'SHA256SUMS'
    require(sums.is_file() and not sums.is_symlink() and sums.stat().st_size <= 4096
            and sums.read_text() == checksums(output), 'audit checksum mismatch')
    require((output / 'scanned-release.json').read_bytes() == (bundle / 'release.json').read_bytes(), 'scanned release changed')
    started = timestamp(summary.get('started_at'))
    services = {}
    for service in release.SERVICES:
        raw = output / (service + '.json')
        row = parse_report(read_json(raw), data['services'][service], data['release_tag'])
        row['report_sha256'] = hashlib.sha256(raw.read_bytes()).hexdigest()
        services[service] = row
    expected = summarize(data, output, services, started, True)
    require(summary == expected, 'audit summary mismatch')
    require(summary['counts']['CRITICAL'] == 0, 'CRITICAL findings block release bundle publication')
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['scan', 'enforce'])
    p.add_argument('--bundle', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--cache', type=Path)
    p.add_argument('--github-summary', type=Path)
    args = p.parse_args(argv)
    try:
        if args.command == 'scan':
            require(args.cache is not None, 'cache required')
            scan_bundle(args.bundle, args.output, args.cache, args.github_summary)
        else:
            enforce(args.bundle, args.output)
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        print('Release image audit failed; inspect retained audit artifacts.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
