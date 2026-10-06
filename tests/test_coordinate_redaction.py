"""Coordinate masking for archived service logs: every known shape is masked, nothing else is."""
import json
from pathlib import Path
import random
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import coordinate_redaction as cr

ENVELOPE = 'v1.AbCdEfGhIjKlMnOp.' + 'Q1w2E3r4T5y6U7i8O9p0-_aSdFgHjKl'
CACHE_KEY = '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08'

# Line shapes taken from the services' own log formats (hub uvicorn access and httpx request
# lines, nginx access and error lines, Caddy JSON errors, Spring and Python exceptions).
COORDINATE_LINES = [
    f'INFO:     172.18.0.9:51234 - "GET /v1/weather/now?loc={ENVELOPE}&caller=user HTTP/1.1" 200 OK',
    'HTTP Request: GET https://dapi.kakao.com/v2/local/search/keyword.json?query=cafe&x=127.027610&y=37.497950&radius=500 "HTTP/1.1 200 OK"',
    'HTTP Request: GET https://api.odsay.com/v1/api/searchPubTransPathT?SX=126.9780&SY=37.5665&EX=127.0276&EY=37.4979&apiKey=*** "HTTP/1.1 200 OK"',
    'HTTP Request: GET http://osrm-foot:5000/route/v1/foot/127.0276,37.4979;126.9780,37.5665?overview=full "HTTP/1.1 200 OK"',
    'HTTP Request: GET http://osrm-bicycle:5000/route/v1/bicycle/127.0276%2C37.4979%3B126.978%2C37.5665?steps=false "HTTP/1.1 200 OK"',
    '2026/10/20 03:41:02 [error] 31#31: *7 upstream timed out, client: 172.18.0.1, request: "GET /api/v1/weather/now?lat=37.5665&lng=126.9780 HTTP/1.1", host: "api.mapservice.app"',
    '{"level":"error","ts":1791259037.501,"logger":"http.log.error","msg":"dial tcp: i/o timeout","request":{"uri":"/api/v1/bikes/nearby?latitude=37.49795&longitude=127.02761"}}',
    "params={'start_lat': 37.4979, 'start_lng': 127.0276, 'endLat': '37.5665', 'endLng': '126.978'}",
    '{"x": 127.027610, "y": 37.497950, "mapx": 1270276100, "mapy": 374979500}',
    'Failed to convert value of type String to double; lat: 37.4979x',
    'origin at 37.497950, 127.027610 out of service area',
    'route=[[127.02761,37.49795],[126.97800,37.56650]]',
    f'WARNING cache write failed key=kakao:nearby:{CACHE_KEY}:verified-v2:live',
    '203.0.113.5 - - [20/Oct/2026:03:41:02 +0900] "GET /api/v1/places?q=%3Flat%3D37.4979%26lng%3D127.0276 HTTP/1.1" 200 512',
    '203.0.113.5 - - [20/Oct/2026:03:41:02 +0900] "GET /api/v1/places?lat=%2B37.4979&lng=%2B127.0276 HTTP/1.1" 200 512',
    '203.0.113.5 - - [20/Oct/2026:03:41:02 +0900] "POST /api/v1/trip?d=%7B%22lat%22%3A37.4979%2C%22lng%22%3A127.0276%7D HTTP/1.1" 400 0',
    '203.0.113.5 - - [20/Oct/2026:03:41:02 +0900] "GET /x?q=\\x22lat\\x22:37.4979,\\x22lng\\x22:127.0276 HTTP/1.1" 400 0',
    f'token=loc{ENVELOPE}&next=1',
    'origin 37.4979.',
]

