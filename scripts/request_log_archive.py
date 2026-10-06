#!/usr/bin/env python3
"""Ship masked, encrypted container request logs to the archive's log-6m/ prefix.

Runs on the production host as root from a systemd timer. For each configured Compose service
it reads the window (cursor, now - settle] with `docker logs -t`, rejoins over-long messages,
masks coordinates (coordinate_redaction.scrub) and refuses the window when anything remains.
The lines become one gzip NDJSON document per service and window, encrypted with age to the
archive recipient in memory, and one new object is created with the Cloud Storage JSON API
(ifGenerationMatch=0, Custom-Time = upload time). The VM account may only create objects under
the prefix: no read, list, overwrite or delete, so gcloud storage cp (which reads first) is not
used. Plain text never touches disk; the state file holds cursors, counts and object names.

  request_log_archive.py arm --start <RFC3339 UTC>   first cursor; nothing before it is ever shipped
  request_log_archive.py run                         one window per service (the timer, or a flush
                                                     right before any container is recreated)
  request_log_archive.py status                      cursors and the last result, no log content

--config and --state-dir point a one-off acceptance run at its own Compose project, prefix and
state (for example log-6m/acceptance/) without touching the production cursors.

Exit status 0 when every service shipped (or had nothing new), 1 otherwise; output is codes,
counts and object names only, never log text.
"""
import argparse
import base64
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinate_redaction as redaction

CONFIG = Path('/etc/map-request-log/config.json')
STATE_DIR = Path('/var/lib/map-request-log')
DOCKER = '/usr/bin/docker'
AGE = '/usr/bin/age'
SYSTEM_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'DOCKER_HOST': 'unix:///var/run/docker.sock',
              'DOCKER_CONFIG': '/var/empty'}
METADATA = 'http://metadata.google.internal/computeMetadata/v1'
UPLOAD = 'https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?uploadType=multipart&ifGenerationMatch=0'
RECIPIENT = re.compile(r'age1[023456789acdefghjklmnpqrstuvwxyz]{58}')
SERVICE = re.compile(r'[a-z][a-z0-9-]{0,30}')
LINE = re.compile(rb'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z (.*)', re.S)


class ArchiveError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ArchiveError(code)


def load_config(path=CONFIG):
    """Write-once root file; every value is checked because a wrong prefix would put request
    records under another retention rule."""
    info = path.lstat()
    require(path.is_file() and not path.is_symlink() and info.st_uid == 0 and info.st_mode & 0o077 == 0,
            'config_not_root_private')
    config = json.loads(path.read_text())
    require(set(config) == {'gcp_project', 'instance', 'bucket', 'prefix', 'recipient', 'compose_project',
                            'services', 'settle_seconds', 'log_config'}, 'config_keys')
    require(config['prefix'].startswith('log-6m/') and config['prefix'].endswith('/'), 'config_prefix')
    require(RECIPIENT.fullmatch(config['recipient']), 'config_recipient')
    require(config['services'] and all(SERVICE.fullmatch(name) for name in config['services']), 'config_services')
    require(isinstance(config['settle_seconds'], int) and 5 <= config['settle_seconds'] <= 120, 'config_settle')
    return config


def run_command(argv, stdin=None):
    result = subprocess.run(argv, input=stdin, capture_output=True, env=SYSTEM_ENV, timeout=300)
    return result.returncode, result.stdout, result.stderr


def ns_to_docker(ns):
    return f'{ns // 1_000_000_000}.{ns % 1_000_000_000:09d}'


def ns_to_rfc3339(ns):
    stamp = dt.datetime.fromtimestamp(ns // 1_000_000_000, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    return f'{stamp}.{ns % 1_000_000_000:09d}Z'


def rfc3339_to_ns(text):
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z', text)
    require(match, 'bad_timestamp')
    seconds = int(dt.datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=dt.timezone.utc).timestamp())
    return seconds * 1_000_000_000 + int((match[2] or '0').ljust(9, '0'))


