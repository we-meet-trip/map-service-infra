#!/usr/bin/env python3
"""Mask coordinates in service log lines before a copy leaves the host.

One rule set for the request-log archive and the old-environment log archive, so both
copies are masked the same way. A masked value becomes *** and the name stays, so a
masked line still shows which parameter it carried.

First pass, applied in this order to the line as written:
  envelope  sealed location envelopes v1.<iv>.<ciphertext> (URL-safe base64) under any name
  hash      64-digit hex tokens (unsalted SHA-256 cache keys built from coordinates can be
            reversed by trying every point at the stored precision)
  osrm      coordinate lists in OSRM service paths /route/v1/<profile>/<lng>,<lat>;...
  named     numbers after a coordinate name: lat, lng, lon, latitude, longitude and any
            name ending in them (start_lat, endLng), x, y, SX, SY, EX, EY, mapx, mapy; as
            query or form pairs, JSON or dict items and plain name: value, also with the
            separator, quote or sign URL-encoded (%3D %3A %22 %27 %2B)
  net       standalone decimals with at least 3 decimals inside the latitudes 33-43 or
            longitudes 124-132 that cover Korea. A number after - or a URL or \\x escape
            counts as standalone; seconds of a hh:mm:ss time do not

residue() checks a copy with \\xNN and %NN escapes resolved once, using looser patterns: the
same envelopes and hashes, a coordinate name followed by a digit, and any decimal in the
Korean ranges with 2-3 integer digits (37.5 ms, Firefox/128.0) outside a hh:mm:ss time.
When the first pass leaves residue, scrub() masks with those looser patterns on the line as
written. If residue remains (escapes inside a number, values crafted so that one mask exposes
the next), it masks every escape, every number with a decimal point except IPv4 address[:port],
every hex run of 64 or more, envelopes and named values. Nothing is left for the decoded copy to
differ on and no step can bring back what an earlier one removed (see _strip), so residue()
finds nothing; the work per line stays a fixed number of passes and the client address in the
line is kept. WITHHELD (the whole line replaced) is a fail-safe that these steps never reach.

Scope: the line shapes the pinned production services write (hub uvicorn and httpx, user
Spring, agent, yolo, nginx proxy, Caddy edge). Shapes they never write are neither masked nor
detected: unnamed integer coordinates, Web Mercator metres, geohash, encoded polylines outside
OSRM paths, degree-minute-second text, scientific notation, decimal commas, JSON \\u escapes,
escapes nested more than one level. Re-check whenever a release changes what a service logs.

  coordinate_redaction.py           filter: stdin lines -> masked lines (counts on stderr)
  coordinate_redaction.py --check   detector only: counts lines with residue, exit 3 if any;
                                    for NDJSON records it checks the "line" field
"""
import json
import re
import sys
from urllib.parse import unquote

MASK = '***'
WITHHELD = '[withheld: coordinate-like content]'
# Docker's json-file driver cuts a message longer than this into fragments; `docker logs -t`
# prints every fragment with the first fragment's timestamp and no newline in between.
DOCKER_FRAGMENT_BYTES = 16 * 1024

ENVELOPE = re.compile(r'v1\.[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{16,}')
HEX64 = re.compile(r'(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])')
OSRM_PATH = re.compile(r'(/(?:route|table|nearest|match|trip|tile)/v1/[A-Za-z0-9_-]+/)([^?\s"\'&]+)')
NAME = r'(?:\b\w*?(?:latitude|longitude|lat|lng|lon)|\b(?:x|y|sx|sy|ex|ey|mapx|mapy))'
QUOTE = r'(?:["\']|%2[27])?'
NAMED = re.compile(r'(?i)(' + NAME + QUOTE + r'\s*(?:[=:]|%3[ad])\s*' + QUOTE + r'(?:\+|%2b)?)(-?\d+(?:\.\d+)?)')
NUMBER_START = r'(?<!\d\d:\d\d:)(?:(?<![\w.%])|(?<=%[0-9A-Fa-f]{2})|(?<=\\x[0-9A-Fa-f]{2}))'
DECIMAL = re.compile(NUMBER_START + r'(-?\d{2,3}\.\d{3,})(?!\d|\.\d)')

# The short names count after anything but a letter, so masking a digit in front of one never
# turns it into a new match.
LOOSE_NAMED = re.compile(r'(?i)((?:lat|lng|lon|latitude|longitude|mapx|mapy|(?<![a-z_])(?:x|y|sx|sy|ex|ey))'
                         r'["\']?\s*[=:]\s*["\']?[+-]?)(\d+(?:\.\d+)?)')
LOOSE_NUMBER = re.compile(r'(?<!\d\d:\d\d:)(?<![\d.])(-?\d{2,3}\.\d+)(?!\d|\.\d)')
ESCAPE = re.compile(r'\\x([0-9A-Fa-f]{2})')
ANY_ESCAPE = re.compile(r'%(?:[0-9A-Fa-f]{2})?|\\x')
NUMBER_RUN = re.compile(r'\d+(?:[.:]\d+)*')
HEX_RUN = re.compile(r'[0-9A-Fa-f]{64,}')
IPV4 = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?')


