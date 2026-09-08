#!/usr/bin/env python3
"""OSRM road-geometry acceptance using public landmark fixtures, never user data.

Run from the serving host. URLs must point to its private foot/bicycle engines.
Engine/dataset identity is checked separately by osrm-release.py and deployment.
"""
import argparse
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.request

# Public fixtures, longitude then latitude. The three Seoul points deliberately
# form an inefficient order so route service reordering would fail leg checks.
FIXTURES = {
    "seoul_manual_order": ((126.9780, 37.5665), (126.9850, 37.5740), (126.9820, 37.5700)),
    "busan": ((129.0756, 35.1796), (129.0815, 35.1730)),
    "jeju": ((126.5219, 33.4996), (126.5285, 33.5060)),
}


def meters(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    d = math.sin((lat2 - lat1) / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2)**2
    return 12742000 * math.asin(min(1, math.sqrt(d)))


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_route(data, points, profile):
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError("engine did not return a route")
    route = data["routes"][0]
    coords = route.get("geometry", {}).get("coordinates", [])
    if route.get("geometry", {}).get("type") != "LineString" or len(coords) < 10:
        raise ValueError("insufficient road geometry")
    if any(len(p) != 2 or not all(number(x) for x in p) or not (124 <= p[0] <= 132 and 32 <= p[1] <= 40) for p in coords):
        raise ValueError("invalid longitude/latitude geometry")
    if any(not number(route.get(k)) or route[k] <= 0 for k in ("distance", "duration")):
        raise ValueError("invalid distance/duration")
    waypoints = data.get("waypoints", [])
    if len(waypoints) != len(points) or len(route.get("legs", [])) != len(points) - 1:
        raise ValueError("waypoint/leg order contract mismatch")
    snaps = [meters(p, w["location"]) for p, w in zip(points, waypoints)]
    if max(snaps) > 150:
        raise ValueError("fixture snapped too far from requested point")
    if meters(coords[0], waypoints[0]["location"]) > 2 or meters(coords[-1], waypoints[-1]["location"]) > 2:
        raise ValueError("wrong start/end coordinate order")
    line_length = sum(meters(a, b) for a, b in zip(coords, coords[1:]))
    if abs(line_length - route["distance"]) > max(10, route["distance"] * .02):
        raise ValueError("geometry and reported distance disagree")
    modes, node_count = set(), 0
    for i, leg in enumerate(route["legs"]):
        steps = leg.get("steps", [])
        if not steps or meters(steps[0]["maneuver"]["location"], waypoints[i]["location"]) > 2 or meters(steps[-1]["maneuver"]["location"], waypoints[i + 1]["location"]) > 2:
            raise ValueError("route changed manual waypoint order")
        nodes = leg.get("annotation", {}).get("nodes", [])
        if len(set(nodes)) < 5 or any(type(n) is not int or n <= 0 for n in nodes):
            raise ValueError("missing OSM node annotation")
        node_count += len(nodes)
        modes.update(step.get("mode") for step in steps)
    allowed = {"walking"} if profile == "foot" else {"cycling", "pushing bike", "walking"}
    if not modes <= allowed or (profile == "bicycle" and "cycling" not in modes):
        raise ValueError("engine profile/movement mode mismatch")
    # A straight two-point placeholder, even densified, must fail. At least one
    # point has a measurable excess distance relative to its endpoint chord.
    chord = meters(coords[0], coords[-1])
    curvature = max(meters(coords[0], p) + meters(p, coords[-1]) - chord for p in coords)
    if curvature < 10:
        raise ValueError("fixture geometry is effectively a straight line")
    return {"distance_m": route["distance"], "duration_s": route["duration"],
            "data_version": data.get("data_version"),
            "geometry_points": len(coords), "geometry_length_m": round(line_length, 2),
            "curvature_excess_m": round(curvature, 2), "max_snap_m": round(max(snaps), 2),
            "osm_node_annotations": node_count, "step_modes": sorted(modes),
            "manual_order_preserved": True,
            "geometry_sha256": hashlib.sha256(json.dumps(coords, separators=(",", ":")).encode()).hexdigest()}


def request(base, points, profile):
    coords = ";".join(",".join(str(v) for v in p) for p in points)
    url = base.rstrip("/") + f"/route/v1/{profile}/{coords}?overview=full&geometries=geojson&steps=true&annotations=nodes,distance,duration&continue_straight=false"
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=15) as response:
        data = json.load(response)
    result = validate_route(data, points, profile)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--foot", required=True)
    parser.add_argument("--bicycle", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests", type=int, default=24)
    parser.add_argument("--data-version", help="require this graph source timestamp in every engine response")
    args = parser.parse_args()
    if not 1 <= args.requests <= 100:
        parser.error("requests must be 1..100; concurrency is fixed at 2")
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "fixtures": {}, "checks": []}
    for profile in ("foot", "bicycle"):
        base = getattr(args, profile)
        for name, points in FIXTURES.items():
            result = request(base, points, profile)
            if args.data_version and result["data_version"] != args.data_version:
                raise ValueError("engine graph source timestamp mismatch")
            report["fixtures"][profile + "/" + name] = result
    # Small bounded real-engine load, no account creation or provider API calls.
    started = time.monotonic()
    def probe(i):
        p = "foot" if i % 2 == 0 else "bicycle"
        return request(getattr(args, p), FIXTURES["seoul_manual_order"], p)["elapsed_ms"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        latencies = list(pool.map(probe, range(args.requests)))
    report["load"] = {"requests": len(latencies), "concurrency": 2, "errors": 0,
                      "elapsed_s": round(time.monotonic() - started, 2),
                      "p50_ms": round(statistics.median(latencies), 2), "max_ms": max(latencies)}
    report["status"] = "PASS"
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
