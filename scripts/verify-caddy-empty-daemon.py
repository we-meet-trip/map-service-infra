#!/usr/bin/env python3
"""Install the reviewed archive in a disposable, empty local Docker daemon.

Only the uniquely labelled fixture container is stopped. No host socket, bind mount,
existing volume, published port, or network is provided to the nested daemon.
This is a Docker-daemon recovery fixture, not an independent VM/OS boot test.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/install-caddy-artifact.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
DIND = 'docker@sha256:5efed980cba3fc126cf54e21a5a6ff8849d05b6e0623d6e7612f48e9cd6cd17e'
LABEL = 'map.acceptance.caddy-empty-daemon'
SOCKET = 'unix:///run/map-caddy-docker.sock'
CONFIG = '''{
 admin off
 skip_install_trust
}
http://localhost:8080 {
 respond /healthz "ok"
}
https://localhost:8443 {
 tls internal
 respond /healthz "ok"
}
'''


def require(value, message):
    if not value:
        raise ValueError(message)


def host(args, *, data=None, binary=None, timeout=60, check=True):
    result = subprocess.run(['docker', *args], input=data, stdin=binary,
                            capture_output=True, text=binary is None, timeout=timeout)
    if check:
        require(result.returncode == 0, 'docker_command_failed')
    return result


def inventory():
    containers = host(['ps', '-aq']).stdout.split()
    values = json.loads(host(['inspect', *containers]).stdout) if containers else []
    return {v['Id']: (v['Image'], v['State']['Status'], v['RestartCount'],
                     sorted((m.get('Name', ''), m['Destination']) for m in v['Mounts'])) for v in values}


def run(args):
    require(not args.output.exists(), 'evidence_already_exists')
    manifest = installer.verify_files(args.archive, args.report)
    host(['image', 'inspect', DIND])  # Pulling the official digest is a separate explicit step.
    run_id = uuid.uuid4().hex
    name = 'map-caddy-empty-20260906-' + run_id[:10]
    baseline = inventory()
    volumes = set(host(['volume', 'ls', '-q']).stdout.split())
    started = time.monotonic()
    result = {'schema_version': 1, 'scope': 'empty_nested_daemon_not_VM_or_OS',
              'started_at': datetime.now(timezone.utc).isoformat(),
              'dind_digest': DIND, 'archive_sha256': manifest['archive_sha256'],
              'reviewed_identity': {key: manifest[key] for key in ('source_image_id', 'platform_image_id', 'config_image_id')},
              'network': 'none', 'host_socket_mounts': 0, 'host_bind_mounts': 0,
              'existing_volume_mounts': 0, 'published_ports': 0,
              'outer_memory_bytes': 1610612736, 'outer_cpus': 1,
              'outer_storage': '768MiB_tmpfs', 'status': 'RUNNING'}
    created = False
    stage = 'create_empty_daemon'

    def inner(command, *, data=None, timeout=90, check=True):
        return host(['exec', '-i', name, 'docker', '--host', SOCKET, *command],
                    data=data, timeout=timeout, check=check)

    def redirect_docker(command, *, data=None, timeout=90):
        require(command[0] == 'docker', 'unexpected_verifier_command')
        if command[1:4] == ['image', 'load', '--input']:
            require(Path(command[4]).resolve() == args.archive.resolve(), 'unexpected_archive_path')
            with args.archive.open('rb') as stream:
                loaded = host(['exec', '-i', name, 'docker', '--host', SOCKET, 'image', 'load'],
                              binary=stream, timeout=timeout)
            return loaded.stdout.decode().strip()
        return inner(command[1:], data=data, timeout=timeout).stdout.strip()

    def launch_edge(image):
        script = "cat > /tmp/Caddyfile <<'CONFIG'\n" + CONFIG + "CONFIG\nexec caddy run --config /tmp/Caddyfile --adapter caddyfile"
        return inner(['run', '-d', '--rm', '--name', 'caddy-recovery-fixture', '--pull', 'never',
                      '--platform', 'linux/amd64', '--network', 'none', '--read-only',
                      '--security-opt', 'no-new-privileges', '--memory', '128m', '--cpus', '0.5',
                      '--pids-limit', '64', '--tmpfs', '/tmp', '--volume', 'fixture-caddy-data:/data',
                      '--volume', 'fixture-caddy-config:/config', '--entrypoint', 'sh', image, '-c', script]).stdout.strip()

    def trusted_health():
        ca = installer.security.INTERNAL_CA_PATH
        for _ in range(20):
            checked = inner(['exec', 'caddy-recovery-fixture', 'sh', '-c',
                'test -s ' + ca + ' && test "$(curl -fsS --max-time 2 http://localhost:8080/healthz)" = ok && '
                'test "$(curl -fsS --cacert ' + ca + ' --max-time 2 https://localhost:8443/healthz)" = ok'], check=False)
            if checked.returncode == 0:
                return inner(['exec', 'caddy-recovery-fixture', 'sha256sum', ca]).stdout.split()[0]
            time.sleep(.5)
        raise ValueError('trusted_recovery_health_failed')

    try:
        host(['run', '-d', '--rm', '--name', name, '--label', LABEL + '=' + run_id,
              '--privileged', '--cgroupns', 'private', '--network', 'none', '--pull', 'never',
              '--memory', '1536m', '--memory-swap', '1536m', '--cpus', '1', '--pids-limit', '384',
              '--tmpfs', '/var/lib/docker:rw,size=805306368,mode=0700',
              '--tmpfs', '/run:rw,size=67108864,mode=0755', '-e', 'DOCKER_TLS_CERTDIR=',
              DIND, 'dockerd', '--host=' + SOCKET, '--data-root=/var/lib/docker',
              '--exec-root=/run/map-caddy-docker-exec', '--storage-driver=vfs',
              '--iptables=false', '--ip6tables=false', '--bridge=none', '--ip-forward=false', '--ip-masq=false'])
        created = True
        stage = 'wait_empty_daemon'
        for _ in range(30):
            checked = inner(['info', '--format', '{{json .}}'], check=False)
            if checked.returncode == 0:
                info = json.loads(checked.stdout)
                break
            time.sleep(.5)
        else:
            raise ValueError('nested_daemon_unavailable')
        require(info['Containers'] == 0 and info['Images'] == 0, 'daemon_not_empty')
        outer = json.loads(host(['inspect', name]).stdout)[0]
        require(not outer['HostConfig']['Binds'] and not outer['HostConfig']['PortBindings'], 'unexpected_host_sharing')
        require(all(m['Type'] == 'tmpfs' for m in outer['Mounts']), 'non_tmpfs_outer_mount')
        result['initial'] = {'containers': info['Containers'], 'images': info['Images'],
                             'daemon_id': info['ID'], 'version': info['ServerVersion'], 'driver': info['Driver']}
        stage = 'verified_archive_install'
        installer.security.run = redirect_docker
        override = args.output.with_suffix('.compose.yml')
        actual = installer.install(args.archive, manifest, override)
        installer.verify_compose(override)
        result['installed'] = actual
        stage = 'first_service_start'
        first_id = launch_edge(actual['id'])
        first_ca = trusted_health()
        before_volumes = json.loads(inner(['volume', 'inspect', 'fixture-caddy-data', 'fixture-caddy-config']).stdout)
        inner(['stop', '--time', '10', 'caddy-recovery-fixture'])
        stage = 'simulate_rejected_configuration'
        rejected = inner(['run', '--rm', '--pull', 'never', '--platform', 'linux/amd64', '--network', 'none',
                          '--read-only', '--tmpfs', '/data', '--tmpfs', '/config', '--tmpfs', '/tmp',
                          '--entrypoint', 'sh', actual['id'], '-c',
                          'printf "http://localhost:8080 {\\n invalid_fixture_directive\\n}\\n" > /tmp/Caddyfile; caddy run --config /tmp/Caddyfile --adapter caddyfile'], check=False)
        require(rejected.returncode != 0, 'bad_configuration_was_not_rejected')
        stage = 'restore_same_image_and_certificates'
        restored_id = launch_edge(actual['id'])
        restored_ca = trusted_health()
        after_volumes = json.loads(inner(['volume', 'inspect', 'fixture-caddy-data', 'fixture-caddy-config']).stdout)
        restored = json.loads(inner(['inspect', 'caddy-recovery-fixture']).stdout)[0]
        require(first_id != restored_id and restored['Image'] == actual['platform_image_id'], 'restored_image_mismatch')
        require(first_ca == restored_ca and before_volumes == after_volumes, 'fixture_certificate_volume_changed')
        result['recovery'] = {'configuration_failure_exit': rejected.returncode, 'container_replaced': True,
                              'same_image': True, 'same_certificate_CA': True, 'same_fixture_volumes': True,
                              'http': 'pass', 'tls_cacert': 'pass'}
        result['status'] = 'PASS'
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as failure:
        result.update(status='FAIL', failure_stage=stage, failure_type=type(failure).__name__)
        if isinstance(failure, ValueError):
            result['failure_code'] = str(failure)
    finally:
        if created:
            details = host(['inspect', name], check=False)
            if details.returncode == 0:
                own = json.loads(details.stdout)[0]
                require(own['Config']['Labels'].get(LABEL) == run_id, 'cleanup_owner_mismatch')
                result['outer_state'] = {'oom_killed': own['State']['OOMKilled'], 'restart_count': own['RestartCount']}
                host(['stop', '--time', '15', name])
            for _ in range(20):
                if host(['inspect', name], check=False).returncode != 0:
                    result['fixture_removed'] = True
                    break
                time.sleep(.25)
            else:
                result['fixture_removed'] = False
                result['status'] = 'FAIL'
        after = inventory()
        result['host_existing_containers_preserved'] = all(after.get(k) == v for k, v in baseline.items())
        result['host_existing_volumes_preserved'] = volumes <= set(host(['volume', 'ls', '-q']).stdout.split())
        if not result['host_existing_containers_preserved'] or not result['host_existing_volumes_preserved']:
            result['status'] = 'FAIL'
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        with args.output.open('x') as stream:
            json.dump(result, stream, indent=2)
            stream.write('\n')
    print(json.dumps(result))
    return 0 if result['status'] == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
