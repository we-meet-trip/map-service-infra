#!/usr/bin/env python3
"""Refuse a migration that the previous version of a service cannot survive.

While a deployment is in progress the old and the new container both talk to the
same database, so a migration may only add. Dropping or renaming a column, adding
a required column without a default, narrowing a type or dropping a table all
break the version that is still serving, and that break is not visible until a
request happens to touch it.

This reads migration files and answers whether they are additive. It changes
nothing and never connects to a database.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

# Each rule is (name, pattern, why). Comments and string literals are removed
# before matching so a rule cannot fire on a word inside a comment or a value.
RULES = [
    ('drop_table', r'\bDROP\s+TABLE\b',
     'the running version still reads this table'),
    ('drop_column', r'\bDROP\s+(?:COLUMN\b|CONSTRAINT\b)',
     'the running version still selects or relies on this'),
    ('rename', r'\bRENAME\s+(?:TO|COLUMN)\b',
     'a rename is a drop and an add at the same instant'),
    ('drop_schema_or_type', r'\bDROP\s+(?:SCHEMA|TYPE|SEQUENCE|INDEX)\b',
     'the running version may still depend on this object'),
    ('set_not_null', r'\bSET\s+NOT\s+NULL\b',
     'rows the running version writes would be rejected'),
    ('add_required_column', r'\bADD\s+(?:COLUMN\s+)?(?!.*\bDEFAULT\b)[^,;()]*\bNOT\s+NULL\b',
     'the running version does not supply this column'),
    ('narrowing_type', r'\bALTER\s+COLUMN\b[^;]*\bTYPE\b',
     'a type change can reject what the running version writes'),
    ('destructive_dml', r'\b(?:TRUNCATE|DELETE\s+FROM)\b',
     'existing rows must not disappear during a deployment'),
]
COMPILED = [(name, re.compile(pattern, re.IGNORECASE | re.DOTALL), why)
            for name, pattern, why in RULES]
ALLOW = re.compile(r'--\s*expand-gate:\s*allow\s+([a-z_]+)\s*(.*)', re.IGNORECASE)


def strip(text):
    """Remove comments and quoted text so a rule matches statements only."""
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.DOTALL)
    text = re.sub(r'--[^\n]*', ' ', text)
    text = re.sub(r"'(?:[^']|'')*'", "''", text)
    text = re.sub(r'\$([a-zA-Z_]*)\$.*?\$\1\$', ' ', text, flags=re.DOTALL)
    return text


def allowances(text):
    """An explicit, reasoned exception written beside the statement."""
    found = {}
    for line in text.splitlines():
        match = ALLOW.search(line)
        if match and match.group(2).strip():
            found[match.group(1).lower()] = match.group(2).strip()
    return found


def inspect(path):
    text = Path(path).read_text()
    granted = allowances(text)
    body = strip(text)
    findings = []
    for name, pattern, why in COMPILED:
        for match in pattern.finditer(body):
            statement = ' '.join(match.group(0).split())[:120]
            findings.append({'file': str(path), 'rule': name, 'statement': statement,
                             'why': why, 'allowed_because': granted.get(name)})
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='+', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    files = []
    for path in args.paths:
        files.extend(sorted(path.rglob('*.sql')) if path.is_dir() else [path])
    findings = [finding for path in files for finding in inspect(path)]
    blocking = [f for f in findings if not f['allowed_because']]
    out = {'status': 'PASS' if not blocking else 'FAIL', 'files_read': len(files),
           'findings': findings, 'blocking': len(blocking), 'database_connections': 0}
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        for finding in findings:
            mark = 'allowed' if finding['allowed_because'] else 'BLOCKS'
            print('%s %s %s: %s' % (mark, finding['file'], finding['rule'], finding['statement']))
        print('%d file(s) read, %d blocking' % (len(files), len(blocking)))
    return 0 if not blocking else 1


if __name__ == '__main__':
    sys.exit(main())