# Values a client can put in a URL or header; none may survive and none may withhold the line.
CRAFTED_LINES = [
    '203.0.113.5 - - [20/Oct/2026:03:41:02 +0900] "GET / HTTP/1.1" 200 0 "-" "33.0-33.0"',
    'GET /search?q=37.5x=5',
    'HTTP Request: GET https://dapi.kakao.com/v2/local/search/keyword.json?query=37.5-38.5',
    'GET /a?b=a37.5&c=127.0',
    'GET /a?q=%2037.5',
    'GET /a?q=37%2E5',
    'GET /a?x=12:34:37.5',
    'GET /a?q=37.512:34:37.512:34:37.512:34:37.5',
    'GET /a?' + 'x=5' * 40,
    'GET /a?q=37' + '%' + '25' * 20 + '2E5',
    'GET /a?' + ''.join(f'37%{"25" * depth}2E5,' for depth in range(20)),
    'GET /a?q=' + 'ab' * 31 + 'fx=37',
    'GET /a?q=\\x337.5',
    # A name masked in the last step must not leave an exact 64-digit hash behind it.
    '203.0.113.5 - - "GET /api/v1/trip/generate?q=%5Cx=5' + 'a' * 64 + ' HTTP/1.1" 503',
    'GET /a?q=%5Cx=5' + 'a' * 64 + 'x=7' + 'b' * 64,
]

# Look like coordinates to a careless pattern but are not; they must pass through untouched.
CONTROL_LINES = [
    'INFO:     172.18.0.9:51234 - "GET /healthz HTTP/1.1" 200 OK',
    'client 37.123.45.6 connected, upstream 124.0.6367.60 user agent Chrome/124.0.6367.60',
    'GET /api/v1/users/me 401 duration=12.345 request_id=5f2c9a',
    'version 3.11.4 build 20261020.1 port 5000 retries 3',
    'format=json; flat=false; long text without numbers',
    'weather grid nx=60 ny=127 base_time=0500',
    '2026-10-06T05:00:37.183Z  INFO 1 --- [nio-8080-exec-1] c.m.user.AuthController  : login ok',
    '2026-10-06T05:00:41.183456Z WARN retry 3 of 5 max: 7 index: 2 key: 4',
]

COORDINATE_VALUES = ('37.49', '127.02', '37.56', '126.97', '1270276100', 'AbCdEfGhIjKlMnOp', CACHE_KEY[:16])


