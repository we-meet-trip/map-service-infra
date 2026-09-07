# Pinned Go security rebuild candidates

Prepared 2026-09-07 from Infra `bb9c63398295c3b8fd3e33f97235d3588c5c7fce`. These files prepare remote builds; they do not deploy, upgrade a live service, approve a candidate, or modify the common scanner/manifest/fixtures/workflow. Local Go and Docker executions: zero.

## Exact reason for the rebuild

Run `34089509166` successfully executed the synthetic compatibility fixtures, but its exact candidate scans still reported Prometheus 2 HIGH/2 CRITICAL, node-exporter 8 HIGH/1 CRITICAL, and postgres-exporter 11 HIGH/1 CRITICAL. Both Prometheus executables contribute findings, so replacing only `/bin/prometheus` would leave vulnerable `/bin/promtool`.

| Input | Official fix boundary | Selected build input |
|---|---|---|
| x/crypto, CVE-2026-56854 | [GO-2026-6303](https://pkg.go.dev/vuln/GO-2026-6303): v0.55.0 | v0.55.0 for all three |
| grpc, CVE-2026-84304 | [Maintainer advisory](https://github.com/grpc/grpc-go/security/advisories/GHSA-vp52-pcj8-j9qc): v1.83.1 | v1.83.1 for Prometheus and promtool |
| x/net, CVE-2026-46600 | [GO-2026-5942](https://pkg.go.dev/vuln/GO-2026-5942): v0.56.0; stdlib Go 1.26.6 | v0.57.0, required by crypto v0.55.0 |
| x/text, CVE-2026-56852 | [GO-2026-5970](https://pkg.go.dev/vuln/GO-2026-5970): v0.39.0 | v0.41.0, required by crypto v0.55.0 |
| Go stdlib findings in exporters | [Go security release](https://groups.google.com/g/golang-announce/c/94pEornpRlI): Go 1.26.6 | Go 1.26.8, current patch in the existing 1.26 branch |

Crypto's advisory concerns SSH authentication callback source-address checks, while grpc's advisory concerns memory exhaustion from fragmented HTTP/2 data. A module-level finding does not establish that these services expose the particular vulnerable call path. No ignore or reachability exemption is introduced: every affected binary is rebuilt and the strict scan must decide the resulting findings.

Two choices were compared: replacing whole service versions again, or preserving each tested upstream release while rebuilding its affected Go modules. The latter keeps the known runtime image and feature contract. [Go's release history](https://go.dev/doc/devel/release) confirms 1.26.8 was released on 2026-09-01; 1.27.1 also exists, but a new Go major is unnecessary for the listed fixes. The pinned 1.26.8 builder registry config was independently retrieved and SHA-verified, including `GOLANG_VERSION=1.26.8` and `GOTOOLCHAIN=local`.

`pins.json` records the verified Linux amd64 builder manifest/config/index digests, immutable upstream tag commits, hashes of upstream go.mod/go.sum/.promu.yml/Dockerfile, module origin commits and both Go checksum-database hashes. Each original source tag was checked with `git ls-remote`, including the dereferenced commit for annotated tags. Versions and checksums are exact; no `@latest`, `go get -u`, forced `replace`, skipped checksum DB or automatic toolchain download is used.

The crypto v0.55.0 [official go.mod](https://proxy.golang.org/golang.org/x/crypto/@v/v0.55.0.mod) requires net v0.57.0, text v0.41.0, sys v0.47.0 and term v0.45.0. These dependencies are explicitly pinned. Trying to force net v0.56.0/text v0.39.0 would conflict with Go's minimum-version selection. All before/after module changes, including other required transitive changes, are retained for root review.

## Source and runtime preservation

| Service | Upstream source commit | Runtime base SHA-256 (Linux amd64) |
|---|---|---|
| Prometheus v3.14.0 | `d7598b7141418fa35be2b5ec5d0fefb634199610` | `e906cef998316bbe319f98711e1b4d8613ad37e14b08ff831d7036e77b7464f9` |
| node-exporter v1.12.1 | `6044da783597cc3b57aef7580ddcdcff58a4ee99` | `da83fae85603c4e47e6c68369a7d746e2dda683dc35ea2e234b4f171e0d92798` |
| postgres-exporter v0.20.1 | `867fbcac31cd18c143e244190ea9168cca069827` | `4f3d82803c1f99ea5e767890de3557d2479ebbc711f63f2e04c663daa840057a` |

The final Docker stage starts from that exact upstream base and copies only rebuilt binaries plus audit evidence. It inherits `User=nobody`, entrypoint, command, environment, working directory, exposed ports and volumes. It retains the original CA certificates, configuration, licenses and other runtime files. Source and module changes are build-only. Root must compare the resulting OCI runtime fields against the prior candidate and run its isolated fixtures again.

Prometheus and promtool preserve the upstream `netgo,builtinassets` build tags from [.promu.yml](https://raw.githubusercontent.com/prometheus/prometheus/d7598b7141418fa35be2b5ec5d0fefb634199610/.promu.yml). Its UI is preserved using the official same-release [web UI artifact](https://github.com/prometheus/prometheus/releases/download/v3.14.0/prometheus-web-ui-3.14.0.tar.gz), 3,272,035 bytes, SHA-256 `be18623c5891d32572998070de0d48522c966b737d9204aa41e0e88d6318e029`. The downloaded bytes matched GitHub's asset digest; its 18 members contain both Mantine and React UIs with no links. The upstream compression script generates the embed declarations; this avoids a Node dependency rebuild or a server without its UI. Generated archive members are bounded, validated and extracted only inside the fresh image builder.

All binaries use static CGO-disabled builds and report an explicit `+map.security.1` version suffix, upstream revision and MAP source revision. Actual ELF headers must also show x86-64 and no dynamic loader/segment. `/usr/share/map-security/go/` contains all command/test/checksum receipts, complete module patch, before/after effective graph, source file hashes, `go version -m`, binary hashes and recipe metadata. Raw original/patched go.mod/go.sum files remain in the optional evidence stage; their exact bytes can also be reconstructed from the upstream commit and complete patch. They are build inputs and audit material, not additional runtime sources. No runtime binary or linked module is removed from strict scans.

## Root integration interface

1. Add `go-security` as a candidate build mode in the root-owned scanner/manifest. Use each exact `runtime_base` from pins.json as its candidate selector. The helper rejects any other base, even if its tag looks newer.
2. Select `security/infrastructure/Dockerfile.go-security`, context `security/infrastructure`, and pass `BASE`, `SERVICE` (one of the three table names), and the final 40-character Infra `SOURCE_SHA`. Existing `RUNTIME_USER` is unnecessary because the final stage inherits the original image metadata. Pin the source commit before dispatching CI.
3. Run serially in GitHub Linux amd64 CI. Increase the build command timeout for this mode to 2,700 seconds; each individual build/test has a bounded timeout. Go compilation uses two workers and GOMAXPROCS=2/GOMEMLIMIT=3GiB. No host filesystem, credentials, Docker socket or serving DB is mounted into the builder. The builder includes Python via the official Debian buildpack's mercurial dependency. The exact OCI layer containing Python is independently streamed, checksum-verified and recorded in `builder-python-evidence.json`; no extra package install is needed.
4. Preserve build stdout/stderr, including failed or timed-out commands (the helper prints bounded captured diagnostics before failing). On success, extract all command/test/module receipts under `/usr/share/map-security/go/` from the loaded exact candidate using an owned temporary container. Optionally export Docker target `evidence` with `--output type=local,dest=<fresh-evidence-directory>` and identical build inputs/cache for raw original/resolved go.mod/go.sum. Read `build-result.json`, actual selected test logs, module diffs, UI archive/resource inventory, `go version -m` and source hashes. A source file named as a test does not prove it executed.
5. Run the existing exact-image strict Trivy checks with unchanged severity/unfixed/DB settings, archive identity comparison and each service's actual isolated runtime/restore fixtures. Fixtures from run 34089509166 cover the prior upstream candidates, not these rebuilt binaries. Keep findings and failures visible.

Local default planning is safe:

```sh
python3 security/infrastructure/go-security/build.py \
  --service prometheus \
  --base prom/prometheus@sha256:e906cef998316bbe319f98711e1b4d8613ad37e14b08ff831d7036e77b7464f9 \
  --source-sha <exact-infra-commit>
```

`--execute-builder` is guarded before filesystem, Git, Go or network operations and requires the dedicated Linux amd64 builder environment. Do not run it locally.

## Implemented validation and current limits

The remote recipe executes selected upstream unit packages: Prometheus config/labels/PromQL parser, node-exporter collectors, postgres-exporter collectors and command package. It then builds all runtime binaries, verifies binary Go/module information and runs loopback metrics smokes. Prometheus additionally checks synthetic configuration and rules with the rebuilt promtool and starts sequential default/old-ui servers to verify HTML and embedded JavaScript responses. No external DB is used; the postgres exporter smoke uses an intentionally unreachable loopback fixture DSN.

Local execution completed 14 stdlib tests (including 7 new recipe safety tests), Python AST, default plan and `git diff --check`; no Go tests, binaries, Docker images or native service starts were executed locally. Remote recipe PASS, new strict counts, runtime metadata preservation, restored data compatibility and resulting artifact digests remain pending until root executes the final candidate. GCP, production data, OSRM, Caddy and all deployment receivers are untouched.
