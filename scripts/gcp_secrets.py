#!/usr/bin/env python3
"""Secret Manager containers and versions through gcloud, with values kept off argv.

  ensure --project P --location L [--check-only] NAME...
      Create each missing secret with user-managed replication in exactly L. An
      existing secret is only checked: its replicas must be exactly {L}.
      --check-only never creates anything.
  generate --project P NAME [--bytes 32] [--rotate]
      Add secrets.token_urlsafe(N) as a new version. Refused while the secret
      already has a version, unless --rotate.
  put --project P NAME
      Add the one value read from stdin (a single trailing newline dropped) as a
      new version. Empty values and values with a line break are refused.
  materialize --project P --output FILE KEY=SECRET_NAME...
      Read the latest version of each secret and write KEY=value lines to FILE
      atomically with mode 0600. Every value must be safe in an env file.

A value only moves through a pipe to or from gcloud and through this process's
memory: it is never an argument, printed, or logged, and gcloud's own messages
are relayed only for commands that carry no value. gcloud copies everything it
prints into its own log files, so every call runs with file logging disabled.
gcloud prints a payload as url-safe base64 (its plain text output may mangle
bytes), so reads decode here. generate and put print only the new version number.
"""
import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

GCLOUD = '/usr/bin/gcloud'
NAME = re.compile(r'[A-Za-z0-9_-]{1,255}')
PROJECT = re.compile(r'[a-z][a-z0-9-]{4,28}[a-z0-9]')
LOCATION = re.compile(r'[a-z]+-[a-z]+[0-9]+')
KEY = re.compile(r'[A-Z][A-Z0-9_]*')
# Characters docker compose reads literally from an env file. '$' (interpolation),
# '#' (comment), quotes and whitespace are left out.
ENV_SAFE = re.compile(rb'[A-Za-z0-9._~+/=:@?&%,-]+')
# Secret Manager refuses payloads above 64 KiB.
MAX_VALUE = 65536
# The only text taken from a failed command that carries a value.
STATUS = re.compile(rb'\b(INVALID_ARGUMENT|NOT_FOUND|ALREADY_EXISTS|PERMISSION_DENIED|RESOURCE_EXHAUSTED|'
                    rb'FAILED_PRECONDITION|UNAUTHENTICATED|UNAVAILABLE|DEADLINE_EXCEEDED|INTERNAL)\b')


class SecretError(Exception):
    pass


def gcloud(*args, value=None, sensitive=False):
    """Run gcloud and return stdout. A value goes in through stdin only."""
    binary = GCLOUD if os.access(GCLOUD, os.X_OK) else shutil.which('gcloud')
    if not binary:
        raise SecretError('gcloud not found')
    env = {**os.environ, 'CLOUDSDK_CORE_DISABLE_PROMPTS': '1', 'CLOUDSDK_CORE_LOG_HTTP': 'false',
           'CLOUDSDK_CORE_DISABLE_FILE_LOGGING': '1'}
    stdin = {'stdin': subprocess.DEVNULL} if value is None else {'input': value}
    command = 'gcloud ' + ' '.join(args[:3])
    try:
        result = subprocess.run([binary, *args], capture_output=True, env=env, timeout=120, **stdin)
    except subprocess.TimeoutExpired:
        raise SecretError(command + ' timed out') from None
    if result.returncode == 0:
        return result.stdout
    if sensitive:
        status = STATUS.search(result.stderr)
        raise SecretError(command + ' failed' + (': ' + status[1].decode() if status else ''))
    raise SecretError(result.stderr.decode(errors='replace').strip() or f'{command} exited {result.returncode}')


def parsed(output, what):
    try:
        return json.loads(output)
    except ValueError:
        raise SecretError(f'unexpected gcloud {what} output') from None


def describe(project, name):
    try:
        output = gcloud('secrets', 'describe', name, '--project', project, '--format=json')
    except SecretError as error:
        # ponytail: a missing secret is recognized by gcloud's NOT_FOUND status text;
        # any other failure stops before anything is created.
        if 'NOT_FOUND' in str(error):
            return None
        raise
    secret = parsed(output, 'describe')
    if not isinstance(secret, dict):
        raise SecretError(f'{name}: unexpected gcloud describe output')
    return secret


def replica_locations(secret):
    replicas = ((secret.get('replication') or {}).get('userManaged') or {}).get('replicas') or []
    return {replica.get('location') for replica in replicas}


def ensure(args):
    failed = False
    for name in args.names:
        secret = describe(args.project, name)
        if secret is None and not args.check_only:
            gcloud('secrets', 'create', name, '--project', args.project,
                   '--replication-policy=user-managed', f'--locations={args.location}')
            print(f'created {name}')
            continue
        if secret is None:
            print(f'{name}: missing', file=sys.stderr)
        elif replica_locations(secret) != {args.location}:
            print(f'{name}: replication is not user-managed in exactly {args.location}', file=sys.stderr)
        else:
            print(f'ok {name}')
            continue
        failed = True
    return 1 if failed else 0


