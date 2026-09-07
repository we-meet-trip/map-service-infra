# Standalone User migration deployment

This working branch wires User's plain migration entrypoint into `cloud-up.sh`
before any application is started. It removes shared PostgreSQL credentials from
User serving and passes `USER_DATABASE_USER=map_user_runtime` with its own password.
The new receiver rejects source without `MAP_USER_STANDALONE_MIGRATION_VERSION=1`.
This is a forward privilege transition; an old R3 image/environment is not a
compatible rollback. A failed migration keeps public cutover closed under the
existing mandatory policy. Do not grant User serving owner/superuser rights.

The root operator separately prepares `/etc/map-deploy/user-migration.env` mode0600
with exactly `USER_MIGRATION_URL`, `USER_MIGRATION_USERNAME=map_user_migrator`, and a
password different from serving. Its same-host URL is
`jdbc:postgresql://postgres:5432/<reviewed_database>?currentSchema=user_service`.
These three values must never appear in serving Compose environment. The rendered
Compose stream is private input to `scripts/user-migration-job.py`, not a log file.
It requires the exact candidate `ghcr.io/we-meet-trip/map-service-user@sha256:…`.
The container image is already present and is not pulled by this helper.

The job has no application ports, host mounts, Docker socket, SSH/provider keys,
Redis environment or external network access. A labelled internal bridge with
`com.docker.network.bridge.gateway_mode_ipv4=isolated` and IPv6 disabled connects
only the existing PostgreSQL container and the temporary job. A plain internal
bridge is rejected: its gateway can expose host services. An engine which cannot
provide isolated gateway mode must fail, without a less restrictive fallback.
This follows Docker's [gateway mode contract](https://docs.docker.com/engine/network/port-publishing/#gateway-modes);
actual PG connectivity and denied host/Redis/external paths still require an
isolated Linux execution test. Connecting this
additional network does not recreate PostgreSQL or attach a new data volume.
Existing network labels/endpoints and DB container identity are checked first.
The network is kept for subsequent jobs; its other endpoint must remain only PG.

Each job uses UID10001, no capabilities, no-new-privileges, a read-only root,
64MiB temporary memory filesystem, 384MiB memory and0.5CPU,128PID limits, no restart
and no daemon log retention. The image's `/usr/bin/timeout` terminates the plain
Java main after300seconds plus10seconds grace even if its host launcher dies.
A separate host deadline handles the normal cleanup path. A local launcher lock
and an active-job check reject overlapping migrations. A previously running job
is never silently stopped or bypassed. The host copy of the migration environment
is removed immediately after Docker consumes it; the job container is removed on
normal success/failure. Before writing that copy, a private ownership marker is
written and synced. A killed launcher or lost Docker create response can leave a
created-only container with credentials and a private temporary directory. A
created-only container has no running timeout yet. The next locked attempt first
rejects every running job, then removes only its labelled, stopped/created jobs
and directories with a valid project/image/name marker. Unknown files, links or
missing/foreign markers fail closed for operator review; they are not deleted.
No existing service container, database row or volume is removed.

Serving must target `postgres:5432`, matching the migrator's reviewed DB name.
Spring configuration, alternate loader/JVM arguments and Compose command or
entrypoint overrides are rejected. The existing exact memory setting
`JAVA_TOOL_OPTIONS=-XX:MaxRAMPercentage=70` is the only allowed JVM override.

Success requires a zero container exit, no OOM, exact image identity and the plain
migrator's structured completion receipt. Raw output and credentials are withheld.
A failed or missing receipt prevents serving. Check-config validates only config;
it does not establish database privileges. Applied migrations are unmodified.

Before live use, inventory owners, grants and defaults; retain a verified backup;
apply the reviewed User role transfer while its serving/consumers are stopped;
and explicitly preserve other service rights before removing inherited PUBLIC
rights. The current GCP catalog has PUBLIC TEMP and SELECT on PostGIS reference
metadata. Runtime's existing cross-schema guard must pass without widening it to
hide a permission failure. No provisioning SQL or secret rotation is performed by
this runner. Empty-host historical bootstrap remains a separate requirement; the
normal migrator must continue to reject an unbootstrapped DB.

Validation scope: fourteen launcher tests exercise distinct private credentials,
immutable targets, forbidden target overrides, gateway isolation, no overlap,
lost-create recovery, timeout cleanup and private environment removal; real
Compose interpolation checks the serving credential boundary and launcher
contract. These mocked Docker tests are not real container
acceptance. The exact-image timeout/network/plain-JVM path, current GCP ACL
transition and restored-host operation still require isolated Linux and GCP
execution before deployment promotion.