class Host:
    """Everything that touches Docker, the metadata server, age and Cloud Storage."""

    def __init__(self, config, command=run_command, opener=None):
        self.config, self.command = config, command
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._token = None

    def metadata(self, path):
        request = urllib.request.Request(METADATA + path, headers={'Metadata-Flavor': 'Google'})
        with self.opener.open(request, timeout=5) as response:
            return response.read().decode()

    def check_host(self):
        require(self.metadata('/project/project-id') == self.config['gcp_project'] and
                self.metadata('/instance/name') == self.config['instance'], 'wrong_host')

    def container(self, service):
        code, out, _ = self.command([DOCKER, 'ps', '-q', '--no-trunc', '--filter',
                                     f'label=com.docker.compose.project={self.config["compose_project"]}',
                                     '--filter', f'label=com.docker.compose.service={service}'])
        ids = out.decode().split() if code == 0 else []
        require(len(ids) == 1, 'container_not_single')
        code, out, _ = self.command([DOCKER, 'inspect', '--format', '{{json .HostConfig.LogConfig}}', ids[0]])
        require(code == 0, 'inspect_failed')
        log = json.loads(out)
        expected = self.config['log_config']
        require(log.get('Type') == expected['type'] and
                {key: log.get('Config', {}).get(key) for key in ('max-size', 'max-file')} ==
                {'max-size': expected['max-size'], 'max-file': expected['max-file']}, 'log_config_changed')
        return ids[0]

    def logs(self, container, since_ns, until_ns):
        """Messages in [since, until] as (timestamp ns, stream, text), stdout and stderr merged."""
        code, out, err = self.command([DOCKER, 'logs', '-t', '--since', ns_to_docker(since_ns), '--until',
                                       ns_to_docker(until_ns), container])
        require(code == 0, 'docker_logs_failed')
        lines = []
        for stream, data in (('stdout', out), ('stderr', err)):
            for record in data.split(b'\n'):
                if not record:
                    continue
                match = LINE.fullmatch(redaction.join_fragments(record))
                require(match, 'unparsed_log_record')
                seconds = rfc3339_to_ns(match[1].decode() + 'Z')
                stamp = seconds + int((match[2] or b'0').decode().ljust(9, '0'))
                lines.append((stamp, stream, match[3].decode('utf-8', 'replace')))
        lines.sort(key=lambda line: line[0])
        return lines

    def encrypt(self, data):
        code, out, _ = self.command([AGE, '--encrypt', '--recipient', self.config['recipient']], stdin=data)
        require(code == 0 and out.startswith(b'age-encryption.org/v1'), 'age_failed')
        return out

    def token(self):
        if self._token is None:
            self._token = json.loads(self.metadata('/instance/service-accounts/default/token'))['access_token']
        return self._token

    def upload(self, name, data, custom_time):
        """Create one object; 200 created, 412 an object of that name exists already."""
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        meta = json.dumps({'name': name, 'customTime': custom_time, 'contentType': 'application/octet-stream',
                           'md5Hash': md5}).encode()
        boundary = 'map-request-log-' + hashlib.sha256(data).hexdigest()[:24]
        body = b''.join([b'--', boundary.encode(), b'\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n', meta,
                         b'\r\n--', boundary.encode(), b'\r\nContent-Type: application/octet-stream\r\n\r\n', data,
                         b'\r\n--', boundary.encode(), b'--\r\n'])
        request = urllib.request.Request(UPLOAD.format(bucket=urllib.parse.quote(self.config['bucket'], safe='')),
                                         data=body, method='POST', headers={
                                             'Authorization': 'Bearer ' + self.token(),
                                             'Content-Type': 'multipart/related; boundary=' + boundary})
        try:
            with self.opener.open(request, timeout=60) as response:
                answer = json.loads(response.read())
                require(answer.get('md5Hash') == md5 and answer.get('name') == name, 'upload_mismatch')
                return 200
        except urllib.error.HTTPError as error:
            return error.code