def _in_korea(text):
    value = abs(float(text))
    return 33 <= value <= 43 or 124 <= value <= 132


def _net(pattern, line):
    count = 0

    def mask(match):
        nonlocal count
        if not _in_korea(match[1]):
            return match[0]
        count += 1
        return MASK

    return pattern.sub(mask, line), count


def redact(line):
    """First pass. Return (masked line, number of masked values)."""
    line, envelopes = ENVELOPE.subn('v1.' + MASK, line)
    line, hashes = HEX64.subn(MASK, line)
    line, paths = OSRM_PATH.subn(lambda match: match[1] + MASK, line)
    line, named = NAMED.subn(lambda match: match[1] + MASK, line)
    line, numbers = _net(DECIMAL, line)
    return line, envelopes + hashes + paths + named + numbers


def _loose(line):
    """Mask what residue() finds in the line as written."""
    line, envelopes = ENVELOPE.subn('v1.' + MASK, line)
    line, hashes = HEX64.subn(MASK, line)
    line, numbers = _net(LOOSE_NUMBER, line)
    line, named = LOOSE_NAMED.subn(lambda match: match[1] + MASK, line)
    return line, envelopes + hashes + numbers + named


def _strip(line):
    """Mask so that no residue can remain. Each step replaces characters with *, which is no
    escape, digit or letter, so it only cuts runs and never joins them; a later step cannot
    bring back what an earlier one removed. No escape returns. A residue decimal must start a
    digit run that has a point and does not follow a point; after the number step only IPv4
    addresses are left, which hold none, and an address cut by a later mask leaves a run that
    follows a point. Every hex run of 64 or more was masked whole, so a later cut leaves runs
    under 64. A masked hash can free the short name behind it (fx=1 -> ***x=1); the name step
    runs last for that reason and cannot free another name, because a digit and * are both not
    letters."""
    line, count = ANY_ESCAPE.subn(MASK, line)

    def number(match):
        nonlocal count
        if '.' not in match[0] or IPV4.fullmatch(match[0]):
            return match[0]
        count += 1
        return MASK

    line = NUMBER_RUN.sub(number, line)
    line, hashes = HEX_RUN.subn(MASK, line)
    line, envelopes = ENVELOPE.subn('v1.' + MASK, line)
    line, named = LOOSE_NAMED.subn(lambda match: match[1] + MASK, line)
    return line, count + hashes + envelopes + named


def residue(line):
    """Names of the checks that still find a coordinate-like value (no values returned)."""
    text = unquote(ESCAPE.sub(r'%\1', line))
    found = []
    if ENVELOPE.search(text):
        found.append('envelope')
    if HEX64.search(text):
        found.append('hash')
    if LOOSE_NAMED.search(text):
        found.append('named')
    if any(_in_korea(match[1]) for match in LOOSE_NUMBER.finditer(text)):
        found.append('number')
    return found


def scrub(line):
    """Return (line, masked, withheld). The looser steps also take harmless numbers in the same
    ranges; the copy is usage evidence, so a lost figure is the price of never keeping a
    coordinate."""
    line, masked = redact(line)
    for step in (_loose, _strip):
        if not residue(line):
            return line, masked, False
        line, more = step(line)
        masked += more
    if residue(line):
        return WITHHELD, masked, True
    return line, masked, False


TIMESTAMP = re.compile(rb'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z ')


def join_fragments(record):
    """Rejoin one `docker logs -t` record (bytes, no trailing newline) split into fragments.

    Every fragment repeats the first timestamp prefix. A multi-byte character cut at a fragment
    edge is stored as U+FFFD bytes, which only lengthens the fragment and shifts the later edges,
    so the repeated prefixes are removed by value after the first fragment rather than at fixed
    offsets.
    """
    head = TIMESTAMP.match(record)
    if not head:
        return record
    prefix, body = head.group(0), record[head.end():]
    return prefix + body[:DOCKER_FRAGMENT_BYTES] + body[DOCKER_FRAGMENT_BYTES:].replace(prefix, b'')


def _text_of(raw):
    try:
        record = json.loads(raw)
    except ValueError:
        return raw
    return record['line'] if isinstance(record, dict) and isinstance(record.get('line'), str) else raw


if __name__ == '__main__':
    lines = masked = withheld = 0
    if sys.argv[1:] == ['--check']:
        for raw in sys.stdin:
            lines += 1
            withheld += bool(residue(_text_of(raw.rstrip('\n'))))
        print(f'MAP_REDACTION_CHECK lines={lines} residue={withheld}')
        sys.exit(3 if withheld else 0)
    for raw in sys.stdin:
        text, count, held = scrub(raw.rstrip('\n'))
        lines, masked, withheld = lines + 1, masked + count, withheld + held
        sys.stdout.write(text + '\n')
    print(f'MAP_REDACTION lines={lines} masked={masked} withheld={withheld}', file=sys.stderr)
