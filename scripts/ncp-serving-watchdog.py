#!/usr/bin/env python3
"""Return the public serving containers when the recorded receipt says they are open.

The controller writes a receipt naming the exact containers it published. This
reads that receipt and starts back only those ids, and only while the receipt
still claims public service. A container the operator deliberately stopped, one
that was replaced underneath, and a deployment already in flight each leave this
doing nothing, so it can never reopen a door somebody else closed.

The deployment lock is taken only once a start is actually due. An idle check
touches nothing, so repeating it often never competes with a deployment for the
lock the way a hold-first design would.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

CONTROLLER = Path('/opt/map-serving-controller/scripts/ncp-production-serving.py')


def load(path=CONTROLLER):
    spec = importlib.util.spec_from_file_location('ncp_serving_controller', path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def published(serving, path):
    """The recorded containers, but only from a receipt still claiming public service."""
    if not path.exists():
        return None
    value = serving.receiver.private.read_json(path, private=True)
    if not (value.get('status') == 'PUBLIC_READY' and value.get('public_serving') == 'OPEN'
            and value.get('resume_public') is True):
        return None
    containers = value.get('containers')
    if not isinstance(containers, dict) or not set(serving.PUBLIC_SERVICES) <= set(containers):
        return None
    result = {}
    for name in serving.PUBLIC_SERVICES:
        entry = containers[name]
        if not (isinstance(entry, dict) and serving.receiver.HEX.fullmatch(str(entry.get('container_id')))
                and serving.receiver.IMAGE.fullmatch(str(entry.get('image_id')))):
            return None
        result[name] = entry
    return result


def state(serving, backend, entry):
    """Running state of the one recorded container, or None if it is no longer that container."""
    try:
        raw = backend.docker(['inspect', '--format',
                              '{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}}}',
                              entry['container_id']])
        item = json.loads(raw)
    except Exception:
        return None
    if not (item.get('id') == entry['container_id'] and item.get('image') == entry['image_id']
            and isinstance(item.get('running'), bool)):
        return None
    return item['running']


def due(serving, backend, containers):
    """Names whose recorded container is still itself but is not running."""
    return [name for name in serving.PUBLIC_SERVICES if state(serving, backend, containers[name]) is False]


def run(serving, backend=None):
    backend = backend or serving.receiver.Backend()
    path = serving.STATE / serving.STATE_NAME
    containers = published(serving, path)
    if containers is None:
        return {'action': 'none', 'reason': 'not_publicly_serving'}
    if not due(serving, backend, containers):
        return {'action': 'none', 'reason': 'no_stopped_container'}
    # ponytail: no flap damping. A container that keeps dying is started again every
    # tick; serving between the restarts beats staying closed. Add a backoff here if
    # a repeating cause ever has to be ridden out rather than fixed.
    try:
        with serving.receiver.writer(serving.STATE):
            # Re-read under the lock: the receipt may have changed while it was held.
            containers = published(serving, path)
            if containers is None:
                return {'action': 'none', 'reason': 'not_publicly_serving'}
            names = due(serving, backend, containers)
            if not names:
                return {'action': 'none', 'reason': 'no_stopped_container'}
            for name in names:
                backend.docker(['start', containers[name]['container_id']], timeout=120)
            started = [name for name in names if state(serving, backend, containers[name]) is True]
            return {'action': 'started', 'started': started,
                    'failed': [name for name in names if name not in started]}
    except serving.receiver.migration.JobError:
        return {'action': 'none', 'reason': 'deployment_lock_held'}
    except Exception:
        return {'action': 'failed', 'reason': 'container_start_failed'}


def main(argv=None):
    os.umask(0o077)
    serving = None
    try:
        serving = load()
        serving.require(os.geteuid() == 0, 'root_watchdog_runner_required')
        serving.receiver.source_ownership(serving.ROOT)
        print(json.dumps(run(serving), sort_keys=True))
        return 0
    except Exception as error:
        known = serving is not None and isinstance(error, serving.receiver.ReceiverError)
        print(json.dumps({'action': 'error',
                          'error_code': str(error) if known else 'watchdog_guard_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