class RedactionTests(unittest.TestCase):
    def test_every_coordinate_shape_is_masked_with_nothing_left(self):
        for line in COORDINATE_LINES:
            with self.subTest(line=line[:60]):
                masked, count, withheld = cr.scrub(line)
                self.assertGreater(count, 0)
                self.assertFalse(withheld)
                self.assertEqual(cr.residue(masked), [])
                for value in COORDINATE_VALUES:
                    self.assertNotIn(value, masked)

    def test_the_first_pass_alone_covers_the_shapes_services_write(self):
        for line in COORDINATE_LINES:
            with self.subTest(line=line[:60]):
                masked, _ = cr.redact(line)
                self.assertEqual(cr.residue(masked), [])

    def test_names_stay_and_values_become_stars(self):
        self.assertEqual(cr.redact('GET /v2/local?x=127.027610&y=37.497950&radius=500')[0],
                         'GET /v2/local?x=***&y=***&radius=500')
        self.assertEqual(cr.redact(f'loc={ENVELOPE}&caller=user')[0], 'loc=v1.***&caller=user')
        self.assertEqual(cr.redact('/route/v1/foot/127.0276,37.4979;126.9780,37.5665?overview=full')[0],
                         '/route/v1/foot/***?overview=full')
        self.assertEqual(cr.redact(f'key=kakao:nearby:{CACHE_KEY}:verified-v2:live')[0],
                         'key=kakao:nearby:***:verified-v2:live')

    def test_control_lines_pass_unchanged(self):
        for line in CONTROL_LINES:
            with self.subTest(line=line[:60]):
                self.assertEqual(cr.redact(line), (line, 0))
                self.assertEqual(cr.residue(line), [])
                self.assertEqual(cr.scrub(line), (line, 0, False))

    def test_harmless_numbers_in_the_coordinate_ranges_are_masked_too(self):
        line = '2026-10-20T03:41:02.123456789Z took 37.5 ms; p95 0.127 s; heap 38.2% rv:128.0 Firefox/128.0'
        self.assertEqual(cr.redact(line), (line, 0))
        self.assertEqual(cr.residue(line), ['number'])
        self.assertEqual(cr.scrub(line), ('2026-10-20T03:41:02.123456789Z took *** ms; p95 0.127 s; heap ***% '
                                          'rv:*** Firefox/***', 4, False))

    def test_crafted_values_are_masked_without_withholding_the_line(self):
        for line in CRAFTED_LINES:
            with self.subTest(line=line[:60]):
                masked, _, withheld = cr.scrub(line)
                self.assertFalse(withheld)
                self.assertEqual(cr.residue(masked), [])
        self.assertEqual(cr.scrub('GET /search?q=37.5x=5')[0], 'GET /search?q=***x=***')
        self.assertEqual(cr.scrub('query=37.5-38.5')[0], 'query=***-***')

    def test_the_last_step_keeps_the_client_address(self):
        line = '203.0.113.5 - - [20/Oct/2026:03:41:02 +0900] "GET /a?q=37%2E5 HTTP/1.1" 200 0 "-" "Mozilla/5.0"'
        self.assertEqual(cr.residue(cr.redact(line)[0]), ['number'])
        masked, _, withheld = cr.scrub(line)
        self.assertFalse(withheld)
        self.assertTrue(masked.startswith('203.0.113.5 - - [20/Oct/2026:03:41:02 +0900] "GET /a?q=37***5 HTTP/'))
        self.assertEqual(cr.residue(masked), [])

    def test_no_input_leaves_residue_or_reaches_the_fail_safe(self):
        # Pieces that interact: escapes, digits that a mask can glue or split, short names that
        # a mask can free, hex runs a mask can cut to 64, time-of-day contexts.
        pieces = ['0', '1', '3', '5', '7', '12', '37', '127', '.', ':', '%', '%2E', '%25', '%3D', '%33', '\\x',
                  '\\x2E', '\\x33', '2E', 'x', 'X', 'y', 'sx', 'lat', 'lng', 'mapx', '=', '"', "'", ' ', '-', '+',
                  'a', 'f', 'v1.', 'AbCdEfGhIjKlMnOp', '*', '/', ',', 'abcdef0123456789' * 4, '0123456789abcdef' * 4,
                  '12:34:', '05:00:37.183Z', 'x=5', '37.5', 'lat=']
        # A narrow set makes the rarer chains (escaped name, then a long hex run) common.
        narrow = ['%5C', '\\x', 'x=5', 'fx=', '=', '5', '.', ':', 'a' * 64, 'a' * 63, '1.2.3.4', '37.5', '12:34:',
                  'v1.', 'AbCdEfGhIjKlMnOp', 'lat=', '*', ' ']
        rng = random.Random(20261007)
        for alphabet in (pieces, narrow):
            for _ in range(30000):
                line = ''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
                masked, _, withheld = cr.scrub(line)
                self.assertFalse(withheld, line)
                self.assertEqual(cr.residue(masked), [], line)

    def test_residue_reports_check_names_only(self):
        self.assertEqual(cr.residue(f'lat=37.5 {ENVELOPE} at 127.0276 {CACHE_KEY}'),
                         ['envelope', 'hash', 'named', 'number'])
        self.assertEqual(cr.residue('q=%2037.5'), ['number'])


