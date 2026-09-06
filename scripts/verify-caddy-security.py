#!/usr/bin/env python3
"""Verify a local, explicitly built Caddy security image without live-service calls.

Requires an existing local upstream image, patched image and its Trivy JSON report.
Runs disposable network-none containers with no host volumes; never deploys an image.
--self-test exercises evidence rejection without Docker or network access.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

UPSTREAM = "caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
GO_VERSION = "go1.26.8"
EXPECTED = {
    "github.com/caddyserver/caddy/v2": "v2.11.4",
    "golang.org/x/crypto": "v0.55.0",
    "golang.org/x/net": "v0.57.0",
    "golang.org/x/text": "v0.41.0",
    "google.golang.org/grpc": "v1.83.1",
}
REQUIRED_MODULES = {"http.handlers.reverse_proxy", "tls.issuance.acme"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(args, *, data=None, timeout=90):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    require(result.returncode == 0, "isolated verification command failed")
    return result.stdout.strip()


def isolated(image, entrypoint="caddy"):
    return ["docker", "run", "--rm", "--platform", "linux/amd64", "--pull", "never", "--network", "none", "--read-only",
            "--security-opt", "no-new-privileges", "--tmpfs", "/data", "--tmpfs", "/config",
            "--tmpfs", "/tmp", "--entrypoint", entrypoint, image]


def metadata(image):
    template = '{"id":{{json .Id}},"os":{{json .Os}},"architecture":{{json .Architecture}}}'
    value = json.loads(run(["docker", "image", "inspect", "--platform", "linux/amd64", "--format", template, image]))
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", value.get("id", "")), "invalid local image identity")
    require(value.get("os") == "linux" and value.get("architecture") == "amd64", "linux/amd64 image required")
    # Docker's containerd store can expose the image-index ID without --platform
    # and its child/config ID with --platform. Bind both through the same immutable
    # parent inspection; running a bare child ID need not work in that image store.
    parent = run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", parent), "invalid parent image identity")
    proven = json.loads(run(["docker", "image", "inspect", "--platform", "linux/amd64", "--format", template, parent]))
    require(proven == value, "image changed during platform identity verification")
    return {**value, "id": parent, "platform_image_id": value["id"]}


def module_names(output):
    modules = {line.strip() for line in output.splitlines()
               if re.fullmatch(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*", line.strip())}
    require(REQUIRED_MODULES <= modules, "standard reverse proxy/TLS modules missing")
    return modules


def verify_buildinfo(output):
    lines = output.splitlines()
    require(lines and lines[0].endswith(": " + GO_VERSION), "unexpected Go toolchain")
    dependencies = {}
    settings = {}
    for line in lines[1:]:
        fields = line.strip().split("\t")
        require(fields[0] != "=>", "replaced modules are not allowed")
        if fields[0] == "dep" and len(fields) >= 3:
            dependencies[fields[1]] = fields[2]
        if fields[0] == "build" and len(fields) == 2:
            key, _, value = fields[1].partition("=")
            settings[key] = value
    require(all(dependencies.get(name) == version for name, version in EXPECTED.items()), "source/security dependency version mismatch")
    require(settings.get("GOOS") == "linux" and settings.get("GOARCH") == "amd64"
            and settings.get("CGO_ENABLED") == "0", "unexpected binary platform")
    return dependencies


def verify_report(report, image_id, platform_image_id=None):
    require(report.get("ArtifactType") == "container_image", "container scan required")
    # The optional child is supplied only by metadata()'s same-parent platform proof.
    allowed = {image_id}
    if platform_image_id is not None:
        require(re.fullmatch(r"sha256:[a-f0-9]{64}", platform_image_id), "invalid proven platform image identity")
        allowed.add(platform_image_id)
    require(report.get("Metadata", {}).get("ImageID") in allowed, "scan does not match patched image")
    config = report.get("Metadata", {}).get("ImageConfig", {})
    require(config.get("architecture") == "amd64" and config.get("os") == "linux", "scan platform mismatch")
    results = report.get("Results", [])
    require(isinstance(results, list) and any(item.get("Class") == "os-pkgs" for item in results)
            and any(item.get("Target") == "usr/bin/caddy" and item.get("Type") == "gobinary" for item in results),
            "scan must cover runtime OS and installed Caddy binary")
    findings = [finding for item in results for finding in item.get("Vulnerabilities", [])
                if finding.get("Severity") in ("HIGH", "CRITICAL")]
    require(not findings, "HIGH/CRITICAL findings remain; review is required before deployment")


def runtime_checks(image_id):
    # Validate the committed public routing configuration with synthetic env values.
    config = (Path(__file__).resolve().parent.parent / "edge/Caddyfile").read_text()
    invoke = isolated(image_id, "sh")
    invoke[2:2] = ["-i", "-e", "EDGE_DOMAIN=map-security.invalid", "-e", "EDGE_EMAIL=security@example.invalid"]
    run(invoke + ["-c", "cat > /tmp/Caddyfile; caddy validate --config /tmp/Caddyfile --adapter caddyfile"], data=config)
    # Real HTTP and TLS boot on loopback inside a network-none disposable container.
    # The internal test CA and certificates live only on tmpfs, without ACME calls.
    boot = r'''set -eu
cat > /tmp/Caddyfile <<'CONFIG'
{
    admin off
    skip_install_trust
}
http://localhost:8080 {
    respond /healthz "ok"
}
https://localhost:8443 {
    tls internal
    respond /healthz "ok"
}
CONFIG
caddy run --config /tmp/Caddyfile --adapter caddyfile > /tmp/caddy.log 2>&1 &
caddy_pid=$!
trap 'kill "$caddy_pid" 2>/dev/null || true; wait "$caddy_pid" 2>/dev/null || true' EXIT
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if [ "$(curl -fsS --max-time 2 http://localhost:8080/healthz 2>/dev/null || true)" = ok ] &&
       [ "$(curl -fkSs --max-time 2 https://localhost:8443/healthz 2>/dev/null || true)" = ok ]; then
        ready=1
        break
    fi
    sleep 1
done
test "$ready" = 1
'''
    run(isolated(image_id, "sh") + ["-c", boot], timeout=60)


def verify(args):
    original, patched = metadata(args.original_image), metadata(args.image)
    require(original["id"] != patched["id"], "upstream binary was not replaced")
    for image in (original["id"], patched["id"]):
        require(run(isolated(image) + ["version"]).split()[0] == "v2.11.4", "Caddy source version changed")
    old_modules = module_names(run(isolated(original["id"]) + ["list-modules"]))
    new_modules = module_names(run(isolated(patched["id"]) + ["list-modules"]))
    require(old_modules == new_modules, "standard module set changed")
    buildinfo = run(isolated(patched["id"], "cat") + ["/usr/share/map-caddy-build/buildinfo.txt"])
    verify_buildinfo(buildinfo)
    # A copied evidence file alone is insufficient: match the installed binary hash.
    expected_hash = run(isolated(patched["id"], "cat") + ["/usr/share/map-caddy-build/binary.sha256"]).split()[0]
    actual_hash = run(isolated(patched["id"], "sha256sum") + ["/usr/bin/caddy"]).split()[0]
    require(re.fullmatch(r"[a-f0-9]{64}", expected_hash) and expected_hash == actual_hash, "installed binary differs from build evidence")
    verify_report(json.loads(args.report.read_text()), patched["id"], patched["platform_image_id"])
    runtime_checks(patched["id"])
    print(json.dumps({"status": "verified", "image_id": patched["id"], "platform_image_id": patched["platform_image_id"], "caddy": "v2.11.4", "go": GO_VERSION,
                      "module_count": len(new_modules), "high_critical": 0,
                      "isolated_http": "pass", "isolated_tls": "pass", "public_config": "valid"}))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.buildinfo = "/out/caddy: go1.26.8\n" + "".join(f"\tdep\t{name}\t{version}\th1:synthetic\n" for name, version in EXPECTED.items()) + "\tbuild\tGOOS=linux\n\tbuild\tGOARCH=amd64\n\tbuild\tCGO_ENABLED=0\n"
        self.image_id = "sha256:" + "a" * 64
        self.report = {"ArtifactType": "container_image", "Metadata": {"ImageID": self.image_id, "ImageConfig": {"architecture": "amd64", "os": "linux"}},
                       "Results": [{"Class": "os-pkgs"}, {"Target": "usr/bin/caddy", "Type": "gobinary"}]}

    def test_expected_evidence_passes(self):
        verify_buildinfo(self.buildinfo)
        verify_report(self.report, self.image_id)

    def test_old_tls_toolchain_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "toolchain"):
            verify_buildinfo(self.buildinfo.replace("go1.26.8", "go1.26.3"))

    def test_source_or_security_dependency_change_is_rejected(self):
        for version in EXPECTED.values():
            with self.subTest(version=version), self.assertRaises(ValueError):
                verify_buildinfo(self.buildinfo.replace(version, "v0.0.0"))

    def test_local_module_replacement_is_rejected(self):
        with self.assertRaises(ValueError):
            verify_buildinfo(self.buildinfo + "\t=>\t/tmp/source\n")

    def test_different_image_or_architecture_scan_is_rejected(self):
        with self.assertRaises(ValueError):
            verify_report(self.report, "sha256:" + "b" * 64)
        self.report["Metadata"]["ImageConfig"]["architecture"] = "arm64"
        with self.assertRaises(ValueError):
            verify_report(self.report, self.image_id)

    def test_same_image_proven_index_and_child_are_accepted_but_not_unrelated_id(self):
        child = "sha256:" + "b" * 64
        verify_report(self.report, self.image_id, child)
        self.report["Metadata"]["ImageID"] = child
        verify_report(self.report, self.image_id, child)
        self.report["Metadata"]["ImageID"] = "sha256:" + "c" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            verify_report(self.report, self.image_id, child)

    def test_metadata_proves_child_against_immutable_parent_and_rejects_race(self):
        child = {"id": "sha256:" + "b" * 64, "os": "linux", "architecture": "amd64"}
        calls = []
        replies = iter((json.dumps(child), self.image_id, json.dumps(child)))
        def inspect(args):
            calls.append(args)
            return next(replies)
        with patch.dict(globals(), {"run": inspect}):
            result = metadata("synthetic:tag")
        self.assertEqual(result["id"], self.image_id)
        self.assertEqual(result["platform_image_id"], child["id"])
        self.assertEqual(calls[-1][-1], self.image_id)
        replies = iter((json.dumps(child), self.image_id, json.dumps({**child, "id": "sha256:" + "c" * 64})))
        with patch.dict(globals(), {"run": inspect}), self.assertRaisesRegex(ValueError, "image changed"):
            metadata("synthetic:tag")

    def test_incomplete_or_vulnerable_report_is_rejected(self):
        for results in ([], [{"Class": "os-pkgs"}], [{"Target": "usr/bin/caddy", "Type": "gobinary"}]):
            with self.subTest(results=results), self.assertRaises(ValueError):
                verify_report({**self.report, "Results": results}, self.image_id)
        for severity in ("HIGH", "CRITICAL"):
            report = copy.deepcopy(self.report)
            report["Results"][1]["Vulnerabilities"] = [{"Severity": severity}]
            with self.subTest(severity=severity), self.assertRaises(ValueError):
                verify_report(report, self.image_id)

    def test_module_output_retains_single_component_modules(self):
        modules = module_names("http\ntls\nhttp.handlers.reverse_proxy\ntls.issuance.acme\nStandard modules: 4\n")
        self.assertEqual(len(modules), 4)
        with self.assertRaises(ValueError):
            module_names("http\ntls\n")

    def test_disposable_run_selects_amd64_without_network_or_pull(self):
        args = isolated(self.image_id)
        for flag, value in (("--platform", "linux/amd64"), ("--network", "none"), ("--pull", "never")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertNotIn("--volume", args)
        self.assertNotIn("-v", args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--original-image", default=UPSTREAM)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(EvidenceTests))
        return 0 if result.wasSuccessful() else 1
    if not args.image or not args.report:
        parser.error("--image and --report are required")
    try:
        verify(args)
    except Exception as error:
        # Tool output and paths are not copied into logs; failed verification never deploys.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
