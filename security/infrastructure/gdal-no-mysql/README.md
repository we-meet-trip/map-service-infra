# Separate GDAL MySQL connector removal candidate

This recipe is candidate-only. It does not replace the manifest's default
Bookworm recipe, the already scanned full Trixie recipe, or any serving image.
Select it explicitly with `--services postgres --postgres-variant trixie-no-mysql`.
A successful build never changes the strict HIGH=0 / CRITICAL=0 security gate.

The full Trixie candidate's PGDG `libgdal39` package directly depends on
`libmariadb3`. PostGIS raster links GDAL; this is a transitive dependency of the
PostGIS image, not proof that PostgreSQL is running a MariaDB server. The exact
package relationship is retained in `../postgres-trixie/package-dependencies.json`.
Root's read-only GCP check on 2026-09-07 found only plpgsql and postgis installed;
postgis_raster was absent, and its two queried GUCs were NULL (not an observed
configured value). This narrow candidate removes the GDAL MySQL input driver
and its actual dynamic dependency. It does not disable raster, topology,
SFCGAL, geometry, protobuf, address standardization, or PostgreSQL features.

The original source version 3.13.2 is retained. Official release source and
separate autotest archive bytes are checked against the sizes and SHA256 values
published by the OSGeo/gdal GitHub release API. `upstream-release.json` records
that primary metadata. No claim is made of detached GPG signature verification.
APT installs use normal signed repositories; no authentication bypass exists.
Transient HTTP/transport failures retry at most three times against the same URL.
TLS verification and checksum failures do not retry or weaken their checks.

The disposable builder installs the exact distribution GDAL development package
and records its complete raster/vector driver inventory. The new source build
turns off `GDAL_USE_MYSQL` and `OGR_ENABLE_DRIVER_MYSQL`. Its resulting inventory
must remove exactly `MySQL`: any other missing driver or any new driver fails.
Four named upstream C++ CTest groups must exist and pass. Distribution gtest
avoids an unpinned CMake dependency download. Source extraction rejects traversal,
links, and duplicate paths; no source is executed on the developer's computer.

An actual dpkg-owned `libgdal39` package with `Source: gdal`, upstream-preserving
version `3.13.2+map1`, license, normal ldconfig lifecycle, and dependencies derived
by dpkg-shlibdeps is installed and held. There is no force removal, dummy package,
empty dependency list, package database editing, or scanner ignore. The exact
package bytes and library bytes are receipted. All original PostGIS build/runtime
package equality checks remain. Final ldd and dpkg checks reject missing libraries
or installed MariaDB runtime packages.

The complete synthetic old/new/restart/logical restore/ACL/locale/SFCGAL fixture
remains required. A new rolled-back probe exercises actual PostGIS to GDAL
GeoTIFF/PNG/JPEG encoding and decoding, pixel values, dimensions and lossless
GeoTIFF georeferencing at every existing observation. JPEG's constant-pixel
answer has an explicit tolerance of one. No existing database or setting changes.

This does not resolve PostgreSQL's own libxml2 dependency or inherited Perl
findings. Historical Bookworm and full Trixie counts used different Trivy DB
editions, so their count difference is not an isolated distro comparison. Every
new run must freeze its own current/candidate scanner DB and preserve all findings.
The 90-minute build timeout is a ceiling for this separate source build; full
workflow ceiling remains 150 minutes. Root must allocate the serial hosted build
slot. No local Docker build, real-user data, paid API, NCP or GCP mutation occurs.

Primary sources reviewed 2026-09-07:

- https://github.com/OSGeo/gdal/releases/tag/v3.13.2
- https://gdal.org/en/stable/development/building_from_source.html
- https://raw.githubusercontent.com/OSGeo/gdal/v3.13.2/autotest/cpp/CMakeLists.txt
- https://security-tracker.debian.org/tracker/CVE-2026-44172
- https://security-tracker.debian.org/tracker/CVE-2026-49261
- https://postgis.net/docs/RT_ST_AsGDALRaster.html
- https://postgis.net/docs/RT_ST_FromGDALRaster.html
- https://postgis.net/docs/RT_ST_GDALDrivers.html

The corresponding PGDG source package patch series was read and checksum-bound
to its official .dsc: it contains only data repack and Python installer changes,
no security backport patch. The source-based candidate excludes the same two
redistribution-excluded WKT files from its installed data, preserves the complete
distribution copyright inventory plus upstream/Lerc notices, and explicitly
requests Debian `hardening=+all` flags. Runtime ELF RELRO, BIND_NOW and stack
protector symbols are required. `distribution-review.json` records the exact
metadata scope; this read-only review does not claim .dsc signature verification.
