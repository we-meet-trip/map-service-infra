"""Coordinate masking for archived service logs: every known shape is masked, nothing else is."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import coordinate_redaction as cr

ENVELOPE = 'v1.AbCdEfGhIjKlMnOp.' + 'Q1w2E3r4T5y6U7i8O9p0-_aSdFgHjKl'

# Line shapes taken from the services' own log formats (hub uvicorn access and httpx request
# lines, nginx error lines, Caddy JSON errors, Spring and Python exceptions).
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
]

# Look like coordinates to a careless pattern but are not; they must pass through untouched.
CONTROL_LINES = [
    'INFO:     172.18.0.9:51234 - "GET /healthz HTTP/1.1" 200 OK',
    'client 37.123.45.6 connected, upstream 124.0.6367.60 user agent Chrome/124.0.6367.60',
    'GET /api/v1/users/me 401 duration=12.345 request_id=5f2c9a',
    'version 3.11.4 build 20261020.1 port 5000 retries 3',
    'format=json; flat=false; long text without numbers',
    'weather grid nx=60 ny=127 base_time=0500',
]


class RedactionTests(unittest.TestCase):
    def test_every_coordinate_shape_is_masked_with_nothing_left(self):
        for line in COORDINATE_LINES:
            with self.subTest(line=line[:60]):
                masked, count = cr.redact(line)
                self.assertGreater(count, 0)
                self.assertEqual(cr.residue(masked), [])
                for value in ('37.49', '127.02', '37.56', '126.97', '1270276100', 'AbCdEfGhIjKlMnOp'):
                    self.assertNotIn(value, masked)

    def test_names_stay_and_values_become_stars(self):
        masked, _ = cr.redact('GET /v2/local?x=127.027610&y=37.497950&radius=500')
        self.assertEqual(masked, 'GET /v2/local?x=***&y=***&radius=500')
        masked, _ = cr.redact(f'loc={ENVELOPE}&caller=user')
        self.assertEqual(masked, 'loc=v1.***&caller=user')
        masked, _ = cr.redact('/route/v1/foot/127.0276,37.4979;126.9780,37.5665?overview=full')
        self.assertEqual(masked, '/route/v1/foot/***?overview=full')

    def test_control_lines_pass_unchanged(self):
        for line in CONTROL_LINES:
            with self.subTest(line=line[:60]):
                self.assertEqual(cr.redact(line), (line, 0))
                self.assertEqual(cr.residue(line), [])

    def test_scrub_masks_harmless_numbers_in_the_coordinate_ranges_too(self):
        line = '2026-10-20T03:41:02.123456789Z took 37.5 ms; p95 0.127 s; heap 38.2%'
        self.assertEqual(cr.redact(line), (line, 0))
        self.assertEqual(cr.scrub(line), ('2026-10-20T03:41:02.123456789Z took *** ms; p95 0.127 s; heap ***%', 2, []))

    def test_scrub_leaves_nothing_in_any_known_shape(self):
        for line in COORDINATE_LINES + CONTROL_LINES:
            with self.subTest(line=line[:60]):
                self.assertEqual(cr.scrub(line)[2], [])
        for line in CONTROL_LINES:
            with self.subTest(control=line[:60]):
                self.assertEqual(cr.scrub(line), (line, 0, []))

    def test_residue_finds_what_the_narrow_net_leaves_and_the_wide_pass_masks_it(self):
        line = 'cache miss for 37.5,127.0 after 2 tries'
        masked, _ = cr.redact(line)
        self.assertEqual(masked, line)
        self.assertEqual(cr.residue(masked), ['number'])
        wide, count = cr.redact(masked, wide=True)
        self.assertEqual((wide, count), ('cache miss for ***,*** after 2 tries', 2))
        self.assertEqual(cr.residue(wide), [])

    def test_residue_reports_check_names_only(self):
        self.assertEqual(cr.residue(f'lat=37.5 {ENVELOPE} at 127.0276'), ['envelope', 'named', 'number'])


class FragmentTests(unittest.TestCase):
    def record(self, message):
        # The shape `docker logs -t` prints for one message longer than 16 KiB (checked against
        # Docker 29.8.0): each 16384-byte fragment gets its own timestamp and no newline between.
        parts = [message[i:i + cr.DOCKER_FRAGMENT_BYTES] for i in range(0, len(message), cr.DOCKER_FRAGMENT_BYTES)]
        stamps = [f'2026-10-06T15:59:00.93279117{i}Z '.encode() for i in range(len(parts))]
        return b''.join(stamp + part for stamp, part in zip(stamps, parts))

    def test_value_cut_at_a_fragment_edge_is_whole_again(self):
        message = b'a' * (cr.DOCKER_FRAGMENT_BYTES - 8) + b' lat=37.123456 ' + b'b' * 20000
        record = self.record(message)
        self.assertNotIn(b'lat=37.123456', record)
        joined = cr.join_fragments(record)
        self.assertEqual(joined, b'2026-10-06T15:59:00.932791170Z ' + message)
        masked, count = cr.redact(joined.decode())
        self.assertEqual((count, cr.residue(masked)), (1, []))

    def test_short_records_and_inner_timestamps_elsewhere_stay(self):
        record = b'2026-10-06T15:59:00.9Z text 2026-10-06T15:59:01Z inside'
        self.assertEqual(cr.join_fragments(record), record)
        self.assertEqual(cr.join_fragments(b'no timestamp'), b'no timestamp')

    def test_multibyte_text_split_inside_a_character_rejoins_exactly(self):
        message = b'a' * (cr.DOCKER_FRAGMENT_BYTES - 1) + '좌표 lat=37.123456'.encode() + b'z' * 100
        joined = cr.join_fragments(self.record(message))
        self.assertEqual(joined.split(b' ', 1)[1], message)


if __name__ == '__main__':
    unittest.main()
