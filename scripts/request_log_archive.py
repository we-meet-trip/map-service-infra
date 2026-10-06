#!/usr/bin/env python3
"""Ship masked, encrypted container stdout/stderr to the archive's log-6m/ prefix.

Runs on the production host as root from a systemd timer. For each configured Compose service
it reads the window (cursor, now - settle] with `docker logs -t` (a stopped container is read to
its end), rejoins over-long messages, masks coordinates with coordinate_redaction.scrub and
replaces any line that still looks like a coordinate with a fixed placeholder. The lines become
one gzip NDJSON document per service and window, encrypted with age to the archive recipient in
memory, and one new object is created with the Cloud Storage JSON API (ifGenerationMatch=0,
Custom-Time = upload time). The VM account may only create objects under the prefix: no read,
list, overwrite or delete, so gcloud storage cp (which reads first) is not used. Plain text never
touches disk; the state file holds cursors, container IDs, the pending window and result codes.

  request_log_archive.py arm --start <RFC3339 UTC>   first cursor (at most 6 hours back);
                                                     nothing before it is ever shipped
  request_log_archive.py run                         one window per service (the timer, or a
                                                     manual run after stopping a container)
  request_log_archive.py status                      cursors and the last result, no log text

--config and --state-dir point a one-off acceptance run at its own Compose project, prefix and
state (for example log-6m/acceptance/) without touching the production cursors.

Exit status 0 when every service shipped (or had nothing new), 1 otherwise. Output is codes,
counts and object names only, never log text or exception messages. A fully successful run
also prints MAP_BACKUP_RESULT=COMPLETE kind=reqlog, so the per-kind backup absence alert notices
a collector that stopped running.
"""
import argparse
import base64
import datetime as dt
import fcntl
import gzip
import hashlib
import http.client
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
NS = 1_000_000_000
ARM_MAX_AGE_NS = 6 * 3600 * NS
# Everything a single service can raise; it becomes that service's code and the run goes on.
SERVICE_ERRORS = (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, http.client.HTTPException)


class ArchiveError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ArchiveError(code)


def load_config(path=CONFIG):
    """Write-once root file; every value is checked because a wrong prefix would put request
    records under another retention rule."""
    info = path.lstat()
    require(path.is_file() and not path.is_symlink() and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
            'config_not_private')
    config = json.loads(path.read_text())
    require(isinstance(config, dict) and set(config) == {'gcp_project', 'instance', 'bucket', 'prefix', 'recipient',
                                                         'compose_project', 'services', 'settle_seconds',
                                                         'log_config'}, 'config_keys')
    require(isinstance(config['prefix'], str) and config['prefix'].startswith('log-6m/') and
            config['prefix'].endswith('/'), 'config_prefix')
    require(isinstance(config['recipient'], str) and RECIPIENT.fullmatch(config['recipient']), 'config_recipient')
    services = config['services']
    require(isinstance(services, list) and services and len(set(services)) == len(services) and
            all(isinstance(name, str) and SERVICE.fullmatch(name) for name in services), 'config_services')
    require(isinstance(config['settle_seconds'], int) and 5 <= config['settle_seconds'] <= 120, 'config_settle')
    log = config['log_config']
    require(isinstance(log, dict) and set(log) == {'type', 'max-size', 'max-file'} and
            all(isinstance(value, str) for value in log.values()), 'config_log')
    return config


def run_command(argv, stdin=None):
    result = subprocess.run(argv, input=stdin, capture_output=True, env=SYSTEM_ENV, timeout=240)
    return result.returncode, result.stdout, result.stderr


def ns_to_docker(ns):
    return f'{ns // NS}.{ns % NS:09d}'


