# Infrastructure security candidates — 2026-09-07

This is a candidate-only interface. It does not modify Compose, receivers, the GCP host, any existing volume or ACL. Redis, patched Caddy and OSRM remain unchanged. The nine historical counts (596 HIGH / 43 CRITICAL) are provenance, not a new scan result.

## Execute and assess

`python3 scripts/scan-infrastructure-candidates.py` only validates and prints the plan. `--run` is restricted to a GitHub-hosted Linux runner. Dispatch `infrastructure-security-candidate.yml` on the exact candidate branch with `services=all`. One job serially scans/builds/tests selected services. No registry publication or deploy is performed; no environment secrets are requested.

Each current reference is immutable. Each candidate tag is resolved to one linux/amd64 manifest before building. OS updates use the selected distribution's normal repositories. The fixture-loaded Docker image config must match the OCI archive scanned. The archive manifest/config, source SHA, selected base digest and SHA-256 are preserved for the existing root receiver owner to review. Candidate labels and strict scans do not authorize installation.

A single Trivy executable digest and a freshly downloaded DB are frozen across the whole run. Current and candidate are scanned with HIGH/CRITICAL, unfixed included, no ignore file, complete package inventory, and exit code 1 on findings. An execution failure is INCOMPLETE. Scan output, package/Go module inventory, vulnerability changes, database metadata/checksum and exact OCI archives remain artifacts even when the job fails. Zero findings alone does not establish data compatibility or operational acceptance.

## State and rollback boundaries

- PostgreSQL: retains major 17 and PostGIS 3.5. The default candidate uses official PostgreSQL17.11 Bookworm, PostGIS3.5.7 and UID/GID999; explicit `--postgres-variant trixie --services postgres` selects an independent Trixie comparison. Both are **logical dump/restore candidates**, not permission to reuse the current data directory. Synthetic schema owners, runtime ACL denials, spatial/json/binary values, identity sequences, extensions, forward writes/restart, pre-upgrade old-image restore and post-upgrade dump restore into old image are actually executed by CI. The synthetic `en_US.utf8` corpus compares glibc locale/index behavior. SFCGAL fixed intersection/solid-volume answers are checked at each baseline, forward, restart and logical-recovery observation. Production collation, all production extensions and application queries still need separate source-specific acceptance and a reviewed backup/recovery plan.
- Prometheus: cold TSDB/WAL copy, fixed historical sample query on candidate, immutable pre-upgrade backup and old-image rollback. Candidate-written TSDB direct downgrade is separately reported; failure requires restoring the retained pre-upgrade copy. Retention gap and live scrape cutover are not tested.
- Grafana: isolated SQLite, synthetic administrator and dashboard, candidate migration/read/restart, stopped pre-upgrade database copy and old-image rollback. Candidate-written database direct downgrade is separately reported and never substitutes for a backup. No production credentials, plugins, encryption key or SQLite file are used.
- Exporters/proxy: isolated startup and HTTP content. PostgreSQL exporter additionally authenticates a fresh synthetic PG17.11 SCRAM monitor role, checks DDL denial and collects two real database metric samples. No serving DB credentials, host filesystem or Docker socket is mounted. Curl: network-disabled TLS protocol/binary smoke; it does not issue a DuckDNS update. Operational integrations need root acceptance.

All fixture resources use fresh random names and ownership labels. Cleanup verifies ownership before deleting only containers and volumes created by that run. Mac Docker/Gradle/Flutter daemons are never started by the plan or local tests.

## Official review inputs

Reviewed 2026-09-07. Use each scan record's PrimaryURL and the actual package/version/status; do not transfer old tag findings onto patched digests.

- [PostgreSQL 17 security table](https://www.postgresql.org/support/security/17/) identifies server, client and contrib vulnerabilities and fixed minors. Candidate minimum 17.11; runtime credentials do not make pg_dump/client vulnerabilities irrelevant to backups.
- [PostGIS 3.5 release notes](https://www.postgis.net/docs/manual-3.5/en/release_notes.html) describe 3.5.6 privilege escalation and 3.5.7 fixes. Minimum 3.5.7 is checked against the running extension library.
- [Prometheus releases](https://github.com/prometheus/prometheus/releases) and [storage documentation](https://prometheus.io/docs/prometheus/latest/storage/) supply version/storage context; TSDB compatibility is measured in the fixture.
- [Grafana OSS download](https://grafana.com/grafana/download?edition=oss) and [security advisories](https://grafana.com/security/security-advisories/) supply upstream version/advisory context. Version 13.2.1 or an OS upgrade is not a zero-CVE assertion.
- [Go vulnerability database](https://vuln.go.dev/) supplies module advisories for exporter/binary findings; runtime privileges and HTTP inputs must be considered separately.

## Review status

Local contract checks are executable under `security/infrastructure/tests/`. Their existence does not mean remote images/fixtures passed. The actual run ID, report counts, exact digests, fixture results and any failed gates belong in `evidence/parallel/infra-security/HANDOFF.md`. This document cannot authorize GCP/NCP live changes, data replacement, develop/master merge or store publication.

## Explicit PostgreSQL comparison

The manifest keeps the tested Bookworm row unchanged. `postgres_alternatives.trixie`
is a pinned descriptor accepted only for the PostgreSQL service and its existing
exact current image. The CLI and workflow require a `postgres`-only selection for
Trixie; ordinary `services=all` continues to use Bookworm. The selected variant,
source SHA, fresh scanner DB and new OCI identity are retained in each run.

```sh
python3 scripts/scan-infrastructure-candidates.py --postgres-variant trixie --services postgres
```

Only the hosted workflow may add `--run`. The new variant has its own full build,
scan and complete restore fixture. Previous Bookworm results never satisfy it.
SFCGAL probes use documented PostGIS3.5 `CG_3DIntersection`, `CG_Extrude`,
`CG_MakeSolid` and `CG_Volume`; transaction rollback preserves the existing
extension catalog/dump contract. Known line overlap has length1, x-range1..2 and
Z0; an extruded unit square of height2 must have volume2. This is measured
function coverage, not all SFCGAL algorithms or production-data compatibility.
Sources: https://postgis.net/docs/manual-3.5/CG_3DIntersection.html and
https://postgis.net/docs/manual-3.5/CG_Volume.html (reviewed 2026-09-07).
