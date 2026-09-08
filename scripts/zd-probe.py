#!/usr/bin/env python3
"""Keep asking the public entry points what they answer while a deployment runs.

One request per second per check, from before the deployment starts until after
it finishes. A deployment is only called seamless when this reports zero
failures: an unexpected status, a refused connection and a timeout all count.

Nothing here writes to the service. The authenticated check deliberately sends
no credential and expects the rejection, so a run leaves no account, schedule or
message behind.
"""
from __future__ import annotations
import argparse
import json
import os
import socket
import ssl
import statistics
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TIMEOUT = 5


def http_once(url, expected):
    start = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'map-deploy-probe'})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            status, body = response.status, response.read(4096)
    except urllib.error.HTTPError as error:
        status, body = error.code, b''
    except Exception as error:
        return {'ok': False, 'status': 0, 'seconds': time.monotonic() - start,
                'kind': type(error).__name__}
    ok = status == expected
    return {'ok': ok, 'status': status, 'seconds': time.monotonic() - start,
            'kind': 'status' if not ok else 'none', 'bytes': len(body)}


def websocket_once(url):
    """Open the chat socket far enough to prove the entry point is answering."""
    start = time.monotonic()
    secure = url.startswith('wss://')
    rest = url.split('://', 1)[1]
    host, _, path = rest.partition('/')
    hostname, _, port = host.partition(':')
    port = int(port) if port else (443 if secure else 80)
    key = 'dGhlIHNhbXBsZSBub25jZQ=='
    try:
        raw = socket.create_connection((hostname, port), timeout=TIMEOUT)
        if secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=hostname)
        with raw:
            raw.settimeout(TIMEOUT)
            raw.sendall(('GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n'
                         'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
                         'Sec-WebSocket-Version: 13\r\n\r\n' % (path, host, key)).encode())
            head = raw.recv(256).decode('latin-1', 'replace')
        status = int(head.split(' ')[1]) if head.startswith('HTTP/1.1') else 0
    except Exception as error:
        return {'ok': False, 'status': 0, 'seconds': time.monotonic() - start,
                'kind': type(error).__name__}
    return {'ok': status == 101, 'status': status, 'seconds': time.monotonic() - start,
            'kind': 'status' if status != 101 else 'none'}


class Probe:
    def __init__(self, checks, sockets):
        self.checks = checks
        self.sockets = sockets
        self.samples = {name: [] for name, _ in checks}
        self.samples.update({name: [] for name, _ in sockets})
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def once(self):
        for name, (url, expected) in self.checks:
            result = http_once(url, expected)
            with self.lock:
                self.samples[name].append(result)
        for name, url in self.sockets:
            result = websocket_once(url)
            with self.lock:
                self.samples[name].append(result)

    def loop(self, seconds, interval=1.0):
        deadline = time.monotonic() + seconds
        while not self.stop.is_set() and time.monotonic() < deadline:
            started = time.monotonic()
            self.once()
            time.sleep(max(0.0, interval - (time.monotonic() - started)))

    def summary(self):
        with self.lock:
            report = {}
            failures = 0
            for name, results in self.samples.items():
                bad = [r for r in results if not r['ok']]
                failures += len(bad)
                times = sorted(r['seconds'] for r in results) or [0.0]
                report[name] = {
                    'requests': len(results), 'failures': len(bad),
                    'p50_seconds': round(statistics.median(times), 3),
                    'p95_seconds': round(times[min(len(times) - 1, int(len(times) * 0.95))], 3),
                    'max_seconds': round(max(times), 3),
                    'failure_kinds': sorted({r['kind'] for r in bad}),
                    'unexpected_statuses': sorted({r['status'] for r in bad}),
                }
            return report, failures


def default_checks(origin, invite_token):
    return [
        ('edge_healthz', (origin + '/healthz', 200)),
        ('application_health', (origin + '/healthz/app', 200)),
        ('authenticated_route_rejects_anonymous', (origin + '/api/v1/users/me', 401)),
        ('unknown_invite_is_not_found', (origin + '/api/v1/chat/invites/' + invite_token, 404)),
        ('actuator_stays_closed', (origin + '/actuator', 404)),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--interval', type=float, default=1.0)
    parser.add_argument('--invite-token', default='0' * 32,
                        help='a token that must not exist, so the answer is a clean not-found')
    parser.add_argument('--websocket', action='append', default=[])
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()

    origin = args.origin.rstrip('/')
    sockets = [('chat_socket_%d' % index, url) for index, url in enumerate(args.websocket)]
    probe = Probe(default_checks(origin, args.invite_token), sockets)
    started = datetime.now(timezone.utc).isoformat()
    probe.loop(args.seconds, args.interval)
    report, failures = probe.summary()
    out = {'status': 'PASS' if failures == 0 else 'FAIL', 'origin': origin,
           'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(),
           'seconds': args.seconds, 'total_failures': failures, 'checks': report,
           'writes_performed': 0}
    if args.receipt and not args.receipt.exists() and not args.receipt.is_symlink():
        with args.receipt.open('x') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(out, indent=2) + '\n')
    print(json.dumps(out, indent=1))
    return 0 if failures == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
