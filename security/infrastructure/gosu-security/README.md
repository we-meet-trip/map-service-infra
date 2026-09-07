# PostgreSQL gosu toolchain candidate

This recipe rebuilds the unchanged gosu 1.19 source with Go 1.26.8. It produces a
reusable `/out/gosu` and complete, small `/out/evidence` directory. It does not
build PostgreSQL, change a database, create a runtime container, deploy or invoke
CI. The parent PG candidate must run its complete strict scan and data fixture
after integration. A successful build is not security approval.

The selected official PostgreSQL 17.11 bookworm linux/amd64 manifest is
`sha256:7bade6d532592ca8ce7ee32def7399dad2607c4ea5583839fc4352a095a11ea6`.
Its actual `/usr/local/bin/gosu` is 1.19, Go 1.24.6, static Linux amd64 v1,
root:root 0755, SHA256
`52c8749d0142edd234e9d6bd5237dff2d81e71f43537e2f4f66f75dd4b243dd0`.
The full 1,250,024-byte OCI layer was hash verified and its ELF build information
parsed without executing it. The same binary downloaded from the official 1.19
release matched byte-for-byte and passed an independently executed gpgv check.
The root evidence file `gosu-base-immutable-verification-20260906.json` records
this metadata separately from any future candidate scan.

The PG recipe's normal Debian apt updates do not replace this directly installed
binary. The base's exact entrypoint calls gosu only when starting as UID 0. The
proposed PG image defaults to UID 999, but removing gosu would break the inherited
root startup contract. This recipe preserves the entrypoint and privilege helper.
It does not claim that a version-based stdlib finding is a reachable remote
PostgreSQL vulnerability. For example, Go's
[CVE-2025-68121 advisory](https://pkg.go.dev/vuln/GO-2026-4337) includes Go 1.24.6
in the affected version range, while the described TLS behavior is not directly
invoked by gosu's reviewed source. No scan finding is excluded or downgraded.

## Source and signature boundaries

- Source tag [1.19](https://github.com/tianon/gosu/releases/tag/1.19) resolves to
  `6456aaa0f3c854d199d0f037f068eb97515b7513`. The recipe verifies that immutable Git
  commit and six original source/module/build-recipe file hashes. No source or
  module patch is made. `source.patch` must be empty and Git status clean.
- The original `github.com/moby/sys/user v0.1.0` and `golang.org/x/sys v0.1.0`
  remain pinned. Public sum.golang.org checks, exact GoModSum/Sum receipts and
  `-mod=readonly` guard the complete two-module graph. No `latest`, `go get -u`,
  private-module bypass or invented module version is used.
- The [upstream build recipe](https://github.com/tianon/gosu/blob/6456aaa0f3c854d199d0f037f068eb97515b7513/Dockerfile)
  deliberately retains symbols: CGO=0, trimpath, linker flags `-d -w`,
  `-buildvcs=true`, and no `-s`. These are preserved. Real Git revision metadata
  is used; the upstream fake-git stamp `vcs.revision=1.19` is not reproduced.
  The main module may consequently be shown as `(devel)`; the exact real source
  revision and clean status are mandatory. The runtime version remains `1.19`
  and reports the new actual Go version.
- The exact Go 1.26.8 linux/amd64 builder is
  `golang@sha256:bc6beb46032d45f421cf400036bf031cdc64f683ba9cdc124e31d063e71670bd`.
  [Go's release history](https://go.dev/doc/devel/release) records this supported
  branch update on 2026-09-01. GOTOOLCHAIN=local prevents an implicit toolchain
  download. The existing Go recipe's registry-layer proof verifies Python 3 in
  this builder. Signed Debian apt installs gpgv in the builder only and the
  complete resulting package inventory is recorded.
- Original release bytes, detached signature and public key are individually
  hash pinned. gpgv must return VALIDSIG for fingerprint
  `B42F6819007F00F88E364FD4036A9C25BF357DD4`. That signature authenticates the
  original upstream binary only. It does **not** sign or authenticate the newly
  compiled binary. The rebuilt file has its own SHA256, source/build receipts
  and candidate source SHA. A public-key rotation or changed release asset fails
  for review. No existing signature is copied beside the new executable.

## Remote-only execution and parent integration

Locally, `python3 gosu-security/build.py --source-sha <40-char-candidate-SHA>`
prints a plan only. `--execute-builder` refuses before writes or subprocesses
unless explicitly enabled in a Linux amd64 root builder with the exact required
environment. Do not launch Docker or Go locally for this work.

The standalone Dockerfile uses `security/infrastructure` as build context and
requires build arg SOURCE_SHA. Its `gosu_builder` stage has no PG input; the
default scratch `artifact` stage contains `/out` and is for extraction only.
No image publishing, receiver or shared CI workflow is provided here.

For the parent PG Dockerfile, copy the **entire `gosu_builder` stage** from
`Dockerfile.gosu-security` ahead of the PG stages. The same build context must
contain `gosu-security/`. In the final PG candidate stage, after the last stage
that could overwrite `/usr/local/bin`, add:

```dockerfile
COPY --from=gosu_builder --chown=0:0 --chmod=0755 /out/gosu /usr/local/bin/gosu
COPY --from=gosu_builder --chown=0:0 /out/evidence/ /usr/share/map-security/gosu/
```

The parent keeps its existing immutable PG base, PostGIS source, runtime USER,
environment, entrypoint, command, ports, stop signal and volume declarations.
The helper adds no runtime libraries, Go compiler, source tree, old vulnerable
binary or private input. Evidence is regular files only, at most 16 MiB per file
and 64 MiB total. Complete source hashes, an empty patch, effective module graph,
module download/checksum receipts, Go environment, builder package inventory,
binary symbols/build info, original signature verification and all executed
commands/tests remain available under that directory. Root's extractor should
copy it from an owned unstarted candidate container and retain it with the scan.
The integrated PostgreSQL parent stores the same receipts under
`/usr/share/map-candidate/gosu/` to include them in its existing export tree.

## Executed remote checks and honest limits

The recipe actually runs the upstream moby user-parser tests (their test imports
are stdlib only) and then verifies the compiled helper. It does not count
`go test` reporting no tests for the gosu main package as a behavioral suite.

Synthetic runtime checks use reserved users/groups created only inside the
disposable root builder: named account supplementary groups, explicit group,
unmapped numeric UID/GID, HOME resetting, malformed/unknown users and groups,
missing commands, exact child exit code, and exec preserving the child PID.
Private copies test refusal of setuid/setgid installs and never enter `/out`.
ELF identity requires static amd64 without PT_INTERP/PT_DYNAMIC; `go version -m`
must contain Go 1.26.8, exact dependency checksums, CGO=0 and the real source
revision. `go tool nm` must find the retained main and privilege-switch symbols.

The original helper's user parser and syscall Exec behavior are preserved by
source identity, with runtime identity/exec checks as above. TTY interaction,
real host privilege configuration, live PG startup, data restore and security
scan are separate parent gates. No govulncheck exclusion wrapper is used.
Future symbol reachability analysis may supplement the full strict Trivy
report; it must not replace that report or remove findings.

Before any parent remote build, only stdlib recipe tests and source validation
have run. Remote compile, runtime, exact scan and PG compatibility remain
unverified until their actual artifacts are reviewed.
