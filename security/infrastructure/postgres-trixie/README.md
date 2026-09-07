# Separate PostgreSQL 17.11 / PostGIS 3.5.7 Trixie alternative

This is an **untested alternative**, not the successor to the tested Bookworm
candidate and not an approved image. It preserves the Bookworm recipe and its
actual evidence. No local image build, APT install, database operation, CI
dispatch or live deployment was performed for this alternative.

## Current evidence and reason to compare

Remote run `34096994701`, source
`11e4c140c291536dd24cd6860e753c3165120422`, built the Bookworm candidate and passed
its synthetic PostgreSQL restore, locale/index and ACL fixture. Its strict scan
still reported **108 HIGH / 16 CRITICAL**. All 124 findings have null FixedVersion
in that exact report; their statuses are `affected`, `fix_deferred` or
`will_not_fix`. `bookworm-grouped-baseline.json` records the package grouping,
not a new scan. The original report, candidate identity and fixture remain under
run `34096994701`; no success is attributed to Trixie.

Two approaches were considered. Keeping Bookworm preserves the tested build and
fixture while individual missing distribution fixes require separate work.
Comparing Trixie retains the PostgreSQL major and glibc locale family while
updating OS libraries, but also changes glibc/collation and SFCGAL's major
version. The latter needs its own full build, restore and scan evidence.

Trixie alone cannot satisfy the strict zero-finding gate. The following Debian
tracker status was read on 2026-09-07; it describes source packages, not verified
exploitability of the MAP container.

