# Grafana core security candidate — 2026-09-07

The exact Grafana 13.2.1 candidate from CI `34089509166` retains HIGH 157 / CRITICAL 3 after the Alpine package update. The core executable contributes two HIGH findings: Apache Thrift `v0.23.1-0.20260429145742-d2acd3c49e58` and gRPC `v1.82.1`. Twelve external catalog backend executables contribute the remaining HIGH 155 / CRITICAL 3. Elasticsearch is also preserved; its target had no findings. These counts are actual results of that run, not predictions of a later scan.

This recipe rebuilds only the core executable from official tag `v13.2.1`, commit `56cd3e9288d8255fecebe5d05b48d191f50674b5`, with Thrift `v0.24.0`, gRPC `v1.83.1`, and Go `1.26.8`. The module archives and go.mod files are verified against pinned public Go checksum-database values. The source's `go.mod` checksum and original two requirements must match before patching. Effective workspace dependency versions and final binary build information must contain exactly the selected fixed versions without replacements. Existing workspace modules and all other features remain present.

The official Linux amd64 Go builder is pinned to manifest `sha256:bc6beb46032d45f421cf400036bf031cdc64f683ba9cdc124e31d063e71670bd` (config `sha256:62173940eb7f48d9f3bbe6553aaeb7cc93857abb7e461ae9547fd5fc20b0bbd9`). The runtime is the exact official Grafana 13.2.1 Linux amd64 manifest `sha256:1dec240d14e232597dce9bfa56dae55f4397b138cdc91e3ee92ac6b157e2fc49`, then its Alpine packages are updated using the same approach as the preceding actual candidate. No image is published or deployed by these files.

## Root scanner interface

- Add `grafana-security` as an explicitly permitted build recipe; keep the existing `grafana/grafana:13.2.1` selector and validate its resolved base against the recipe's exact manifest. Pass the existing `BASE`, `SOURCE_SHA`, and `RUNTIME_USER` build arguments. The recipe requires the upstream runtime UID `472`.
- The build context remains `security/infrastructure`; use `Dockerfile.grafana-security`. Allow up to 3600 seconds for the core build plus source/module download. Go compilation is capped at two processes and two scheduler threads. No local build is authorized or needed.
- Continue the existing verified Docker-archive scan/OCI identity/source SHA chain. The core build does not lower the HIGH/CRITICAL policy or suppress any vulnerability. Residual plugin findings should continue to fail that gate.
- Extract `/usr/share/map-security/grafana-core/` from an owned temporary candidate container. It contains source patch, authenticated module receipts, complete effective-module inventory, `go version -m`, source file hashes, binary hash, upstream/source-recipe SHAs, and build logs. Raw source manifests are not installed as runtime dependencies; the source commit plus recorded patch reconstructs them exactly.
- The frontend and all thirteen catalog plugin trees are hashed before and after the core replacement/OS update. Every expected plugin must retain both `plugin.json` and `MANIFEST.txt`; any content addition, deletion, or change fails the comparison. No manifest, plugin, or signature check is removed.
- Run the existing synthetic SQLite dashboard/admin login, restart, pre-upgrade backup restore, and direct downgrade fixtures. Add a plugin inventory check: all thirteen expected IDs remain visible with valid signatures under `/api/plugins`; creating/loading the Prometheus data source should still exercise its unchanged backend. Build presence does not prove this runtime behavior.
- Core `make build-go` uses the upstream `oss` tags, Linux amd64, `CGO_ENABLED=0`, source timestamp, upstream commit, explicit version `13.2.1`, and distinct branch `map-security-core`. The upstream Dockerfile also supports a static `CGO_ENABLED=0` distroless executable, but actual SQLite/serving compatibility remains a remote fixture gate.

## Why the catalog plugins remain a release blocker

Read-only checks on 2026-09-07 found Grafana's newest official published release to be 13.2.1 (published September 2). The latest supported Linux amd64 packages for all thirteen catalog IDs were published between May 13 and August 27. Their exact source commits, package checksums, and module requirements were checked separately; several still require the vulnerable versions found in the actual binary scan. No later signed package was verified as fixed.

In that exact Grafana source:

1. `pkg/setting/setting.go:1587` adds both installed plugins and `data/plugins-bundled` to `cfg.PluginsPaths`.
2. `pkg/services/pluginsintegration/pluginsources/pluginsources.go:42` classifies those paths as `ClassExternal`. Its `corePluginPaths` includes only `public/app/plugins/datasource` and `public/app/plugins/panel`.
3. `pkg/plugins/manager/signature/manifest.go:143` verifies the signed manifest and hashes every recorded file. Replacing a backend binary produces `SignatureStatusModified`.
4. `pkg/plugins/manager/signature/signature.go:73` rejects modified signatures. An unsigned-plugin allowlist does not authorize modified signed plugins.

Thus a custom rebuild of the catalog backend executables would fail normal plugin loading without an appropriate valid signature. Moving them into core paths, deleting manifests, enabling unsigned plugins, disabling verification, or omitting their binaries would weaken the required contract and is excluded. Valid vendor-signed patched packages, followed by exact scans and loading/feature fixtures, remain required to clear their residual findings. No vendor message or signing request has been sent.

## Official sources

- [Grafana 13.2.1 source](https://github.com/grafana/grafana/tree/56cd3e9288d8255fecebe5d05b48d191f50674b5), [releases](https://github.com/grafana/grafana/releases).
- [Catalog bundling defaults](https://github.com/grafana/grafana/blob/56cd3e9288d8255fecebe5d05b48d191f50674b5/scripts/catalog-plugins-defaults), [catalog download contract](https://github.com/grafana/grafana/blob/56cd3e9288d8255fecebe5d05b48d191f50674b5/scripts/download-catalog-plugins.sh).
- [Plugin source classification](https://github.com/grafana/grafana/blob/56cd3e9288d8255fecebe5d05b48d191f50674b5/pkg/services/pluginsintegration/pluginsources/pluginsources.go), [manifest verification](https://github.com/grafana/grafana/blob/56cd3e9288d8255fecebe5d05b48d191f50674b5/pkg/plugins/manager/signature/manifest.go), [signature enforcement](https://github.com/grafana/grafana/blob/56cd3e9288d8255fecebe5d05b48d191f50674b5/pkg/plugins/manager/signature/signature.go).
- [gRPC vendor advisory and fixed version](https://github.com/grpc/grpc-go/security/advisories/GHSA-vp52-pcj8-j9qc), [Apache Thrift 0.24.0](https://github.com/apache/thrift/releases/tag/v0.24.0), [Go release history](https://go.dev/doc/devel/release).

Local validation is limited to stdlib contract tests, syntax, immutable-source/module metadata, and source review. Remote build, exact scan, plugin loading, migration/restore, and live deployment outcomes must be recorded separately after actual execution.
