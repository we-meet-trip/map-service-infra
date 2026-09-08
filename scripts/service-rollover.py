#!/usr/bin/env python3
"""Replace one service's container without dropping a request.

A second container is started from the new image beside the one that is serving.
Only when it answers its own health check does the proxy start sending new
requests to it; requests already in flight finish against the old container.
The canonical container is then recreated from the new image, traffic returns to
it, and the temporary container is stopped.

Nothing here removes a volume, a database container or another service. If any
step fails, the temporary container is removed and the proxy keeps pointing at
whatever was already serving, so the deployment ends with the old version still
answering rather than with nothing answering.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SERVICES = {
    'hub': {'port': 8000, 'path': '/health/ready', 'variable': 'hub_upstream'},
    'agent': {'port': 8000, 'path': '/health/ready', 'variable': 'agent_upstream'},
    'yolo': {'port': 8000, 'path': '/health', 'variable': 'vision_upstream'},
    'user': {'port': 8080, 'path': '/actuator/health', 'variable': 'bff_upstream'},
}
SUFFIX = '-rollover'
LABEL = 'kr.mapservice.rollover'
IMAGE = re.compile(r'ghcr\.io/we-meet-trip/map-service-[a-z]+@sha256:[a-f0-9]{64}')
ID = re.compile(r'[a-f0-9]{64}')
NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}')
PROTECTED_RESERVE = 512 * 1024 ** 2
DURATION = re.compile(r'(?:([0-9]+)h)?(?:([0-9]+)m)?(?:([0-9.]+)s)?')
STOP_SECONDS = 30
DISK_FLOOR = 2 * 1024 ** 3
ENV = {'PATH': '/usr/bin:/bin:/usr/local/bin', 'DOCKER_HOST': 'unix:///var/run/docker.sock',
       'DOCKER_CONFIG': '/var/empty'}


class RolloverError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise RolloverError(code)


def docker(args, timeout=60):
    try:
        result = subprocess.run(['docker', *args], env=ENV, capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise RolloverError('docker_command_unavailable') from None
    require(result.returncode == 0, 'docker_command_failed')
    return result.stdout.strip()


def container_id(project, service):
    ids = docker(['ps', '-q', '--no-trunc', '--filter', f'label=com.docker.compose.project={project}',
                  '--filter', f'label=com.docker.compose.service={service}']).splitlines()
    require(len(ids) == 1 and ID.fullmatch(ids[0]), 'one_running_container_required:' + service)
    return ids[0]


def inspect(cid, template):
    return docker(['inspect', '--format', template, cid])


def state(cid):
    return json.loads(inspect(cid, '{"id":{{json .Id}},"image":{{json .Image}},'
                                  '"running":{{json .State.Running}},'
                                  '"health":{{if .State.Health}}{{json .State.Health.Status}}'
                                  '{{else}}"none"{{end}}}'))


def address(cid):
    for candidate in inspect(cid, '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}').split():
        if candidate and not candidate.startswith('127.'):
            return candidate
    raise RolloverError('container_has_no_address')


def memory_available():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    raise RolloverError('memory_reading_unavailable')


SCALE = {'B': 1, 'KIB': 1024, 'MIB': 1024 ** 2, 'GIB': 1024 ** 3,
         'KB': 1000, 'MB': 1000 ** 2, 'GB': 1000 ** 3}


def working_set(cid):
    raw = docker(['stats', '--no-stream', '--format', '{{.MemUsage}}', cid])
    found = re.match(r'([0-9.]+)\s*([A-Za-z]+)', raw.split('/')[0].strip())
    require(found is not None, 'memory_usage_unreadable')
    value, unit = found.groups()
    require(unit.upper() in SCALE, 'memory_usage_unreadable')
    return int(float(value) * SCALE[unit.upper()])


def admission(cid):
    """Refuse to start a second copy when the host cannot hold one."""
    used = working_set(cid)
    needed = int(used * 1.5) + PROTECTED_RESERVE
    available = memory_available()
    free = shutil.disk_usage('/').free
    report = {'working_set_bytes': used, 'required_bytes': needed,
              'memory_available_bytes': available, 'disk_free_bytes': free}
    require(available >= needed, 'insufficient_memory_for_second_container')
    require(free >= DISK_FLOOR, 'insufficient_disk')
    return report


def duration(raw):
    found = DURATION.fullmatch(str(raw).strip())
    require(found is not None and any(found.groups()), 'duration_unreadable')
    hours, minutes, seconds = (float(part or 0) for part in found.groups())
    return int(hours * 3600 + minutes * 60 + seconds)


def stop_seconds(entry):
    """How long this service is given to finish what it is doing.

    A service that is allowed minutes to drain must get those minutes as the
    temporary copy goes away, or the requests still running on it are cut.
    """
    raw = entry.get('stop_grace_period')
    if not raw:
        return STOP_SECONDS
    return max(STOP_SECONDS, duration(raw))


def service_config(config, service):
    try:
        entry = config['services'][service]
        image = entry['image']
    except (KeyError, TypeError):
        raise RolloverError('service_missing_from_configuration') from None
    require(isinstance(image, str) and IMAGE.fullmatch(image), 'service_image_not_pinned')
    # The temporary copy is started from the image, so a service whose process is
    # named in the configuration rather than the image would quietly run
    # something else. Refuse instead of replacing it with the wrong program.
    require(not entry.get('entrypoint') and not entry.get('command'),
            'service_overrides_its_own_process')
    return entry, image


def health_args(entry):
    """Give the temporary copy the same readiness test as the original.

    Compose holds this test, not the image, so a copy created without it is
    called ready the moment it starts and is asked for an answer it cannot give
    yet. A test this cannot reproduce exactly is refused rather than dropped.
    """
    check = entry.get('healthcheck') or {}
    test = check.get('test')
    if not test or check.get('disable'):
        return []
    require(isinstance(test, list) and test and test[0] in ('CMD-SHELL', 'NONE'),
            'healthcheck_cannot_be_reproduced')
    if test[0] == 'NONE':
        return ['--no-healthcheck']
    require(len(test) == 2 and isinstance(test[1], str), 'healthcheck_cannot_be_reproduced')
    args = ['--health-cmd', test[1]]
    for key, flag in (('interval', '--health-interval'), ('timeout', '--health-timeout'),
                      ('start_period', '--health-start-period')):
        if check.get(key):
            args += [flag, '%ds' % duration(check[key])]
    if check.get('retries'):
        args += ['--health-retries', str(int(check['retries']))]
    return args


def create_args(name, project, service, image, entry, networks, stop=STOP_SECONDS):
    """Build the temporary container from the same rendered configuration.

    Published ports are deliberately dropped: the canonical container owns them
    and a second binding would fail. Restart is off so a failure stays visible.
    """
    require(NAME.fullmatch(name), 'rollover_name_invalid')
    args = ['create', '--pull=never', '--name', name, '--restart=no', '--init',
            '--stop-timeout', str(stop),
            '--label', f'{LABEL}=1', '--label', f'kr.mapservice.project={project}',
            '--label', f'kr.mapservice.service={service}', '--log-driver=none',
            '--network', networks[0], *health_args(entry)]
    for key, value in sorted((entry.get('environment') or {}).items()):
        args += ['--env', f'{key}={value}' if value is not None else key]
    limits = ((entry.get('deploy') or {}).get('resources') or {}).get('limits') or {}
    if limits.get('memory'):
        args += ['--memory', str(limits['memory'])]
    if limits.get('cpus'):
        args += ['--cpus', str(limits['cpus'])]
    for mount in entry.get('volumes') or []:
        source, target = mount.get('source'), mount.get('target')
        if source and target:
            args += ['--volume', '%s:%s%s' % (source, target, ':ro' if mount.get('read_only') else '')]
    for option in entry.get('security_opt') or []:
        args += ['--security-opt', option]
    for capability in entry.get('cap_drop') or []:
        args += ['--cap-drop', capability]
    args.append(image)
    return args


def upstream_body(service, target):
    variable = SERVICES[service]['variable']
    return ('# Written by a running deployment; removed when it finishes.\n'
            'set $%s http://%s:%d;\n' % (variable, target, SERVICES[service]['port']))


def upstream_source(cid):
    """The host directory the proxy actually reads its overrides from."""
    return inspect(cid, '{{range .Mounts}}{{if eq .Destination "/etc/nginx/upstreams"}}'
                        '{{.Source}}{{end}}{{end}}').strip()


def reload_proxy(project, upstreams, service, target):
    """Point one name at a different address and let nginx pick it up."""
    proxy = container_id(project, 'proxy')
    # Writing where the proxy does not read leaves every reload successful and
    # every request still on the container about to be replaced, which is the
    # one outage this whole procedure exists to avoid.
    mounted = upstream_source(proxy)
    require(mounted and os.path.realpath(mounted) == os.path.realpath(str(upstreams)),
            'proxy_reads_a_different_upstream_directory')
    path = upstreams / (service + '.conf')
    if target is None:
        if path.exists():
            path.unlink()
    else:
        staged = path.with_suffix('.conf.new')
        staged.write_text(upstream_body(service, target))
        os.chmod(staged, 0o644)
        os.replace(staged, path)
    check = subprocess.run(['docker', 'exec', proxy, 'nginx', '-t'], env=ENV,
                           capture_output=True, text=True, timeout=30)
    if check.returncode != 0:
        if path.exists():
            path.unlink()
        raise RolloverError('proxy_configuration_rejected')
    docker(['exec', proxy, 'nginx', '-s', 'reload'], timeout=30)
    return {'service': service, 'target': target or 'canonical', 'proxy': proxy[:12]}


def wait_healthy(cid, seconds):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = state(cid)
        require(last['running'], 'rollover_container_exited')
        if last['health'] in ('healthy', 'none'):
            return last
        time.sleep(2)
    raise RolloverError('rollover_container_never_became_healthy')


def probe(host, service, timeout=5, seconds=0):
    url = 'http://%s:%d%s' % (host, SERVICES[service]['port'], SERVICES[service]['path'])
    deadline = time.monotonic() + seconds
    while True:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except Exception:
            status = 0
        if status == 200 or time.monotonic() >= deadline:
            return status
        time.sleep(2)


def public_ok(probes):
    for url, expected in probes:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except Exception:
            return False, url
        if status != expected:
            return False, url
    return True, ''


def rollover(config, project, service, upstreams, probes, recreate, *,
             health_seconds=180, settle=10, report=None):
    require(service in SERVICES, 'unknown_service')
    entry, image = service_config(config, service)
    drain = stop_seconds(entry)
    image_id = docker(['image', 'inspect', '--format', '{{.Id}}', image])
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', image_id), 'service_image_identity_invalid')

    blue = container_id(project, service)
    before = state(blue)
    require(before['running'], 'canonical_container_not_running')
    report = {} if report is None else report
    report.update({'service': service, 'image': image, 'image_id': image_id,
                   'blue': blue[:12], 'started_at': datetime.now(timezone.utc).isoformat(),
                   'admission': admission(blue), 'drain_seconds': drain, 'steps': []})
    networks = [n for n in inspect(blue, '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}').split()]
    require(networks, 'container_has_no_network')

    name = project + '-' + service + SUFFIX
    stale = docker(['ps', '-aq', '--no-trunc', '--filter', f'name=^{name}$']).splitlines()
    require(not stale, 'previous_rollover_container_present')

    green = None
    switched = False
    try:
        green = docker(create_args(name, project, service, image, entry, networks, drain), timeout=120)
        require(ID.fullmatch(green), 'rollover_container_id_invalid')
        for extra in networks[1:]:
            docker(['network', 'connect', extra, green])
        docker(['start', green], timeout=120)
        report['green'] = green[:12]
        report['steps'].append({'step': 'green_started', 'at': datetime.now(timezone.utc).isoformat()})

        health = wait_healthy(green, health_seconds)
        report['steps'].append({'step': 'green_healthy', 'health': health['health']})
        status = probe(address(green), service, seconds=health_seconds)
        require(status == 200, 'rollover_container_did_not_answer')
        report['steps'].append({'step': 'green_answered', 'status': status})

        report['steps'].append(reload_proxy(project, upstreams, service, name))
        switched = True
        time.sleep(settle)
        ok, failed = public_ok(probes)
        require(ok, 'public_probe_failed_after_switch:' + failed)
        report['steps'].append({'step': 'traffic_on_green'})

        report['steps'].append({'step': 'canonical_recreate_requested'})
        # The canonical container is replaced while the temporary one serves.
        recreate(service)

        canonical = container_id(project, service)
        require(canonical != blue, 'canonical_container_was_not_replaced')
        after = state(canonical)
        require(after['image'] == image_id, 'canonical_image_mismatch')
        wait_healthy(canonical, health_seconds)
        require(probe(address(canonical), service, seconds=health_seconds) == 200,
                'canonical_did_not_answer')
        report['canonical'] = canonical[:12]
        report['steps'].append({'step': 'canonical_healthy'})

        report['steps'].append(reload_proxy(project, upstreams, service, None))
        switched = False
        time.sleep(settle)
        ok, failed = public_ok(probes)
        require(ok, 'public_probe_failed_after_return:' + failed)
        report['steps'].append({'step': 'traffic_on_canonical'})
        report['status'] = 'PASS'
        return report
    except BaseException as error:
        report['status'] = 'FAIL'
        report['error_code'] = str(error) if isinstance(error, RolloverError) else 'rollover_failed'
        # The proxy must never be left pointing at a container we are removing.
        if switched:
            try:
                reload_proxy(project, upstreams, service, None)
                report['steps'].append({'step': 'traffic_returned_after_failure'})
            except Exception:
                report['steps'].append({'step': 'traffic_return_failed'})
                report['error_code'] = 'rollover_failed_with_proxy_pointing_at_temporary'
                raise
        raise
    finally:
        if green is not None and ID.fullmatch(green):
            try:
                if state(green)['running']:
                    docker(['stop', '--time', str(drain), green], timeout=drain + 60)
                docker(['rm', green])
                report['steps'].append({'step': 'green_removed'})
            except Exception:
                report['steps'].append({'step': 'green_removal_failed'})
        report['finished_at'] = datetime.now(timezone.utc).isoformat()


def compose_recreate(compose, service):
    # Compose leaves a container alone when it decides nothing changed, and the
    # caller then finds the canonical container was never replaced. Asking for it
    # explicitly also makes a same-image rehearsal possible.
    subprocess.run([*compose, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                    '--force-recreate', '--wait', '--wait-timeout', '180', service],
                   env={**os.environ, **ENV}, check=True, timeout=600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, choices=sorted(SERVICES))
    parser.add_argument('--project', required=True)
    parser.add_argument('--upstreams', required=True, type=Path)
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--probe', action='append', default=[],
                        help='URL=expected status, checked before and after each switch')
    parser.add_argument('--compose', required=True,
                        help='shell-quoted docker compose invocation for the canonical recreate')
    args = parser.parse_args()
    import shlex
    import sys
    probes = []
    for item in args.probe:
        url, _, expected = item.rpartition('=')
        probes.append((url, int(expected)))
    report = {'status': 'FAIL', 'error_code': 'rollover_not_started', 'service': args.service}
    try:
        require(os.geteuid() == 0, 'root_deployment_operator_required')
        require(args.upstreams.is_dir(), 'upstream_directory_missing')
        raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
        require(len(raw) <= 4 * 1024 * 1024, 'compose_input_too_large')
        config = json.loads(raw)
        compose = shlex.split(args.compose)
        report = {}
        rollover(config, args.project, args.service, args.upstreams, probes,
                 lambda service: compose_recreate(compose, service), report=report)
    except Exception as error:
        report.setdefault('service', args.service)
        report['status'] = 'FAIL'
        report.setdefault('error_code',
                          str(error) if isinstance(error, RolloverError) else 'rollover_failed')
    if not args.receipt.exists() and not args.receipt.is_symlink():
        with args.receipt.open('x') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'admission'}))
    return 0 if report.get('status') == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