def ns_to_rfc3339(ns):
    stamp = dt.datetime.fromtimestamp(ns // NS, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    return f'{stamp}.{ns % NS:09d}Z'


def rfc3339_to_ns(text):
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z', text)
    require(match, 'bad_timestamp')
    seconds = int(dt.datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=dt.timezone.utc).timestamp())
    return seconds * NS + int((match[2] or '0').ljust(9, '0'))


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
        """(container ID, running) of the one service container; stopped ones count too so their
        last lines can still be read. One-off `compose run` containers are left out."""
        code, out, _ = self.command([DOCKER, 'ps', '-a', '-q', '--no-trunc',
                                     '--filter', 'label=com.docker.compose.oneoff=False',
                                     '--filter', f'label=com.docker.compose.project={self.config["compose_project"]}',
                                     '--filter', f'label=com.docker.compose.service={service}'])
        ids = out.decode().split() if code == 0 else []
        require(len(ids) == 1, 'container_not_single')
        code, out, _ = self.command([DOCKER, 'inspect', '--format',
                                     '{{json .HostConfig.LogConfig}} {{.State.Running}}', ids[0]])
        require(code == 0, 'inspect_failed')
        log_json, running = out.decode().rsplit(' ', 1)
        log, expected = json.loads(log_json), self.config['log_config']
        # Any other option (mode=non-blocking drops lines, compress changes the files) fails closed.
        require(log.get('Type') == expected['type'] and
                log.get('Config') == {'max-size': expected['max-size'], 'max-file': expected['max-file']},
                'log_config_changed')
        return ids[0], running.strip() == 'true'

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
                stamp = rfc3339_to_ns(match[1].decode() + 'Z') + int((match[2] or b'0').decode().ljust(9, '0'))
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
        require(self.state_path.is_file() and not self.state_path.is_symlink(), 'not_armed')
        return json.loads(self.state_path.read_text())

    def write_state(self, state):
        temporary = self.state_path.with_suffix('.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with open(descriptor, 'w') as stream:
            json.dump(state, stream, indent=1, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_path)
        directory = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def arm(self, start):
        require(not self.state_path.exists(), 'already_armed')
        cursor, now = rfc3339_to_ns(start), self.now_ns()
        require(cursor <= now, 'start_in_future')
        # A mistyped start would ship rehearsal-period lines the VM can never delete.
        require(now - cursor <= ARM_MAX_AGE_NS, 'start_too_old')
        services = {}
        for name in self.config['services']:
            services[name] = {'cursor': cursor}
            try:
                # Recorded so a container recreated before the first run still shows up as replaced.
                services[name]['container'] = self.host.container(name)[0]
            except (ArchiveError,) + SERVICE_ERRORS:
                pass
        self.write_state({'schema': 1, 'services': services})
        return cursor

    def object_name(self, service, until_ns):
        moment = dt.datetime.fromtimestamp(until_ns // NS, dt.timezone.utc)
        return f'{self.config["prefix"]}{moment:%Y/%m/%d}/{service}/{moment:%H%M%S}.{until_ns % NS:09d}Z.ndjson.gz.age'

    def ship(self, service, entry, save):
        """One window for one service. Updates entry in place; returns (code, details)."""
        container, running = self.host.container(service)
        details = {}
        replaced = entry.get('container') not in (None, container)
        if replaced:
            # Lines of a container removed before its last window was read are lost, unless that
            # container was read to its end after it stopped (drained).
            # ponytail: a drained container that is started again and replaced before the next
            # run loses what it wrote meanwhile without a gap; upgrade path: compare docker
            # events (start, destroy) with the run times.
            details['replaced'] = True
            if entry.get('drained') != entry.get('container'):
                details['lost_on_replace'] = True
            entry.pop('last_line', None)
        # The cursor never passes the clock reading it came from, so a clock behind it has
        # stepped back. Lines stamped in the stepped-back time sort before the cursor in the
        # json-file and the next window's since skips them.
        if self.now_ns() < entry['cursor']:
            details['clock_behind'] = True
        fresh = 'pending' not in entry
        if fresh:
            # ponytail: a line that reaches the json-file more than settle_seconds after its
            # timestamp is skipped by the next window's since; upgrade path: overlap windows by
            # settle and drop lines already shipped by (ts, stream, sha256(line)).
            until = self.now_ns() - (self.config['settle_seconds'] * NS if running else 0)
            entry['pending'] = {'since': entry['cursor'] + 1, 'until': until}
            # The window and its object name are fixed on disk before anything reaches the bucket,
            # so a retry after a lost response repeats the same name and gets 412.
            save()
        window = entry['pending']
        lines = self.host.logs(container, window['since'], window['until']) if window['until'] >= window['since'] else []
        last = entry.get('last_line')
        # Docker opens all rotated files for one read and rotation only deletes, so a last line
        # that is still there now was there for the whole read above.
        # ponytail: the first window after arm or a replacement has no earlier line to check, so
        # rotation before it ships (a long outage that fails every run meanwhile) is not
        # reported; upgrade path: compare the container's oldest kept line with its start time.
        if last and not replaced and not self.host.logs(container, last, last):
            details['rotation_gap'] = True
        documents, masked, withheld = [], 0, 0
        for stamp, stream, text in lines:
            text, count, held = redaction.scrub(text)
            masked, withheld = masked + count, withheld + held
            documents.append(json.dumps({'ts': ns_to_rfc3339(stamp), 'service': service, 'stream': stream,
                                         'line': text}, ensure_ascii=False))
        details.update(lines=len(lines), masked=masked, withheld=withheld)
        if documents:
            payload = self.host.encrypt(gzip.compress(('\n'.join(documents) + '\n').encode(), mtime=0))
            name = self.object_name(service, window['until'])
            status = self.host.upload(name, payload, ns_to_rfc3339(self.now_ns()))
            require(status in (200, 412), f'upload_{status}')
            details.update(object=name, status=status)
            entry['last_line'] = lines[-1][0]
        entry.update(cursor=max(entry['cursor'], window['until']), container=container)
        # Read to its end only when this window was laid out after the container had stopped;
        # a window left over from an earlier attempt may end before its last lines.
        if fresh and not running:
            entry['drained'] = container
        else:
            entry.pop('drained', None)
        del entry['pending']
        if details.get('rotation_gap') or details.get('lost_on_replace') or details.get('clock_behind'):
            return 'gap', details
        return ('withheld' if withheld else 'ok'), details

    def run(self):
        with open(os.open(self.state_dir / 'lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'w') as lock:
            # A manual run waits for a timer run in progress and then reads its own window.
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.read_state()
            self.host.check_host()
            results = {}
            for service in self.config['services']:
                entry = state['services'].setdefault(service, {'cursor': self.now_ns()})
                try:
                    results[service] = self.ship(service, entry, lambda: self.write_state(state))
                except ArchiveError as error:
                    results[service] = (str(error), {})
                except SERVICE_ERRORS as error:
                    results[service] = (type(error).__name__, {})
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
        info = args.state_dir.lstat()
        require(args.state_dir.is_dir() and not args.state_dir.is_symlink() and info.st_uid == os.geteuid() and
                info.st_mode & 0o077 == 0, 'state_dir_not_private')
        archive = Archive(config, Host(config), args.state_dir)
        if args.command == 'arm':
            print('MAP_REQLOG_ARMED cursor=' + ns_to_rfc3339(archive.arm(args.start)))
            return 0
        if args.command == 'status':
            state = archive.read_state()
            for name, entry in sorted(state['services'].items()):
                print(f'MAP_REQLOG_STATUS service={name} cursor={ns_to_rfc3339(entry["cursor"])}'
                      f' pending={"pending" in entry} drained={"drained" in entry}')
            print('MAP_REQLOG_LAST ' + json.dumps(state.get('last_run'), sort_keys=True))
            return 0
        results = archive.run()
    except ArchiveError as error:
        print(f'MAP_REQLOG_RESULT=FAILED code={error}')
        return 1
    except Exception as error:  # noqa: BLE001 — only the type name may reach the journal
        print(f'MAP_REQLOG_RESULT=FAILED code={type(error).__name__}')
        return 1
    for service, (code, details) in results.items():
        print(f'MAP_REQLOG service={service} code={code} ' + ' '.join(f'{key}={value}' for key, value in sorted(details.items())))
    failed = sorted(service for service, (code, _) in results.items() if code != 'ok')
    if failed:
        print('MAP_REQLOG_RESULT=FAILED services=' + ','.join(failed))
        return 1
    print('MAP_REQLOG_RESULT=OK')
    print('MAP_BACKUP_RESULT=COMPLETE kind=reqlog')
    return 0


if __name__ == '__main__':
    sys.exit(main())