class Archive:
    def __init__(self, config, host, state_dir=STATE_DIR, now_ns=time.time_ns):
        self.config, self.host, self.state_dir, self.now_ns = config, host, state_dir, now_ns
        self.state_path = state_dir / 'state.json'

    def read_state(self):
        require(self.state_path.is_file(), 'not_armed')
        return json.loads(self.state_path.read_text())

    def write_state(self, state):
        temporary = self.state_path.with_suffix('.tmp')
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
            json.dump(state, stream, indent=1, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_path)

    def arm(self, start):
        require(not self.state_path.exists(), 'already_armed')
        cursor = rfc3339_to_ns(start)
        require(cursor <= self.now_ns(), 'start_in_future')
        self.write_state({'schema': 1, 'services': {name: {'cursor': cursor} for name in self.config['services']}})
        return cursor

    def object_name(self, service, until_ns):
        moment = dt.datetime.fromtimestamp(until_ns // 1_000_000_000, dt.timezone.utc)
        return (f'{self.config["prefix"]}{moment:%Y/%m/%d}/{service}/{moment:%H%M%S}.'
                f'{until_ns % 1_000_000_000:09d}Z.ndjson.gz.age')

    def ship(self, service, entry):
        """One window for one service. Updates entry in place; returns (code, details)."""
        container = self.host.container(service)
        details = {}
        if entry.get('container') and entry['container'] != container:
            # The previous container is gone together with lines nobody collected after the cursor.
            details['replaced'] = True
        last = entry.get('last_line')
        if last and entry.get('container') == container and not self.host.logs(container, last, last):
            details['rotation_gap'] = True
        if 'pending' not in entry:
            entry['pending'] = {'since': entry['cursor'] + 1,
                                'until': self.now_ns() - self.config['settle_seconds'] * 1_000_000_000}
        window = entry['pending']
        lines = self.host.logs(container, window['since'], window['until']) if window['until'] >= window['since'] else []
        documents, masked = [], 0
        for stamp, stream, text in lines:
            text, count, left = redaction.scrub(text)
            require(not left, 'residue_after_wide_mask')
            masked += count
            documents.append(json.dumps({'ts': ns_to_rfc3339(stamp), 'service': service, 'stream': stream,
                                         'line': text}, ensure_ascii=False))
        details.update(lines=len(lines), masked=masked)
        if documents:
            payload = self.host.encrypt(gzip.compress(('\n'.join(documents) + '\n').encode(), mtime=0))
            name = self.object_name(service, window['until'])
            status = self.host.upload(name, payload, ns_to_rfc3339(self.now_ns()))
            require(status in (200, 412), f'upload_{status}')
            details.update(object=name, status=status)
            entry['last_line'] = lines[-1][0]
        entry.update(cursor=window['until'], container=container)
        del entry['pending']
        return ('gap' if details.get('rotation_gap') or details.get('replaced') else 'ok'), details

    def run(self):
        with open(os.open(self.state_dir / 'lock', os.O_WRONLY | os.O_CREAT, 0o600), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = self.read_state()
            self.host.check_host()
            results = {}
            for service in self.config['services']:
                entry = state['services'].setdefault(service, {'cursor': self.now_ns()})
                try:
                    results[service] = self.ship(service, entry)
                except ArchiveError as error:
                    results[service] = (str(error), {})
                # The cursor (or the pending window) is saved after every service so a crash
                # repeats at most one window, under the same object name.
                self.write_state(state)
            state['last_run'] = {'at': ns_to_rfc3339(self.now_ns()),
                                 'codes': {service: code for service, (code, _) in results.items()}}
            self.write_state(state)
            return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--state-dir', type=Path, default=STATE_DIR)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('arm').add_argument('--start', required=True)
    commands.add_parser('run')
    commands.add_parser('status')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        require(args.state_dir.is_dir() and not args.state_dir.is_symlink() and
                args.state_dir.stat().st_mode & 0o077 == 0, 'state_dir_not_private')
        archive = Archive(config, Host(config), args.state_dir)
        if args.command == 'arm':
            print('MAP_REQLOG_ARMED cursor=' + ns_to_rfc3339(archive.arm(args.start)))
            return 0
        if args.command == 'status':
            state = archive.read_state()
            for name, entry in sorted(state['services'].items()):
                print(f'MAP_REQLOG_STATUS service={name} cursor={ns_to_rfc3339(entry["cursor"])}'
                      f' pending={"pending" in entry}')
            print('MAP_REQLOG_LAST ' + json.dumps(state.get('last_run'), sort_keys=True))
            return 0
        results = archive.run()
    except (ArchiveError, OSError, ValueError, KeyError) as error:
        print(f'MAP_REQLOG_RESULT=FAILED code={type(error).__name__}:{error}')
        return 1
    for service, (code, details) in results.items():
        print(f'MAP_REQLOG service={service} code={code} ' + ' '.join(f'{key}={value}' for key, value in sorted(details.items())))
    failed = sorted(service for service, (code, _) in results.items() if code != 'ok')
    print('MAP_REQLOG_RESULT=' + ('OK' if not failed else 'FAILED services=' + ','.join(failed)))
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