def add_version(project, name, value):
    output = gcloud('secrets', 'versions', 'add', name, '--project', project, '--data-file=-',
                    '--format=value(name)', value=value, sensitive=True)
    version = output.decode(errors='replace').strip().rpartition('/')[2]
    if not version.isdigit():
        raise SecretError(f'{name}: unexpected gcloud versions add output')
    print(version)
    return 0


def generate(args):
    listed = gcloud('secrets', 'versions', 'list', args.name, '--project', args.project, '--limit=1', '--format=json')
    if parsed(listed or b'[]', 'versions list') and not args.rotate:
        raise SecretError(f'{args.name} already has a version; pass --rotate to add another')
    return add_version(args.project, args.name, secrets.token_urlsafe(args.bytes).encode())


def put(args):
    if sys.stdin.isatty():
        raise SecretError('put reads the value from a pipe, not a terminal')
    value = sys.stdin.buffer.read(MAX_VALUE + 2)
    if value.endswith(b'\n'):
        value = value[:-1]
    if not value:
        raise SecretError('empty value refused')
    if b'\n' in value or b'\r' in value:
        raise SecretError('value with a line break refused')
    if len(value) > MAX_VALUE:
        raise SecretError('value larger than 64 KiB refused')
    return add_version(args.project, args.name, value)


def decode_payload(output, key):
    text = output.strip()
    try:
        # gcloud may leave out the padding; both base64 alphabets are accepted.
        return base64.b64decode(text + b'=' * (-len(text) % 4), altchars=b'-_', validate=True)
    except binascii.Error:
        raise SecretError(f'{key}: gcloud payload is not base64') from None


def write_private(path, data):
    # A same-directory temporary file renamed over the target: readers see either
    # the old file or the complete new one, and never a wider mode.
    temporary = path.with_name(f'.{path.name}.{secrets.token_hex(8)}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def materialize(args):
    lines = []
    for key, name in args.pairs:
        try:
            output = gcloud('secrets', 'versions', 'access', 'latest', f'--secret={name}', '--project', args.project,
                            '--format=get(payload.data)', sensitive=True)
        except SecretError as error:
            raise SecretError(f'{key}: {name}: {error}') from None
        value = decode_payload(output, key)
        if not ENV_SAFE.fullmatch(value):
            raise SecretError(f'{key}: value is empty or has characters outside [A-Za-z0-9._~+/=:@?&%,-]')
        lines.append(key.encode() + b'=' + value + b'\n')
    write_private(Path(args.output), b''.join(lines))
    print(f'wrote {len(lines)} keys to {args.output}')
    return 0


def checked(regex, what):
    def check(text):
        if not regex.fullmatch(text):
            raise argparse.ArgumentTypeError(f'invalid {what}: {text!r}')
        return text
    return check


def pair(text):
    key, _, name = text.partition('=')
    if not (KEY.fullmatch(key) and NAME.fullmatch(name)):
        raise argparse.ArgumentTypeError(f'expected KEY=SECRET_NAME, got {text!r}')
    return key, name


def byte_count(text):
    count = int(text)
    if not 16 <= count <= 1024:
        raise argparse.ArgumentTypeError('--bytes must be between 16 and 1024')
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)
    name, project = checked(NAME, 'secret name'), checked(PROJECT, 'project ID')
    ensure_parser = commands.add_parser('ensure')
    ensure_parser.add_argument('--location', required=True, type=checked(LOCATION, 'location'))
    ensure_parser.add_argument('--check-only', action='store_true')
    ensure_parser.add_argument('names', nargs='+', type=name, metavar='NAME')
    generate_parser = commands.add_parser('generate')
    generate_parser.add_argument('name', type=name, metavar='NAME')
    generate_parser.add_argument('--bytes', type=byte_count, default=32)
    generate_parser.add_argument('--rotate', action='store_true')
    put_parser = commands.add_parser('put')
    put_parser.add_argument('name', type=name, metavar='NAME')
    materialize_parser = commands.add_parser('materialize')
    materialize_parser.add_argument('--output', required=True)
    materialize_parser.add_argument('pairs', nargs='+', type=pair, metavar='KEY=SECRET_NAME')
    for sub in (ensure_parser, generate_parser, put_parser, materialize_parser):
        sub.add_argument('--project', required=True, type=project)
    args = parser.parse_args(argv)
    if args.command == 'materialize' and len({key for key, _ in args.pairs}) != len(args.pairs):
        parser.error('each KEY may appear only once')
    try:
        return {'ensure': ensure, 'generate': generate, 'put': put, 'materialize': materialize}[args.command](args)
    except (SecretError, OSError) as error:
        print(f'gcp_secrets: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
