# Independent administrator and learning roles

These files implement deployable role contracts and local validation. No independent
administrator/learning VM has been provisioned, and no current GCP services or data
have been moved by these files. Providers, specifications and budgets still require
the user's decision. They do not authorize NCP resources or real-user learning.

## Central administrator

`docker-compose.role-admin.yml` is a separate project/volume/daemon/host contract.
It contains the control PostgreSQL, Admin API/Web, an explicit one-shot migration
profile, Prometheus and authenticated Grafana. All host-facing ports bind loopback
for an independently authenticated TLS/VPN access path. No application stack lives
in this role. `admin`/`control-postgres`/`prometheus` DNS is local to this role only;
remote targets must have private HTTPS FQDNs, authenticated APIs and verified TLS.
Private resolution, firewall routes, actual certificates and individual Grafana
permissions must be verified on the chosen host; DNS spelling is not that proof.

Prepare dedicated external volumes and private runtime/migration/bootstrap secret
files. Required image variables must contain exact `@sha256:` references whose
provenance/security reports have been verified; these files supply no unverified
mutable default. Do not point the external volume variables at existing GCP state.

On an empty, dedicated control DB, run the reviewed Admin repository
`scripts/bootstrap-control-db.sql` as the provisioner, apply `alembic upgrade head`
using only `ADMIN_CONTROL_MIGRATION_DATABASE_URL`, then apply
`scripts/grant-control-runtime.sql`. The runtime receives only
`ADMIN_CONTROL_DATABASE_URL` for `map_admin_runtime`, `ADMIN_RUN_MIGRATIONS=false`,
bootstrap/session settings and explicit `ADMIN_TARGETS`. Its database has no
`hub_data` requirement. Every default/test/prod target declares User/Hub/Agent
HTTPS URLs and an environment-specific internal token. The central role uses
API-only targets; SQL and Redis diagnostics are explicitly unconfigured until the
owning services expose equivalent management APIs. Missing coverage is not green.

Prometheus configuration is JSON syntax in `prometheus.yml` (valid YAML subset),
so the stdlib verifier can inspect the exact file without a parser dependency.
Every remote scrape uses `scheme:https`, certificate verification, an authorization
`credentials_file` under `/etc/prometheus/credentials/`, and explicit FQDN/port
`static_configs` with `map_environment:test|prod`. Root's application-host
exporters stay on their actual target host. Grafana has anonymous access disabled;
restoring central accounts, dashboards, time series and alert delivery remains a
required host migration/restore exercise.

Render with `docker compose --profile '*' --env-file <private-file> -f
<role-compose> config --format json` into a 0600 private file (rendering contains
secrets). `scripts/verify-role-manifest.py` verifies the exact Compose SHA, rendered
image/mount/credential/network contract, expected host identity and deploy account.
Pass `--scrape-config` for admin. The manifest fields are `schema_version:1`,
`role:admin|learning`, `host_identity`, `deploy_account`, `compose_sha256`, and
`data_scope:control|synthetic`. Host identity/account values must come from the
receiver's root-owned policy, not an untrusted request. Manifest integrity is not
publisher authentication: receiver provenance verification remains mandatory.

A role-specific lock/receiver and verified one-time ownership handoff must precede
stopping old co-host Admin/Prometheus/Grafana. Root owns that existing-receiver
change. This Compose alone cannot prevent the old GCP receiver from recreating old
roles. Rollback restores the previous central release and its own data snapshot;
never redirect production clients to the GCP test database.

## Dataset-only learning worker

`docker-compose.role-learning.yml` starts no job by default. Only the explicit
`approved-synthetic-job` profile runs `scripts/dataset-worker.py`. It has no network,
HTTP ports, devices, Docker socket, SSH, serving DB/Redis or location key. Its only
writable mount is a dedicated private candidate output directory; memory is 512 MiB,
CPU 1 and PID count 64. Source export is a different source-environment process.

`training/segment_stats.py` is a byte-for-byte pinned program artifact copied from
the existing Agent evaluator, with provenance in `training/segment-stats-source.json`.
The wrapper verifies its SHA before import; an upstream change requires a reviewed
artifact and pin update. This source snapshot avoids relying on an unshipped Agent
`eval` directory at deployment time. `TRAINING_PROGRAM_FILE` points to this artifact.
No new statistical ranking algorithm is introduced here.

A 0600 job JSON in the read-only input directory contains `schema_version:1`, a safe
`job_id`, `data_scope:synthetic`, `approved:true`, a basename-only `dataset` JSONL,
`dataset_sha256`, `rows`, `max_bytes`, `max_seconds`, and `min_support` (at least 3).
Only synthetic data is accepted while real-user training is HOLD. A verified
operator/deployer must still supply the job manifest; a JSON boolean is not evidence
of consent, allowed provider use or deletion propagation.

