# PostgreSQL 17 / PostGIS 3.5 Debian candidate build contract

Prepared 2026-09-07; delegated from Session D/B root. Candidate source only: no local Docker, build, CI, database migration, data-volume ownership change, or live deployment was performed. Existing Alpine candidate is not approved for the live `en_US.utf8` database. This recipe is not a claim of zero vulnerabilities or restore compatibility.

## Decision and evidence

Actual GCP metadata at `2026-09-07T05:44:03.173615+00:00` is PostgreSQL `170005` / `17.5 (Debian 17.5-1.pgdg110+1)`, installed and loaded PostGIS `3.5.2`, UTF8, `en_US.utf8`, installed extensions `plpgsql` and `postgis`. Root evidence: `evidence/parallel/infra-security/postgres-runtime-metadata-20260906.json` in the MAP workspace. The query was READ ONLY with no user table reads or secret output. Synthetic fixtures also cover topology/pgcrypto; this source recipe preserves the relevant extension binaries rather than changing those tests.

| Approach | Official source and read-only observation on 2026-09-07 | Decision |
|---|---|---|
| Current `postgis/postgis:17-3.5` + normal apt upgrade | [Official recipe](https://raw.githubusercontent.com/postgis/docker-postgis/master/17-3.5/Dockerfile) selects `postgres:17-bullseye` and PostGIS `3.5.2+dfsg-1.pgdg110+1`. Registry config confirms PG17.5, PostGIS3.5.2, LANG=en_US.utf8. Normal [bullseye PGDG Packages](https://apt.postgresql.org/pub/repos/apt/dists/bullseye-pgdg/main/binary-amd64/Packages.gz) exposes PG17.11 but only PostGIS3.5.2 for PG17 at observation. | Reject as the patch-complete recipe: ordinary upgrade cannot reach PostGIS3.5.7 from this package inventory. Debian11 EOSL was also reported by the exact scan. No archive/sid repository substitution. |
| Official PostgreSQL17.11 bookworm + source-built PostGIS3.5.7, multi-stage | [Official PostgreSQL recipe](https://raw.githubusercontent.com/docker-library/postgres/master/17/bookworm/Dockerfile) supplies PG17.11, UID/GID999 and generated glibc `en_US.utf8`. [Debian bookworm support](https://www.debian.org/releases/bookworm/) remains a supported distribution at the check date. PostGIS3.5.7 official source is available. | Select as a candidate for remote build and compatibility tests. Same libc family and locale name are preserved; collation equivalence is still unproven. |
| bookworm + unqualified `apt install postgresql-17-postgis-3` | Normal [bookworm PGDG Packages](https://apt.postgresql.org/pub/repos/apt/dists/bookworm-pgdg/main/binary-amd64/Packages.gz) currently lists PostGIS3.6.4/3.6.3/3.6.2. | Reject: this silently changes the requested 3.5 minor line. The recipe does not install this package. |

The two PGDG package lists were fetched over official HTTPS and parsed using Python stdlib; no package was installed locally. Package-list signatures were not independently verified locally. Remote apt must keep its normal signed repository verification; unauthenticated/trusted=yes/expired-release bypasses are not permitted.

PG17.11 and PostGIS3.5.7 are the selected fixed versions, not an assertion that all advisories are covered. The [PostgreSQL17 security table](https://www.postgresql.org/support/security/17/) and [PostGIS3.5 release notes](https://www.postgis.net/docs/manual-3.5/en/release_notes.html) substantiate core and extension fixes absent from the old image. Raw scan findings remain independent evidence.

## Official registry identities

Registry V2 anonymous read-only GETs fetched each tag, selected its Linux amd64 descriptor, then fetched the descriptor and config blob. SHA256 of the received bytes matched the declared manifest/config digests. No layer was pulled and no registry credential was printed.

| Image | Index | Linux amd64 manifest | Image config |
|---|---|---|---|
| `postgres:17.11-bookworm` | `sha256:051f7b7b3abdd564d5d1bd1e8c4b9c1b6e77087d1dd22020ede611c096a272e0` | `sha256:7bade6d532592ca8ce7ee32def7399dad2607c4ea5583839fc4352a095a11ea6` | `sha256:a2ea0e68c465e0acf4c3672471b22b6b62972bb341e6f31544c855d85ba43745` |
| `postgis/postgis:17-3.5` | `sha256:01a6a70e41e6c4467c8f55f6063555ed72db2d6662cd0d571040d42eadaeb6f6` | `sha256:8dfee83d8bd4c2873dc4a233c13ba2799a44f2edb16a0552d58715917fac32ba` | `sha256:2ed748fc602dd3031c6724db8cb289e1578c2deb552a4f6e291f6f7e5e6e4f69` |

Selected config: `PG_MAJOR=17`, `PG_VERSION=17.11-1.pgdg12+2`, `GOSU_VERSION=1.19`, `LANG=en_US.utf8`, architecture amd64 / OS linux. The Dockerfile accepts only the selected `postgres@<amd64 manifest>` as BASE, not a mutable tag or index. Independent config identity verification remains part of the root scanner/OCI contract.

## PostGIS source integrity and features

- Official source: [postgis-3.5.7.tar.gz](https://download.osgeo.org/postgis/source/postgis-3.5.7.tar.gz), 14,975,173 bytes.
- Independently computed SHA256: `af9ab591854d52a0d1115f90b797ef1cd60d01b85a11ff813073689e332272ff`. This exact value is mandatory in the Dockerfile and cannot be overridden by a build arg.
- [Official MD5 file](https://download.osgeo.org/postgis/source/postgis-3.5.7.tar.gz.md5): `480b8a90e32cb1ce6f2852f73aa9e835`, matching the downloaded archive. MD5 is only an additional release-identity cross-check; it is not cryptographic authenticity proof. The corresponding official `.sha256` URL returned 404. Do not describe the locally calculated SHA256 as an upstream-published checksum or claim a verified release signature.
- Tarball `Version.config` declares major3/minor5/micro7. The Dockerfile checks all three. It does not substitute a development archive, a mutable git branch, or PostGIS3.6.
- [Official install documentation](https://www.postgis.net/docs/manual-3.5/en/postgis_installation.html) and the downloaded `configure`/makefiles were read. The builder uses PG17's explicit `pg_config`, GEOS/PROJ/GDAL/XML/JSON-C/Protobuf-C/SFCGAL/PCRE2 from normal repositories, with topology, raster, protobuf and address standardizer enabled. No feature is removed merely to reduce scan counts.
- `make -j2` is the remote compile concurrency cap. `make install DESTDIR=/stage` uses upstream staging; the default install includes known-version upgrade path generation. In source3.5.7, upgrade scripts use `postgis--3.5.2--ANY.sql` and `postgis--ANY--3.5.7.sql`, not a direct `3.5.2--3.5.7.sql` filename. The build fails if these, core/topology/raster control files, or runtime module files are missing.
- `pgcrypto.so`/`pgcrypto.control` must be supplied by the selected PG17 server package; the final stage checks them. Do not remove the fixture's pgcrypto/topology assertions when diagnosing a build failure.

The official tarball was preserved at `/tmp/map-postgis-source-check-20260907/postgis-3.5.7.tar.gz`. It is source, not a fixture database or runtime artifact.

## Build/runtime contract

Build context remains `security/infrastructure`. The source recipe is `Dockerfile.postgres-debian`; root must not switch this recipe into the shared manifest implicitly.

```text
BASE=postgres@sha256:7bade6d532592ca8ce7ee32def7399dad2607c4ea5583839fc4352a095a11ea6
SOURCE_SHA=<exact 40-character commit containing this Dockerfile>
platform=linux/amd64
target=candidate (the default final stage)
```

The shared common stage updates normal bookworm repositories while holding PG17 server/client at `17.11-1.pgdg12+2`. If that exact server-dev package can no longer be resolved, the builder fails instead of choosing another major/version or disabling signature checks. Dependency freshness therefore changes between builds; archive/config digest and package versions, not source commit alone, identify the built artifact.

The builder has compilers/server headers; the final stage starts from the common runtime stage and copies only `/stage`. Required runtime libraries have a builder/final dpkg version comparison and the main extension modules have `ldd` missing-library checks. These checks are built-in assertions, not locally executed PASS evidence. Remote strict scan still covers the complete final image, including gosu and dependencies. Source-built PostGIS may not acquire an OS-package CVE entry, so actual `PostGIS_Lib_Version()`/extension version and direct official-advisory review remain mandatory alongside Trivy; disappearance of a package record is not proof of a fix.

Final image USER is `999:999`, with the official PostgreSQL entrypoint/CMD/PGDATA/volume/stop signal inherited. No root startup chown is available in this candidate. Fresh fixture volumes must inherit the correctly owned base directory; restored fixture volumes must already have verified UID/GID999 ownership. Existing live directories must never be recursively chowned by this build or a convenience workaround.

No `initdb`, `CREATE EXTENSION`, `ALTER EXTENSION`, `ALTER DATABASE ... REFRESH COLLATION VERSION`, or data-volume operation runs at image build time. Empty fixture database initialization and explicit extension creation belong to root's synthetic fixture. An already initialized production volume must never be treated as empty to activate extensions.

Image evidence directory `/usr/share/map-candidate/` contains base/final dpkg inventories, apt policy, glibc version, PostGIS source URL/hash, Version.config, configure log, and builder/final runtime dependency lists. Export these as public artifact evidence after checking for unintended environment output. Runtime credentials are not build args or inputs.

## Exact interface request to root (not applied here)

1. Keep the existing `postgres.current` reference unchanged. Replace only that row's candidate selector/build as follows after taking this commit:

   ```json
   {
     "service": "postgres",
     "candidate_selector": "postgres@sha256:7bade6d532592ca8ce7ee32def7399dad2607c4ea5583839fc4352a095a11ea6",
     "build": "postgres-debian"
   }
   ```

2. `scripts/scan-infrastructure-candidates.py:load_spec` currently allows four recipe names and prohibits every repository change. Add `postgres-debian` only for `service == postgres`, the exact old `postgis/postgis@sha256:01a6a70e41e6c4467c8f55f6063555ed72db2d6662cd0d571040d42eadaeb6f6`, and the exact new selector above. Retain the current repository-equality check for all other cases. A generic repository-check removal is not requested. `build_candidate` already maps recipe names to `Dockerfile.<build>` and passes BASE/SOURCE_SHA; no custom live receiver hook is needed.

3. The source compile may require a measured increase from the existing 1200s candidate-build timeout; never overlap it with native builds. Keep CPU2 compiler concurrency. An unbuilt recipe has no recorded build duration.

4. Root's PG fixture must assert old170005, new>=170011 within major17, old3.5.2/new3.5.7 within PostGIS3.5, process UID/GID999, `LC_COLLATE/LC_CTYPE=en_US.utf8`, UTF8 and glibc. Preserve topology/pgcrypto availability checks and per-role denial tests. Add deterministic synthetic Korean/Latin/accented/case/numeric text and unique/index/order comparisons through old dump → new restore/new writes/restart → old backup restore. Capture old/new collation provider versions and index behavior; locale name equality alone is insufficient.

5. Debian11→12 changes glibc. This candidate allows a **logical migration investigation**, not direct physical PGDATA reuse. Use the approved backup/restore plan, test rebuilt indexes and constraints under the new locale implementation, and retain untouched old data/backup for rollback. Do not relabel a database C or refresh collation metadata to hide a mismatch. A candidate-to-old logical export, an old-image direct read, and restoration of an old backup are distinct compatibility claims.

6. Run a separately identified paired current/candidate PG strict scan using the same frozen DB checksums/metadata and exact OCI/config identities. Preserve all HIGH/CRITICAL including unfixed. Record fixture execution independently of scan success. Neither this recipe nor an all-green synthetic fixture authorizes live GCP ACL/deployment or migration.

## Checks actually performed

Read-only repo HEAD/refs/dirty/worktree inventory; own worktree at base `475d8d56654b9521197ebc00c2d1cbc5af8f2d2f`; own exclusive delegated claim `infra-postgres-debian.json`; graphify AST update and query before source inspection; official registry manifest/config byte hashes and platform/version fields; normal PGDG package-list comparison; source archive SHA256/official MD5/Version.config and configure/staging/upgrade rules; Dockerfile shell syntax and whitespace review. Graph AST update is repeated after the edits. No Docker daemon, local compilation, paid provider, CI workflow or GCP change was invoked.
