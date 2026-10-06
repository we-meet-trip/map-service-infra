#!/usr/bin/env python3
"""Mask coordinates in service log lines before a copy leaves the host.

One rule set for the request-log archive and the old-environment log archive, so both
copies are masked the same way. A masked value becomes *** and the name stays, so a
masked line still shows which parameter it carried.

Rules, applied in this order to one line:
  envelope  sealed location envelopes v1.<iv>.<ciphertext> (URL-safe base64) under any name
  osrm      coordinate lists in OSRM service paths /route/v1/<profile>/<lng>,<lat>;...
  named     numbers after a coordinate name: lat, lng, lon, latitude, longitude and any
            name ending in them (start_lat, endLng), x, y, SX, SY, EX, EY, mapx, mapy;
            as query or form pairs (name=v), JSON or dict items ("name": v, 'name': 'v')
            and plain name: v
  net       standalone decimals with at least 3 decimals inside the latitudes 33-43 or
            longitudes 124-132 that cover Korea, whatever the context

residue() is a separate, wider check: any envelope, any coordinate name still followed by
a number, and any standalone decimal with at least 1 decimal in those ranges. A caller
masks once more with the wider net (redact(line, wide=True)) and drops the line batch if
residue remains.
"""
import re
import sys

MASK = '***'
# Docker's json-file reader prints an over-long message as fragments of this many bytes, each
# with its own timestamp prefix and no newline in between (`docker logs -t`).
DOCKER_FRAGMENT_BYTES = 16 * 1024

ENVELOPE = re.compile(r'\bv1\.[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{16,}')
OSRM_PATH = re.compile(r'(/(?:route|table|nearest|match|trip|tile)/v1/[A-Za-z0-9_-]+/)([^?\s"\'&]+)')
NAME = r'(?:\b\w*?(?:latitude|longitude|lat|lng|lon)|\b(?:x|y|sx|sy|ex|ey|mapx|mapy))'
NAMED = re.compile(r'(?i)(' + NAME + r'["\']?\s*[=:]\s*["\']?)(-?\d+(?:\.\d+)?)')
# A number starts after a non-word character other than . % -, or right after a URL-encoded
# comma, semicolon or space (127.0276%2C37.4979), and does not run on into more digits or dots
# (so IPv4 octets and version strings stay out).
NUMBER_START = r'(?:(?<![\w.%-])|(?<=%2[Cc])|(?<=%3[Bb])|(?<=%20))'
DECIMAL_NARROW = re.compile(NUMBER_START + r'(-?\d{2,3}\.\d{3,})(?![\d.])')
DECIMAL_WIDE = re.compile(NUMBER_START + r'(-?\d{2,3}\.\d+)(?![\d.])')


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


def redact(line, wide=False):
    """Return (masked line, number of masked values)."""
    line, envelopes = ENVELOPE.subn('v1.' + MASK, line)
    line, paths = OSRM_PATH.subn(lambda match: match[1] + MASK, line)
    line, named = NAMED.subn(lambda match: match[1] + MASK, line)
    line, numbers = _net(DECIMAL_WIDE if wide else DECIMAL_NARROW, line)
    return line, envelopes + paths + named + numbers


def residue(line):
    """Names of the checks that still find a coordinate-like value (no values returned)."""
    found = []
    if ENVELOPE.search(line):
        found.append('envelope')
    if NAMED.search(line):
        found.append('named')
    if any(_in_korea(match[1]) for match in DECIMAL_WIDE.finditer(line)):
        found.append('number')
    return found


def scrub(line):
    """Mask, check, mask once more with the wide net if needed. Returns (line, masked, residue).

    The wide pass also masks harmless numbers in the same ranges (37.5 ms, 38.2 %); the copy is
    for usage evidence, so a lost latency figure is the price of never keeping a coordinate.
    """
    line, masked = redact(line)
    if residue(line):
        line, more = redact(line, wide=True)
        masked += more
    return line, masked, residue(line)


TIMESTAMP = re.compile(rb'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z ')


def join_fragments(record):
    """Rejoin one `docker logs -t` record (bytes, no trailing newline) split into fragments.

    The first timestamp prefix stays; each later one sits exactly DOCKER_FRAGMENT_BYTES of
    message content after the previous fragment started and is removed, so a value cut at a
    fragment edge is whole again before masking.
    """
    head = TIMESTAMP.match(record)
    if not head:
        return record
    body = record[head.end():]
    position = DOCKER_FRAGMENT_BYTES
    while len(body) > position:
        inner = TIMESTAMP.match(body, position)
        if not inner:
            break
        body = body[:position] + body[inner.end():]
        position += DOCKER_FRAGMENT_BYTES
    return record[:head.end()] + body


if __name__ == '__main__':
    # Filter: mask stdin to stdout; exit 3 when anything remains after the wide pass.
    remaining = 0
    for raw in sys.stdin:
        text, _, left = scrub(raw.rstrip('\n'))
        remaining += bool(left)
        sys.stdout.write(text + '\n')
    sys.exit(3 if remaining else 0)
