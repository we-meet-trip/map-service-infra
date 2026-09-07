#!/usr/bin/env python3
"""Build a fully inventoried GDAL package without its unused MySQL connector.

Only an explicitly marked disposable Linux image builder may execute. The
default CLI reports the recipe; it cannot run package tools or downloads.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import urllib.error
import zipfile

VERSION = '3.13.2'
PACKAGE_VERSION = '3.13.2+map1'
ASSETS = {
    'source': ('gdal-3.13.2.tar.xz', 9989660,
               '0200b7878d837a7f475ff4070121d0e601f8ef801c2fd83a64294c544f609211'),
    'tests': ('gdalautotest-3.13.2.zip', 23847042,
              'd2cd2af7f164151f40008745c5bf269043a5a0a0c1ab0d2ed5b4a5a55f19a96f'),
}
REQUIRED_DRIVERS = {'GTiff', 'MEM', 'VRT', 'PNG', 'JPEG', 'GeoJSON', 'ESRI Shapefile', 'GPKG'}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def drivers(raw):
    # GDAL/OGR use human-readable short names, including "ESRI Shapefile".
    found = set()
    for line in raw.splitlines():
        match = re.match(r'^\s*(.+?)\s+-[^:]*:\s+', line)
        if match:
            found.add(match.group(1).strip())
    require(len(found) >= 50 and REQUIRED_DRIVERS <= found, 'gdal_driver_inventory_incomplete')
    return found


def verify_driver_delta(before, after):
    require('MySQL' in before and 'MySQL' not in after, 'mysql_driver_must_be_removed')
    require(before - after == {'MySQL'}, 'non_mysql_driver_removed')
    # New drivers may expose additional inputs; do not silently add them either.
    require(not after - before, 'unexpected_new_gdal_driver')
    return {'removed': ['MySQL'], 'added': [], 'preserved_count': len(after)}


def validate_mysql_configuration(cache, targets):
    # GDAL's ogr_dependent_driver uses CMakeDependentOption, which changes a
    # dependency-disabled option from BOOL to INTERNAL. Its value must still be
    # exactly OFF, and no compiled MySQL target may exist.
    values = {}
    for key in ('GDAL_USE_MYSQL', 'OGR_ENABLE_DRIVER_MYSQL'):
        rows = re.findall(r'^' + key + r':([^=\n]+)=([^\n]*)$', cache, re.M)
        require(len(rows) == 1 and rows[0][0] in ('BOOL', 'INTERNAL') and rows[0][1] == 'OFF',
                'mysql_disable_not_effective')
        values[key] = {'type': rows[0][0], 'value': rows[0][1]}
    require('ogr_MySQL' not in targets and 'ogrsf_frmts/mysql/' not in targets, 'mysql_build_target_present')
    return values


def relative(name):
    path = PurePosixPath(name)
    require(name and path.parts and '\\' not in name and not path.is_absolute()
            and '..' not in path.parts and all(ord(c) >= 32 for c in name), 'unsafe_source_path')
    return path


def download(asset, root):
    name, size, expected = ASSETS[asset]
    target = root / name
    url = 'https://github.com/OSGeo/gdal/releases/download/v3.13.2/' + name
    require(not target.exists(), 'upstream_target_already_exists')
    attempts = []
    for attempt in range(1, 4):
        partial = root / (name + '.attempt-' + str(attempt) + '.partial')
        try:
            with urllib.request.urlopen(url, timeout=120) as src, partial.open('xb') as dst:
                total = 0
                while block := src.read(1024 * 1024):
                    total += len(block)
                    require(total <= size, 'upstream_asset_size_limit')
                    dst.write(block)
            # Size/checksum failures are not transient and never retried.
            require(total == size and sha(partial) == expected, 'upstream_asset_checksum_mismatch')
            partial.rename(target)
            attempts.append({'attempt': attempt, 'status': 'PASS'})
            break
        except urllib.error.HTTPError as error:
            code = 'HTTP_' + str(error.code)
            retry = error.code in (408, 429, 500, 502, 503, 504)
        except (urllib.error.URLError, TimeoutError) as error:
            reason = getattr(error, 'reason', error)
            code = 'TLS_VERIFY' if isinstance(reason, ssl.SSLCertVerificationError) else 'TRANSPORT'
            retry = code != 'TLS_VERIFY'
        attempts.append({'attempt': attempt, 'status': code})
        print('gdal_download_attempt=' + json.dumps({'asset': asset, **attempts[-1]}), flush=True)
        require(retry and attempt < 3, 'upstream_download_failed_' + code)
        time.sleep(attempt * 2)
    return target, {'url': url, 'size': size, 'sha256': expected,
                    'attempts': attempts,
                    'authenticity': 'Exact asset digest published by official OSGeo/gdal GitHub release; no detached-signature claim'}


def unpack(source, tests, root):
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        require(sum(x.size for x in members) < 512 * 1024**2, 'source_expansion_limit')
        seen = set()
        for member in members:
            path = relative(member.name)
            require(path.parts[0] == 'gdal-3.13.2' and (member.isfile() or member.isdir()), 'source_member_type')
            require(path not in seen, 'source_duplicate_path')
            seen.add(path)
        archive.extractall(root, filter='data')
    tree = root / 'gdal-3.13.2'
    with zipfile.ZipFile(tests) as archive:
        entries = archive.infolist()
        require(sum(x.file_size for x in entries) < 1024**3, 'test_expansion_limit')
        roots = [str(relative(x.filename).parent.parent) for x in entries
                 if x.filename.endswith('/cpp/CMakeLists.txt') or x.filename == 'cpp/CMakeLists.txt']
        require(len(roots) == 1, 'unique_test_root_required')
        prefix = PurePosixPath(roots[0])
        seen = set()
        for entry in entries:
            path = relative(entry.filename)
            require(path not in seen and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG, stat.S_IFDIR),
                    'test_links_duplicates_forbidden')
            seen.add(path)
            if path == prefix and entry.is_dir():
                continue
            require(path.is_relative_to(prefix), 'test_root_escape')
            target = tree / 'autotest' / path.relative_to(prefix)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
    require((tree / 'autotest/cpp/CMakeLists.txt').is_file(), 'cpp_tests_missing')
    return tree


def build(source_sha):
    require(os.environ.get('MAP_GDAL_NO_MYSQL_BUILDER') == '1'
            and sys.platform == 'linux' and os.getuid() == 0, 'disposable_linux_builder_only')
    require(re.fullmatch(r'[a-f0-9]{40}', source_sha), 'source_sha_required')
    root, evidence, package = Path('/map-gdal-build'), Path('/out/gdal-evidence'), Path('/out/gdal-package')
    require(not root.exists() and not evidence.exists() and not package.exists(), 'fresh_builder_paths_required')
    root.mkdir(); evidence.mkdir(parents=True); package.mkdir(parents=True)
    receipt = {'source_sha': source_sha, 'upstream_version': VERSION, 'package_version': PACKAGE_VERSION,
               'status': 'INCOMPLETE', 'commands': [], 'full_format_inventory_except_mysql_required': True}

    def run(label, argv, *, cwd=root, env=None, timeout=900):
        started = time.monotonic()
        stdout, stderr = evidence / (label + '.stdout'), evidence / (label + '.stderr')
        with stdout.open('xb') as out, stderr.open('xb') as err:
            process = subprocess.run(argv, cwd=cwd, env=env, stdout=out, stderr=err, timeout=timeout)
        receipt['commands'].append({'label': label, 'argv': argv, 'returncode': process.returncode,
                                    'elapsed_seconds': round(time.monotonic()-started, 3)})
        require(process.returncode == 0, 'gdal_command_failed_' + label)
        require(stdout.stat().st_size < 16 * 1024**2 and stderr.stat().st_size < 16 * 1024**2, 'bounded_build_log')
        return stdout.read_text(errors='strict')

    try:
        require(run('native-gdal-version', ['/usr/bin/gdal-config', '--version']).strip() == VERSION,
                'baseline_gdal_version_changed')
        before = drivers(run('baseline-raster-formats', ['/usr/bin/gdalinfo', '--formats'])
                         + run('baseline-vector-formats', ['/usr/bin/ogrinfo', '--formats']))
        source, source_proof = download('source', root)
        tests, tests_proof = download('tests', root)
        receipt['assets'] = {'source': source_proof, 'tests': tests_proof}
        tree = unpack(source, tests, root)
        build_dir = root / 'build'
        hardening_env = os.environ | {'DEB_BUILD_MAINT_OPTIONS': 'hardening=+all'}
        flags = {key: run('hardening-' + key.lower(), ['dpkg-buildflags', '--get', key],
                          env=hardening_env).strip() for key in ('CFLAGS', 'CPPFLAGS', 'CXXFLAGS', 'LDFLAGS')}
        require('-fstack-protector-strong' in flags['CFLAGS'] and '-fstack-protector-strong' in flags['CXXFLAGS']
                and '-D_FORTIFY_SOURCE=' in flags['CPPFLAGS']
                and '-Wl,-z,relro' in flags['LDFLAGS'] and '-Wl,-z,now' in flags['LDFLAGS'],
                'debian_hardening_flags_missing')
        receipt['debian_hardening_flags'] = flags
        run('configure', ['cmake', '-S', str(tree), '-B', str(build_dir), '-G', 'Ninja',
            '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_INSTALL_PREFIX=/usr/local',
            '-DCMAKE_C_FLAGS=' + flags['CPPFLAGS'] + ' ' + flags['CFLAGS'],
            '-DCMAKE_CXX_FLAGS=' + flags['CPPFLAGS'] + ' ' + flags['CXXFLAGS'],
            '-DCMAKE_SHARED_LINKER_FLAGS=' + flags['LDFLAGS'],
            '-DCMAKE_EXE_LINKER_FLAGS=' + flags['LDFLAGS'],
            '-DGDAL_USE_MYSQL:BOOL=OFF', '-DOGR_ENABLE_DRIVER_MYSQL:BOOL=OFF',
            '-DBUILD_PYTHON_BINDINGS=OFF', '-DBUILD_JAVA_BINDINGS=OFF', '-DBUILD_CSHARP_BINDINGS=OFF',
            '-DBUILD_TESTING=ON', '-DUSE_EXTERNAL_GTEST=ON'])
        cache = (build_dir / 'CMakeCache.txt').read_text()
        shutil.copy2(build_dir / 'CMakeCache.txt', evidence / 'CMakeCache.txt')
        targets = run('ninja-target-inventory', ['ninja', '-C', str(build_dir), '-t', 'targets', 'all'])
        receipt['mysql_configuration'] = validate_mysql_configuration(cache, targets)
        run('compile', ['cmake', '--build', str(build_dir), '--parallel', '2'], timeout=3600)
        inventory = json.loads(run('ctest-inventory', ['ctest', '--test-dir', str(build_dir), '--show-only=json-v1']))
        selected = {'test-unit', 'test-float16', 'test-copy-words', 'test-block-cache-4'}
        require(selected <= {x['name'] for x in inventory['tests']}, 'required_upstream_tests_missing')
        run('upstream-tests', ['ctest', '--test-dir', str(build_dir), '--output-on-failure',
            '--no-tests=error', '--timeout', '300', '-R', '^('+'|'.join(sorted(selected))+')$'], timeout=1500)
        env = os.environ | {'DESTDIR': str(package)}
        # Match Debian's normal dh_strip packaging lifecycle: hardening flags
        # include -g for diagnostics, but debug sections do not belong in the
        # serving image. Dynamic symbols needed by the runtime are preserved.
        run('install', ['cmake', '--install', str(build_dir), '--strip'], env=env)
        libdir = package / 'usr/local/lib'
        # Never satisfy the candidate inventory by auto-loading the original
        # distribution's plugin directory still present in this builder.
        env = os.environ | {'LD_LIBRARY_PATH': str(libdir), 'GDAL_DATA': str(package / 'usr/local/share/gdal'),
                            'GDAL_DRIVER_PATH': 'disable'}
        after = drivers(run('candidate-raster-formats', [str(package / 'usr/local/bin/gdalinfo'), '--formats'], env=env)
                        + run('candidate-vector-formats', [str(package / 'usr/local/bin/ogrinfo'), '--formats'], env=env))
        receipt['baseline_drivers'], receipt['candidate_drivers'] = sorted(before), sorted(after)
        receipt['driver_delta'] = verify_driver_delta(before, after)
        libraries = [x for x in libdir.glob('libgdal.so.*') if x.is_file() and not x.is_symlink()]
        require(len(libraries) == 1, 'unique_gdal_runtime_library_required')
        linked = run('runtime-ldd', ['ldd', str(libraries[0])], env=env)
        require('not found' not in linked and 'libmariadb' not in linked and 'libmysql' not in linked,
                'mysql_linkage_or_missing_dependency')
        elf_program = run('runtime-elf-program-headers', ['readelf', '-W', '-l', str(libraries[0])])
        elf_dynamic = run('runtime-elf-dynamic', ['readelf', '-W', '-d', str(libraries[0])])
        elf_symbols = run('runtime-elf-symbols', ['readelf', '-W', '--dyn-syms', str(libraries[0])])
        elf_sections = run('runtime-elf-sections', ['readelf', '-W', '--section-headers', str(libraries[0])])
        require('GNU_RELRO' in elf_program and 'BIND_NOW' in elf_dynamic
                and '__stack_chk_fail' in elf_symbols, 'runtime_elf_hardening_missing')
        require(not re.search(r'\.(?:z)?debug_info\b', elf_sections), 'runtime_debug_sections_not_stripped')
        control_dir = root / 'debian'; control_dir.mkdir()
        (control_dir / 'control').write_text('Source: gdal\nSection: libs\nPriority: optional\nMaintainer: MAP Release <mapadmin26@gmail.com>\nStandards-Version: 4.7.0\n\nPackage: libgdal39\nArchitecture: amd64\nDescription: MAP GDAL runtime with the MySQL driver removed\n')
        deps = run('runtime-package-dependencies', ['dpkg-shlibdeps', '-O', '-e'+str(libraries[0])]).strip()
        require(deps.startswith('shlibs:Depends=') and '\n' not in deps, 'invalid_shlibs_dependency_output')
        deps = deps.split('=', 1)[1]
        require('mariadb' not in deps and 'mysql' not in deps and 'libgdal' not in deps, 'forbidden_package_dependency')
        control = package / 'DEBIAN'; control.mkdir()
        (control / 'control').write_text('Package: libgdal39\nSource: gdal\nVersion: '+PACKAGE_VERSION+'\nArchitecture: amd64\nMaintainer: MAP Release <mapadmin26@gmail.com>\nDepends: '+deps+'\nDescription: Source-built GDAL 3.13.2, original drivers except MySQL\n MAP candidate only; exact upstream and modified build options are receipted.\n')
        # This is a real dpkg-owned shared library package, including its cache
        # lifecycle. Do not rely on builder LD_LIBRARY_PATH in the final image.
        for name in ('postinst', 'postrm'):
            path = control / name
            path.write_text('#!/bin/sh\nset -e\nldconfig\n')
            path.chmod(0o755)
        copyright_dir = package / 'usr/share/doc/libgdal39'; copyright_dir.mkdir(parents=True)
        shutil.copy2('/usr/share/doc/libgdal39/copyright', copyright_dir / 'copyright')
        shutil.copy2(tree / 'LICENSE.TXT', copyright_dir / 'upstream-LICENSE.TXT')
        shutil.copy2(tree / 'third_party/LercLib/NOTICE', copyright_dir / 'Lerc-NOTICE')
        # Match PGDG's repack.patch: these WKT extras are excluded from the
        # distribution source. They were not part of the original image's data.
        for filename in ('cubewerx_extra.wkt', 'ecw_cs.wkt'):
            path = package / 'usr/local/share/gdal' / filename
            require(path.is_file(), 'repack_expected_data_missing')
            path.unlink()
        receipt['distribution_excluded_data_files'] = ['cubewerx_extra.wkt', 'ecw_cs.wkt']
        run('debian-package', ['dpkg-deb', '--root-owner-group', '--build', str(package), '/out/map-gdal.deb'])
        receipt.update(status='PASS', package_sha256=sha('/out/map-gdal.deb'), runtime_dependencies=deps,
                       runtime_library_sha256=sha(libraries[0]))
    finally:
        (evidence / 'build-result.json').write_text(json.dumps(receipt, indent=2)+'\n')
        (evidence / 'SHA256SUMS.json').write_text(json.dumps({str(x.relative_to(evidence)): sha(x)
            for x in evidence.rglob('*') if x.is_file() and x.name != 'SHA256SUMS.json'}, indent=2)+'\n')
        # Failed Docker stages cannot be copied out as candidate evidence. Keep
        # bounded public-source diagnostics in the existing CI build log too.
        print('gdal_build_receipt=' + json.dumps(receipt, sort_keys=True), flush=True)
        if receipt['status'] != 'PASS':
            diagnostic_paths = sorted(evidence.glob('*.stderr'))
            if receipt['commands']:
                diagnostic_paths.append(evidence / (receipt['commands'][-1]['label'] + '.stdout'))
            for path in diagnostic_paths:
                if path.stat().st_size:
                    with path.open('rb') as stream:
                        stream.seek(max(0, path.stat().st_size - 4096))
                        print('gdal_log_tail=' + path.name + ':' + stream.read(4096).decode(errors='replace'), flush=True)
            cache_path = evidence / 'CMakeCache.txt'
            if cache_path.exists():
                print('gdal_mysql_cache=' + json.dumps([line for line in cache_path.read_text().splitlines()
                    if line.startswith(('GDAL_USE_MYSQL:', 'OGR_ENABLE_DRIVER_MYSQL:'))]), flush=True)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-builder', action='store_true')
    parser.add_argument('--source-sha', required=True)
    args = parser.parse_args()
    if not args.execute_builder:
        print(json.dumps({'status': 'PLAN_ONLY', 'mysql_only_removal': True, 'source_sha': args.source_sha}))
        return 0
    build(args.source_sha)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