class CommandTests(unittest.TestCase):
    def run_module(self, text, *args):
        return subprocess.run([sys.executable, '-B', str(ROOT / 'scripts' / 'coordinate_redaction.py'), *args],
                              input=text, capture_output=True, text=True)

    def test_filter_masks_every_line_and_counts_on_stderr(self):
        done = self.run_module('lat=37.4979\nhealthz ok\ntook 37.5 ms\n')
        self.assertEqual((done.returncode, done.stdout), (0, 'lat=***\nhealthz ok\ntook *** ms\n'))
        self.assertEqual(done.stderr.strip(), 'MAP_REDACTION lines=3 masked=2 withheld=0')

    def test_check_reads_the_line_field_of_archive_records_and_fails_on_residue(self):
        records = [{'ts': '2026-10-20T03:41:02.000000000Z', 'service': 'proxy', 'stream': 'stdout',
                    'line': 'GET /a?lat=*** 200'},
                   {'ts': '2026-10-20T03:41:03.000000000Z', 'service': 'proxy', 'stream': 'stdout',
                    'line': 'GET /a?lat=37.4979 200'},
                   # Only the decoded field shows this one: in the record text the quote is escaped.
                   {'ts': '2026-10-20T03:41:04.000000000Z', 'service': 'hub', 'stream': 'stdout',
                    'line': '{"x": 127, "y": 37}'}]
        done = self.run_module(''.join(json.dumps(record) + '\n' for record in records), '--check')
        self.assertEqual((done.returncode, done.stdout.strip()), (3, 'MAP_REDACTION_CHECK lines=3 residue=2'))
        self.assertEqual(cr.residue(json.dumps(records[2])), [])
        done = self.run_module(json.dumps(records[0]) + '\n', '--check')
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'MAP_REDACTION_CHECK lines=1 residue=0'))


def docker_record(message, prefix=b'2026-10-06T15:59:00.932791171Z '):
    """What `docker logs -t` prints for one message (checked against Docker 29.8.0): 16384-byte
    fragments of the original bytes, each stored as JSON text (a cut multi-byte character turns
    into U+FFFD) and printed with the same timestamp prefix and no newline in between."""
    parts = [message[i:i + cr.DOCKER_FRAGMENT_BYTES] for i in range(0, len(message), cr.DOCKER_FRAGMENT_BYTES)]
    stored = [part.decode('utf-8', 'replace').encode() for part in parts]
    return b''.join(prefix + part for part in stored), b''.join(stored)


class FragmentTests(unittest.TestCase):
    def test_value_cut_at_a_fragment_edge_is_whole_again(self):
        message = b'a' * (cr.DOCKER_FRAGMENT_BYTES - 8) + b' lat=37.123456 ' + b'b' * 20000
        record, stored = docker_record(message)
        self.assertNotIn(b'lat=37.123456', record)
        joined = cr.join_fragments(record)
        self.assertEqual(joined, b'2026-10-06T15:59:00.932791171Z ' + message)
        masked, count = cr.redact(joined.decode())
        self.assertEqual((count, cr.residue(masked)), (1, []))

    def test_a_cut_character_shifts_later_edges_and_the_join_still_holds(self):
        # The first edge cuts a 3-byte character after its first byte, the second edge falls
        # inside a coordinate.
        head = b'a' * (cr.DOCKER_FRAGMENT_BYTES - 1) + '좌'.encode()
        message = head + b'b' * (2 * cr.DOCKER_FRAGMENT_BYTES - len(head) - 6) + b'lat=37.123456' + b'c' * 100
        record, stored = docker_record(message)
        prefix = b'2026-10-06T15:59:00.932791171Z '
        self.assertEqual(record.index(prefix, 1), len(prefix) + cr.DOCKER_FRAGMENT_BYTES + 2)
        self.assertNotIn(b'lat=37.123456', record)
        joined = cr.join_fragments(record)
        self.assertEqual(joined, prefix + stored)
        text = joined.decode()
        self.assertIn('lat=37.123456', text)
        self.assertEqual(cr.scrub(text)[0].count('lat=***'), 1)

    def test_short_records_and_inner_timestamps_elsewhere_stay(self):
        record = b'2026-10-06T15:59:00.9Z text 2026-10-06T15:59:01Z inside'
        self.assertEqual(cr.join_fragments(record), record)
        self.assertEqual(cr.join_fragments(b'no timestamp'), b'no timestamp')


if __name__ == '__main__':
    unittest.main()
