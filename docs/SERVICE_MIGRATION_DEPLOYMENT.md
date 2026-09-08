# Standalone service migration deployment

This working branch wires the plain migration entrypoint of User, Hub and Agent
into `cloud-up.sh` before any application is started. Each serving container
keeps only its own restricted runtime login: User reads
`USER_DATABASE_USER=map_user_runtime`, Hub reads a `HUB_DATABASE_URL` naming
`map_hub_runtime`, and Agent reads `POSTGRES_USER=map_agent_runtime` with
`AGENT_DATABASE_PASSWORD`. The database container's own operator password is no
longer inherited by any service, and the metrics exporter uses a `pg_monitor`
login through `POSTGRES_EXPORTER_DSN`. The new receiver rejects source without
`MAP_USER_STANDALONE_MIGRATION_VERSION=1` and `MAP_SERVICE_MIGRATION_VERSION=1`.
This is a forward privilege transition; an older image or environment that
expects a shared operator login is not a compatible rollback. A failed migration
keeps public cutover closed under the existing mandatory policy. Do not grant any
serving container owner or superuser rights.

The root operator separately prepares one mode0600 file per service, each holding
only that service's migrator credential and a password different from serving:

| service | file | keys | same-host target |
|---|---|---|---|
| user | `/etc/map-deploy/user-migration.env` | `USER_MIGRATION_URL`, `USER_MIGRATION_USERNAME=map_user_migrator`, `USER_MIGRATION_PASSWORD` | `jdbc:postgresql://postgres:5432/<reviewed_database>?currentSchema=user_service` |
| hub | `/etc/map-deploy/hub-migration.env` | `HUB_MIGRATION_DATABASE_URL` naming `map_hub_migrator` | `postgresql+psycopg://…@postgres:5432/<reviewed_database>` |
| agent | `/etc/map-deploy/agent-migration.env` | `AGENT_CHECKPOINT_MIGRATION_DSN` naming `map_agent_migrator` | `postgresql://…@postgres:5432/<reviewed_database>` |

These values must never appear in serving Compose environment, and a file
prepared for one service is rejected by the other two. The rendered Compose
stream is private input to `scripts/service-migration-job.py`, not a log file. It
requires the exact candidate `ghcr.io/we-meet-trip/map-service-<service>@sha256:…`
for the service being migrated. The image is already present and is not pulled by
this helper. Each service also has its own launcher lock, its own labelled job
kind and its own private network, so one service's recovery never touches
another's job.

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
main after the service's own deadline plus10seconds grace even if its host
launcher dies:300seconds for User's Flyway run,900seconds for Hub's Alembic run
and for Agent's concurrent index creation. A separate host deadline handles the
normal cleanup path. A local launcher lock
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

Serving must target `postgres:5432`, matching the migrator's reviewed DB name,
and the migrator password must differ from the runtime password. For User,
Spring configuration, alternate loader/JVM arguments and Compose command or
entrypoint overrides are rejected; because Spring binds the environment loosely,
`spring.datasource.url`, `SPRING.DATASOURCE.URL` and `spring_datasource_url` are
folded to one spelling before that check. The existing exact memory setting
`JAVA_TOOL_OPTIONS=-XX:MaxRAMPercentage=70` is the only allowed JVM override. For
Agent, `LANGGRAPH_SCHEMA` must be a plain identifier and the job resolves the
same schema the serving container reads.

Success requires a zero container exit, no OOM, exact image identity and the
migrator's own completion receipt: User's structured JSON record, Hub's
`Hub migration completed` line, Agent's `Agent checkpoint migration completed`
line, each appearing exactly once. Raw output and credentials are withheld. A
failed or missing receipt prevents serving. Check-config validates only config
and exists for User only; it does not establish database privileges.
Applied migrations are unmodified. After the three jobs, `verify_hub_revision`
compares the revision the Hub image ships with the revision the database records
and blocks startup on any difference. That check never runs an upgrade, because
only the isolated job holds a migrator credential.

Before live use, inventory owners, grants and defaults; retain a verified backup;
apply the reviewed role transfer while its serving/consumers are stopped;
and explicitly preserve other service rights before removing inherited PUBLIC
rights. The current GCP catalog has PUBLIC TEMP and SELECT on PostGIS reference
metadata. Runtime's existing cross-schema guard must pass without widening it to
hide a permission failure. No provisioning SQL or secret rotation is performed by
this runner. Empty-host historical bootstrap remains a separate requirement; the
normal migrator must continue to reject an unbootstrapped DB. The Admin
application keeps its existing separate `map_admin` login in this phase; only the
three shared operator logins are separated here.

Validation scope: twenty-five launcher tests exercise distinct private
credentials per service, cross-service credential and job rejection, immutable
targets, forbidden target overrides, gateway isolation, per-service networks and
locks, no overlap, lost-create recovery, timeout cleanup, per-service commands
and deadlines, completion-receipt uniqueness and private environment removal;
real Compose interpolation checks the serving credential boundary and the
launcher contract for all three services. These mocked Docker tests are not real
container acceptance. The exact-image timeout/network/plain-JVM path, current GCP
ACL transition and restored-host operation still require isolated Linux and GCP
execution before deployment promotion.
