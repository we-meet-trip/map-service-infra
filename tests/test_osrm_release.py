import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name.replace("_", "-") + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


release = module("osrm_release")
acceptance = module("osrm_acceptance")
systemd = module("osrm_systemd")


class HostUnitsTest(unittest.TestCase):
    def test_units_require_verified_readonly_graph_and_only_stop_routing(self):
        units = systemd.render("test", "/var/lib/map-osrm/release-r1", "/opt/map-service-infra", "map-test-net", 5200, 5201)
        mount = units["srv-map\\x2dosrm\\x2dtest.mount"]
        service = units["map-osrm-test.service"]
        self.assertIn("Options=loop,ro,nodev,nosuid,noexec", mount)
        self.assertIn("BindsTo=srv-map\\x2dosrm\\x2dtest.mount", service)
        self.assertIn("ExecStartPre=/usr/bin/python3", service)
        self.assertIn(" verify-tree ", service)
        self.assertIn("--project-name map-routing-test", service)
        self.assertIn("stop --timeout 15", service)
        self.assertNotIn(" down", service)

    def test_prod_cannot_reuse_test_network(self):
        with self.assertRaisesRegex(ValueError, "mixed serving"):
            systemd.render("prod", "/release", "/infra", "map-test-net", 5200, 5201)

    def test_unit_path_injection_and_colliding_ports_rejected(self):
        for path, ports in (("/release\nExecStart=/bin/false", (5200, 5201)), ("/release/../other", (5200, 5201)), ("/release", (5200, 5200))):
            with self.subTest(path=path), self.assertRaises(ValueError):
                systemd.render("test", path, "/infra", "map-test-net", *ports)


class ReleaseTest(unittest.TestCase):
    def make(self, path, *, missing=None, extra=None, mode=0o644):
        files = {}
        with tarfile.open(path, "w:gz") as tar:
            for name in release.RUNTIME_FILES:
                content = name.encode()
                files[name] = {"bytes": len(content), "sha256": release.hashlib.sha256(content).hexdigest()}
                if name == missing:
                    continue
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = mode
                tar.addfile(info, io.BytesIO(content))
            if extra:
                tar.addfile(extra, io.BytesIO(b""))
        return {"schema": 1, "engine_image": release.IMAGE, "engine_version": release.VERSION,
                "algorithm": "mld", "runtime_files": files,
                "profiles": {p: {"sha256": release.PROFILE_HASHES[p]} for p in release.PROFILES},
                "archive": {"sha256": release.digest(path)}}

    def test_complete_runtime_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.tar.gz"
            release.verify_archive(path, self.make(path))

    def test_incomplete_geometry_cannot_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.tar.gz"
            with self.assertRaisesRegex(ValueError, "missing archive"):
                release.verify_archive(path, self.make(path, missing="foot/korea.osrm.geometry"))

    def test_path_traversal_symlink_duplicate_rejected(self):
        for name, kind in (("../escape", tarfile.REGTYPE), (release.RUNTIME_FILES[0], tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "runtime.tar.gz"
                extra = tarfile.TarInfo(name)
                extra.type = kind
                extra.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
                with self.assertRaisesRegex(ValueError, "unexpected/duplicate"):
                    release.verify_archive(path, self.make(path, extra=extra))

    def test_corrupted_file_digest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.tar.gz"
            manifest = self.make(path)
            manifest["runtime_files"][release.RUNTIME_FILES[0]]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "member checksum"):
                release.verify_archive(path, manifest)

    def test_engine_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.tar.gz"
            manifest = self.make(path)
            manifest["engine_image"] = "osrm:latest"
            with self.assertRaisesRegex(ValueError, "engine/schema"):
                release.verify_archive(path, manifest)

    def test_root_only_generated_file_index_mode_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.tar.gz"
            manifest = self.make(path, mode=0o700)
            with self.assertRaisesRegex(ValueError, "nonroot-readable"):
                release.verify_archive(path, manifest)

    def test_profile_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.tar.gz"
            manifest = self.make(path)
            manifest["profiles"]["foot"]["sha256"] = release.PROFILE_HASHES["bicycle"]
            with self.assertRaisesRegex(ValueError, "profile checksum"):
                release.verify_archive(path, manifest)

    def test_mounted_tree_exact_files_and_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / "runtime.tar.gz"
            manifest = self.make(archive)
            tree = base / "mounted"
            for name in release.RUNTIME_FILES:
                path = tree / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
            release.verify_tree(tree, manifest)
            (tree / release.RUNTIME_FILES[0]).write_bytes(b"corruption")
            with self.assertRaisesRegex(ValueError, "checksum/size"):
                release.verify_tree(tree, manifest)


class RouteTest(unittest.TestCase):
    def sample(self, *, straight=False):
        points = [(126.978 + i * .0002, 37.5665 + (0 if straight else (.0005 if 2 <= i <= 7 else 0))) for i in range(11)]
        steps = [{"mode": "walking", "maneuver": {"location": p}} for p in (points[0], points[-1])]
        data = {"code": "Ok", "waypoints": [{"location": p} for p in (points[0], points[-1])], "routes": [{
            "distance": sum(acceptance.meters(a, b) for a, b in zip(points, points[1:])), "duration": 400,
            "geometry": {"type": "LineString", "coordinates": points},
            "legs": [{"steps": steps, "annotation": {"nodes": list(range(1, 12))}}]}]}
        return data, (points[0], points[-1])

    def test_road_geometry_and_profile(self):
        data, points = self.sample()
        self.assertTrue(acceptance.validate_route(data, points, "foot")["manual_order_preserved"])

    def test_densified_straight_placeholder_rejected(self):
        data, points = self.sample(straight=True)
        with self.assertRaisesRegex(ValueError, "straight"):
            acceptance.validate_route(data, points, "foot")

    def test_wrong_profile_rejected(self):
        data, points = self.sample()
        with self.assertRaisesRegex(ValueError, "profile/movement"):
            acceptance.validate_route(data, points, "bicycle")

    def test_geometry_distance_mismatch_rejected(self):
        data, points = self.sample()
        data["routes"][0]["distance"] *= 2
        with self.assertRaisesRegex(ValueError, "reported distance"):
            acceptance.validate_route(data, points, "foot")

    def test_manual_order_mutation_rejected(self):
        data, points = self.sample()
        data["routes"][0]["legs"][0]["steps"].reverse()
        with self.assertRaisesRegex(ValueError, "manual waypoint order"):
            acceptance.validate_route(data, points, "foot")

    def test_latitude_longitude_swap_rejected(self):
        data, points = self.sample()
        data["routes"][0]["geometry"]["coordinates"] = [p[::-1] for p in data["routes"][0]["geometry"]["coordinates"]]
        with self.assertRaisesRegex(ValueError, "longitude/latitude"):
            acceptance.validate_route(data, points, "foot")

    def test_nan_duration_rejected(self):
        data, points = self.sample()
        data["routes"][0]["duration"] = float("nan")
        with self.assertRaisesRegex(ValueError, "distance/duration"):
            acceptance.validate_route(data, points, "foot")
