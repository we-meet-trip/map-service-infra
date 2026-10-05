#!/usr/bin/env python3
"""Refuse an OpenTofu plan that would destroy state-bearing resources or switch production off.

Reads `tofu show -json <planfile>` output (file argument or stdin), prints every changing resource as
"<actions> <address>" without attribute values, then one "BLOCK" line per forbidden change. --frozen
(apply-freeze window) also refuses switching an existing web-public rule or alert policy on.
Exit status: 0 clean, 1 blocked, 2 when the input is not a plan.
"""
import argparse
import json
import sys

# Deleting or replacing these loses data, history, the published address or the keyless deploy trust path.
PROTECTED = frozenset({
    'google_compute_address',
    'google_compute_disk',
    'google_compute_instance',
    'google_storage_bucket',
    'google_logging_project_bucket_config',
    'google_iam_workload_identity_pool',
    'google_iam_workload_identity_pool_provider',
})


def summary(plan):
    """Action and address of each changing resource; safe to paste into a review."""
    return [f"{'/'.join(item['change']['actions'])} {item['address']}" for item in plan.get('resource_changes', [])
            if item['change']['actions'] not in (['no-op'], ['read'])]


def violations(plan, frozen=False):
    found = []
    for item in plan.get('resource_changes', []):
        kind, address = item['type'], item['address']
        actions = item['change']['actions']
        before = item['change'].get('before')
        after = item['change'].get('after') or {}
        deleted = 'delete' in actions
        if kind in PROTECTED and deleted:
            found.append(f"{address}: {'/'.join(actions)} would delete it")
        if before is None:
            # A first creation may start disabled; only changing an existing switch is refused.
            continue
        if kind == 'google_compute_firewall' and str(before.get('name', '')).endswith('-web-public'):
            off_before, off_after = before.get('disabled') is True, after.get('disabled') is True
            if deleted or (off_after and not off_before):
                found.append(f'{address}: web-public would stop serving')
            elif frozen and off_before != off_after:
                found.append(f'{address}: web-public would start serving while frozen')
        if kind == 'google_monitoring_alert_policy':
            # A missing `enabled` is the API default, which is on.
            on_before, on_after = before.get('enabled') is not False, after.get('enabled') is not False
            if on_before and (deleted or not on_after):
                found.append(f'{address}: alert policy would stop alerting')
            elif frozen and (deleted or on_before != on_after):
                found.append(f'{address}: alert policy would change while frozen')
    return found


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--frozen', action='store_true', help='also refuse turning web-public or an alert policy on')
    parser.add_argument('plan', nargs='?', help='`tofu show -json <planfile>` output (default: stdin)')
    args = parser.parse_args(argv[1:])
    try:
        if args.plan:
            with open(args.plan, encoding='utf-8') as handle:
                plan = json.load(handle)
        else:
            plan = json.load(sys.stdin)
    except (OSError, ValueError) as error:
        print(f'cannot read plan JSON: {error}', file=sys.stderr)
        return 2
    # `tofu show -json` without a plan file prints state, which would pass vacuously.
    if not isinstance(plan, dict) or 'planned_values' not in plan:
        print('input is not `tofu show -json <planfile>` output', file=sys.stderr)
        return 2
    for line in summary(plan):
        print(line)
    blocked = violations(plan, frozen=args.frozen)
    for reason in blocked:
        print(f'BLOCK {reason}')
    return 1 if blocked else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