The worker validates input checksum, row shape/count, at most 10,000 rows/64 MiB,
and at most 300 seconds. It rejects serving credential environment variables,
changed program bytes and path escape. A job directory lock prevents concurrent
writers. Matching completed jobs return the existing verified result; conflicting
IDs fail. Output JSON is private and atomically published with checksum receipt;
SIGTERM/SIGINT and timeout record cancellation/failure. An unreceipted candidate is
not usable after interruption. Files are not automatically deleted.

The candidate has `serving_promotion:false`; it is never mounted into Agent serving.
Separate distributed leases, dataset eligibility/deletion indexes, evaluation,
reviewer approval, promotion and rollback remain gates. Existing `segment_stats`
counts session/candidate support, which is not proven distinct-user support; this
synthetic-only worker does not make it eligible for real-user personalization.

Validation: `python3 -m unittest discover -s tests -p 'test_role*.py' -v` checks the
real Compose render, host/credential/network denial cases, exact pinned aggregator,
synthetic candidate checksum, private modes, HOLD and duplicate/conflicting jobs.
This local result does not prove remote host isolation, endpoint reachability,
full backup/restore, Grafana rendering or RPO/RTO.

## GCP에서 중앙 관리자 이전 후 serving 배포를 유지하는 계약

중앙 서버를 검증한 다음, GCP root가 `/var/lib/map-deploy/admin-handoff.json`과 그 SHA256을 참조하는 `topology.json`을 설정한다. 이 파일들은 infra checkout 밖에 있어 앱 rollback으로 사라지지 않는다. 현재 호스트에는 **설정하지 않았다**. 기존 결합 상태에서 임의로 이 플래그를 켜면 관리자 이전 증거가 없는 채 접근을 잃으므로 금지한다.

`admin-handoff.json`은 status PASS, application_instance_id `2327348931395410137`, 서로 다른 central_instance_id, checks의 control_auth/target_read/target_isolation/browser_charts/audit_restore/serving_survives_admin_failure 모두 PASS를 실제 독립 호스트 증거로 기록해야 한다. `topology.json`은 schema_version 1, instance_id, mode `application`, verified_admin_handoff_sha256을 담고 root 소유·group/other 쓰기 금지로 설치한다. receiver 자체도 root 설치본을 갱신해야 한다. 운영자 제공 사실을 증명하지 못하는 JSON만 작성해 gate를 우회하지 않는다.

활성화 전 순서: 새 receiver 및 현재 checkout의 detached 지원 검사 → 독립 관리자 계정/감사 DB 복원·API/화면·권한/장애 시험 → 대상 호스트에 최소 권한 metrics 자격과 사설 TLS/auth 수집 gateway 설치·검증 → 기존 중앙 역할 프로세스(admin/admin-web/prometheus/grafana/cadvisor)만 중지 → 실제 검증 증거와 root topology 설치. 기존 DB/볼륨은 보존한다. GCP의 이전 Prometheus/Grafana 자료는 삭제하지 않고 별도 이관·보존한다.

활성화한 receiver는 `docker-compose.target-exporters.yml`의 PostgreSQL/Redis/node exporter만 현 GCP 호스트에 유지한다. node exporter의 host root/PID는 이 serving 호스트를 본다. PostgreSQL에는 전용 pg_monitor login DSN, Redis에는 metrics용 ACL 자격을 별도로 요구하며 기존 관리자/DB owner 비밀번호를 자동 fallback하지 않는다. 세 원시 exporter 포트는 loopback으로만 노출되므로 외부 중앙 Prometheus가 접근하려면 별도 사설 TLS/auth gateway가 필요하다. 아직 이 네트워크/계정 provision은 실행하지 않았다.

앱의 readiness는 중앙 관리자 가용성을 기다리지 않는다. 이전 관리자 앱 이미지 override를 merge하지 않고, target exporter의 이전 exact image ID만 rollback 파일에 보관한다. 과거 active 목록에 retired 관리자 항목이 있어도 복귀는 거절한다. 기존 co-host 모드는 topology 파일이 없을 때 그대로 유지되며, 독립 역할 전환과 일반 앱 배포는 별개 변경이다. 로컬 5개 topology 회귀와 실제 cloud-up shell 경로 회귀를 실행했다. 독립 VM 4대의 실제 장애 격리나 RPO/RTO 측정을 대신하지 않는다.