| Bookworm CRITICAL finding | Trixie status in official tracker | Consequence |
| --- | --- | --- |
| [aom CVE-2023-6879](https://security-tracker.debian.org/tracker/CVE-2023-6879) | Fixed | A newer distribution may remove this report entry; exact scan required. |
| [sqlite3 CVE-2025-7458](https://security-tracker.debian.org/tracker/CVE-2025-7458) | Fixed | Same limitation. |
| [zlib CVE-2023-45853](https://security-tracker.debian.org/tracker/CVE-2023-45853) | Fixed | Debian also notes the affected MiniZip code is not built by Bookworm's zlib source; retain the original scan finding and distinguish that scope note. |
| [perl CVE-2026-13221](https://security-tracker.debian.org/tracker/CVE-2026-13221) | Vulnerable | Trixie 5.40.1-6 still needs an upstream/distribution fix. |
| [perl CVE-2026-42496](https://security-tracker.debian.org/tracker/CVE-2026-42496) | Vulnerable | Archive::Tar fix remains deferred; do not remove this finding. |
| [perl CVE-2026-8376](https://security-tracker.debian.org/tracker/CVE-2026-8376) | Vulnerable | Advisory describes a 32-bit Perl trigger; this amd64 candidate has no new reachability assessment or exclusion. |
| [libxml2 CVE-2026-6653](https://security-tracker.debian.org/tracker/CVE-2026-6653) | Vulnerable | Trixie's `really2.9.14` packaging retains the old XML ABI; a 2.12-looking version is not proof of a fix. |

The three Perl IDs accounted for 12 Bookworm binary-package rows. This is not a
prediction of the alternative's total: new packages and scanner DB revisions can
change the result. No severity override, ignore-unfixed option or scanner
exception is introduced.

## Exact base and retained contracts

The observed official `postgres:17.11-trixie` tag resolved to:

| Object | Digest |
| --- | --- |
| Multi-platform index | `sha256:67f41722b7a8cbdb868a44a4995c846eddfdc2973bccb291ce937dce88ad5675` |
| Selected Linux amd64 manifest | `sha256:d13db94ae661d517c5ed57c509a578d5ea64aae639871ba25294f4f42d83de28` |
| Image config | `sha256:7296f210ae81031ec955dbad9a67a84fe958572a2153b8d0826a647522904dc1` |

`index.json`, `manifest.json` and `config.json` preserve the raw bytes. Their
hashes and descriptor chain are checked by the offline tests. The Dockerfile pins
the amd64 manifest and refuses a different BASE. The official source annotation
identifies [Dockerfile revision 2603e26](https://github.com/docker-library/postgres/blob/2603e26e245e558218728ee14e0a42dcb020dc7f/17/trixie/Dockerfile).

Config metadata retains `PG_MAJOR=17`, `PG_VERSION=17.11-1.pgdg13+2`,
`LANG=en_US.utf8`, `PGDATA=/var/lib/postgresql/data`, the original entrypoint/CMD,
port 5432, SIGINT and volume path. Bounded, hash-verified reads of 1,278,195 bytes
of small registry layers observed the postgres UID/GID 999 row, a PGDATA directory
owned by 999:999, and the same gosu/entrypoint bytes as the Bookworm base.
`registry-proof.json` records those limited observations. The complete rootfs was
not reconstructed, and no downloaded binary was executed. Build/fixture guards
must still verify the resulting runtime UID and ownership.

The unchanged PostGIS 3.5.7 source URL and checksum, raster, topology, SFCGAL,
protobuf and address-standardizer build flags, PG crypto module checks and
3.5.2-to-3.5.7 extension paths are preserved. PG packages remain held at their
exact 17.11 Debian build. The root's reviewed Go 1.26.8 gosu stage is copied byte
for byte from source `11e4c140c291536dd24cd6860e753c3165120422`, with its original
upstream-signature provenance kept separate from the newly built binary. The
Trixie base gosu itself was observed to have the same original SHA256
`52c8749d0142edd234e9d6bd5237dff2d81e71f43537e2f4f66f75dd4b243dd0`.

Only normal Trixie/PGDG signed repositories are used; no sid/forky package or
libxml ABI substitution is added. The selected runtime inventory is
`libgdal39 libgeos-c1t64 libjson-c5 libpcre2-8-0 libproj25 libprotobuf-c1
libsfcgal2 libxml2`. Every actual tuple must be installed, native/all architecture,
nonduplicate and one of the eight explicit names. Final APT selects the builder's
exact versions with no removal; strict tuple comparison and all six module ldd
checks remain mandatory.

Trixie's `libsfcgal-dev` selects SFCGAL 2.0 / `libsfcgal2`, unlike Bookworm's 1.4.1
/ `libsfcgal1`. This is a library major change, not a PostGIS major change. Source
feasibility and the unchanged build flags do not establish behavior or data
compatibility. No feature is disabled to make the build pass.

`package-dependencies.json` contains the scoped PGDG, Debian main, security and
updates metadata. Each compressed index matched the official Release file's
SHA256/size; Release signatures were not checked during this read-only review.
Actual builds must retain APT signature verification. The exact
`postgresql-server-dev-17=17.11-1.pgdg13+2` is present in PGDG main, alongside
the matching server/client packages. This proves published availability, not a
completed package solver or install.

The [PostGIS 3.5.7 source](https://github.com/postgis/postgis/tree/9816f82458db774e62906cfb2c4f01f8b262c862)
sets SFCGAL's minimum to 1.3.1 without a 2.x upper bound; its SFCGAL integration
also contains version-gated 2.1 support. This supports attempting the build with
Trixie's 2.0.0 library. It does not establish compile, geometry behavior or stored
data compatibility. The tag and immutable source bytes were independently
cross-checked without compiling them.

## Root integration interface and remaining gates

`mode-wiring-request.json` is a proposed interface only. The root owns the
manifest, scanner, workflow and shared fixture. Keep the tested Bookworm row as
the current candidate until explicitly choosing a separate comparison run. To
run this alternative, root would need the dedicated `postgres-trixie` mode,
exact base allowlist, 2,400-second build allowance and existing guarded evidence
export path. The build context also needs the already reviewed `gosu-security/`
recipe from the integrated source. This commit does not copy or edit that recipe.

Before adoption, run the complete existing strict scanner against the exact
alternative digest with tool/DB metadata, then the entire synthetic old PG17.5 /
PostGIS3.5.2 to new17.11/3.5.7 logical restore, new writes, restart, old backup
restore and post-new logical rollback fixture. Inspect glibc and collation
versions and Korean/Latin/accent/case/numeric index-vs-sequential order changes
separately. Extend SFCGAL behavior coverage where needed. Preserve failures and
backup evidence. A prior Bookworm PASS does not satisfy any of these Trixie gates.

No physical existing volume reuse, live migration, production user data, role
changes or GCP/NCP deployment is authorized by this recipe. Strict security zero,
actual data compatibility and user review remain separate requirements.
