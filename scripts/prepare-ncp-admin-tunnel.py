#!/usr/bin/env python3
"""Prepare private GCP admin/NCP API-only SSH files; never change Docker or servers."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

TOKENS = ('INTERNAL_SERVICE_TOKEN', 'USER_ADMIN_INTERNAL_TOKEN', 'HUB_ADMIN_INTERNAL_TOKEN')
REMOTE_PORTS = {'user': 8080, 'agent': 8000, 'hub': 8001}
NETWORK = 'map-admin-ncp-tunnel'
INSTALL = '/etc/map-admin-ncp'
ROOT = Path(__file__).resolve().parents[1]


def private_read(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('credential inputs must be private regular files (0600)')
    return path.read_text()


def read_tokens(raw):
    values = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, separator, value = line.partition('=')
        if name not in (*TOKENS, 'APP_ENV'):
            continue
        if not separator or name in values or value != value.strip():
            raise ValueError('production runtime token input is not literal or has duplicate fields')
        values[name] = value
    if values.get('APP_ENV') != 'prod':
        raise ValueError('runtime credentials must explicitly identify APP_ENV=prod')
    selected = {key: values.get(key, '') for key in TOKENS}
    if any(not re.fullmatch(r'[A-Za-z0-9_+/=-]{32,}', value) or value.lower().startswith('replace-')
           for value in selected.values()) or len(set(selected.values())) != 3:
        raise ValueError('three independent production service/admin tokens are required')
    return selected


def addressing(ncp_host, source_ip, cidr, gateway, local_ports, ssh_user, ssh_port):
    for value in (ncp_host, source_ip):
        address = ipaddress.IPv4Address(value)
        if address.is_unspecified or address.is_multicast or address.is_loopback:
            raise ValueError('explicit unicast server/source IPv4 addresses are required')
    network, bridge = ipaddress.IPv4Network(cidr, strict=True), ipaddress.IPv4Address(gateway)
    private = [ipaddress.IPv4Network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if (not any(network.subnet_of(parent) for parent in private) or network.prefixlen > 30
            or bridge not in network or bridge in (network.network_address, network.broadcast_address)):
        raise ValueError('an unused private bridge subnet and usable gateway are required')
    if len(set(local_ports.values())) != 3 or any(not 1024 <= port <= 65535 for port in local_ports.values()):
        raise ValueError('three distinct, available unprivileged local ports are required')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', ssh_user) or ssh_user == 'root':
        raise ValueError('a dedicated non-root NCP SSH username is required')
    if not 1 <= ssh_port <= 65535:
        raise ValueError('invalid NCP SSH port')


def merge_targets(existing, tokens, gateway, local_ports):
    if not isinstance(existing, dict) or not all(isinstance(value, dict) for value in existing.values()):
        raise ValueError('existing ADMIN_TARGETS must be an object of target objects')
    target = {**tokens, 'ADMIN_DATABASE_URL': '', 'ADMIN_REDIS_URL': '',
              **{name.upper() + '_BASE_URL': f'http://{gateway}:{local_ports[name]}' for name in REMOTE_PORTS}}
    if 'prod' in existing and existing['prod'] != target:
        raise ValueError('existing production target differs; preserve and review it before replacement')
    return {**existing, 'prod': target}


def ssh_config(ncp_host, ssh_port, ssh_user, gateway, local_ports):
    lines = [
        'Host map-ncp-admin', f'  HostName {ncp_host}', f'  Port {ssh_port}', f'  User {ssh_user}',
        f'  IdentityFile {INSTALL}/identity', f'  UserKnownHostsFile {INSTALL}/known_hosts',
        '  GlobalKnownHostsFile /dev/null', '  StrictHostKeyChecking yes', '  UpdateHostKeys no',
        '  IdentitiesOnly yes', '  IdentityAgent none', '  BatchMode yes',
        '  PasswordAuthentication no', '  KbdInteractiveAuthentication no',
        '  ForwardAgent no', '  RequestTTY no', '  ExitOnForwardFailure yes',
        '  ConnectTimeout 10', '  ServerAliveInterval 30', '  ServerAliveCountMax 3',
    ]
    lines += [f'  LocalForward {gateway}:{local_ports[name]} 127.0.0.1:{port}' for name, port in REMOTE_PORTS.items()]
    return '\n'.join(lines) + '\n'


def prepare(args):
    addressing(args.ncp_host, args.gcp_source_ip, args.bridge_cidr, args.bridge_gateway,
               args.local_ports, args.ncp_ssh_user, args.ncp_ssh_port)
    if args.output_dir.exists() or args.output_dir.is_symlink():
        raise ValueError('output directory must be new; existing credentials are never overwritten')
    tokens = read_tokens(private_read(args.production_env_file))
    targets = merge_targets(json.loads(private_read(args.current_admin_targets_file)), tokens,
                            args.bridge_gateway, args.local_ports)
    identity = private_read(args.identity_file)
    # Public key extraction and known-host matching are local and reveal no private values.
    pubkey = subprocess.check_output(['ssh-keygen', '-y', '-P', '', '-f', str(args.identity_file)],
                                     text=True, stderr=subprocess.DEVNULL).strip().split()
    if len(pubkey) < 2 or pubkey[0] not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256'):
        raise ValueError('a supported dedicated SSH identity is required')
    host_lookup = args.ncp_host if args.ncp_ssh_port == 22 else f'[{args.ncp_host}]:{args.ncp_ssh_port}'
    if not args.known_hosts_file.is_file() or args.known_hosts_file.is_symlink():
        raise ValueError('independently verified NCP known_hosts file required')
    known = subprocess.check_output(['ssh-keygen', '-F', host_lookup, '-f', str(args.known_hosts_file)],
                                    text=True, stderr=subprocess.DEVNULL)
    known = '\n'.join(line for line in known.splitlines() if line and not line.startswith('#')) + '\n'
    if not known.strip():
        raise ValueError('verified host key is missing for the selected NCP endpoint')
    options = ['restrict', 'port-forwarding', f'from="{args.gcp_source_ip}/32"']
    options += [f'permitopen="127.0.0.1:{port}"' for port in REMOTE_PORTS.values()]
    authorized = ','.join(options) + ' ' + ' '.join(pubkey[:2]) + ' map-gcp-admin-api-only\n'
    sshd = (f'Match User {args.ncp_ssh_user}\n'
            '    AuthenticationMethods publickey\n    PasswordAuthentication no\n'
            '    KbdInteractiveAuthentication no\n    AllowTcpForwarding local\n'
            '    PermitListen none\n    PermitTTY no\n    X11Forwarding no\n'
            '    AllowAgentForwarding no\n    ForceCommand /usr/sbin/nologin\n'
            '    PermitOpen ' + ' '.join(f'127.0.0.1:{port}' for port in REMOTE_PORTS.values()) + '\nMatch all\n')
    plan = {
        'status': 'PREPARED_NOT_DEPLOYED', 'network': {'name': NETWORK, 'internal': True,
        'subnet': args.bridge_cidr, 'gateway': args.bridge_gateway},
        'local_ports': args.local_ports, 'remote_ports': REMOTE_PORTS,
        'target_environment': 'prod', 'database_access': False, 'redis_access': False,
        'checks_not_run': ['gcp_subnet_and_port_availability', 'docker_bridge_membership',
                           'ssh_host_identity_external_verification', 'ssh_forwarding',
                           'admin_target_authorization', 'tunnel_disconnect_isolation'],
    }
    outputs = {'identity': identity, 'known_hosts': known,
               'compose.yml': (ROOT / 'docker-compose.admin.ncp.yml').read_text().replace(
                   '${NCP_ADMIN_TARGETS_ENV_FILE:-/etc/map-admin-ncp/admin-targets.env}',
                   '/etc/map-admin-ncp/admin-targets.env'),
               'ssh_config': ssh_config(args.ncp_host, args.ncp_ssh_port, args.ncp_ssh_user,
                                        args.bridge_gateway, args.local_ports),
               'admin-targets.env': 'ADMIN_TARGETS=' + json.dumps(targets, separators=(',', ':')) + '\n',
               'ncp-authorized_keys': authorized, 'ncp-sshd.conf': sshd,
               'plan.json': json.dumps(plan, indent=2) + '\n'}
    args.output_dir.mkdir(parents=True, mode=0o700)
    args.output_dir.chmod(0o700)
    for name, value in outputs.items():
        fd = os.open(args.output_dir / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(value)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('ncp-host', 'gcp-source-ip', 'bridge-cidr', 'bridge-gateway', 'ncp-ssh-user'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--ncp-ssh-port', type=int, default=22)
    for name in REMOTE_PORTS:
        parser.add_argument('--local-' + name + '-port', type=int, required=True)
    for name in ('production-env-file', 'current-admin-targets-file', 'identity-file', 'known-hosts-file', 'output-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    args.local_ports = {name: getattr(args, 'local_' + name + '_port') for name in REMOTE_PORTS}
    try:
        result = prepare(args)
        print(json.dumps({'status': result['status'], 'target': 'prod', 'forward_count': 3,
                          'database_access': False, 'redis_access': False}))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        # Never print parsed values, command output, JSON decode context or token-bearing lines.
        print('Admin tunnel preparation rejected: ' + (str(error) if type(error) is ValueError
                                                      else type(error).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
