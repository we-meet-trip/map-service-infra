# GCP test database bootstrap (new empty host)

[`scripts/gcp-test-db-bootstrap.sh`](../scripts/gcp-test-db-bootstrap.sh) takes the
new GCP test VM `map-test` (project `mapservice-test`, database `map_test`) from "no
database" to the state in which the normal migration jobs (`cloud-up.sh` →
`service-migration-job.py` for user, hub and agent) have nothing left to apply and
every serving container passes its own database guard. It is step 9 of the new test
host enrollment in [CUTOVER_SUPERVISOR.md](CUTOVER_SUPERVISOR.md). Nothing is
restored: the test database is new and holds synthetic data only. It is never run
against production or against a database that already serves.

## Why the first boot must not run `db/init`

| Fact | Source |
|---|---|
| The first boot of the Compose `postgres` service runs `db/init`: PostGIS, schemas `user_service`, `hub_data`, `langgraph`, `admin_data` and the `map_admin` login | `docker-compose.yml` (postgres volumes), `db/init/00-create-schemas.sql`, `db/init/10-admin.sh` |
| The image entrypoint creates `POSTGRES_DB` and runs `/docker-entrypoint-initdb.d/*` only when the data directory has no `PG_VERSION`; `POSTGRES_DB` defaults to `POSTGRES_USER` | `postgis/postgis:17-3.5` `docker-entrypoint.sh` |
| User's prepare SQL refuses any extension other than plpgsql, any non-system schema, relation, routine or type, default ACLs, any other backend on the target and pre-existing `map_user_*` roles | map-service-user `docs/user-database-bootstrap-prepare.sql` |
| The target is created from `template0` and marked `map-user-bootstrap:v1:<64 hex>` before PostGIS or any schema exists; the normal User migrator refuses a database without genuine V001–V004 history | map-service-user `docs/user-database-bootstrap.md`, [SERVICE_MIGRATION_DEPLOYMENT.md](SERVICE_MIGRATION_DEPLOYMENT.md) |
| PostGIS grants SELECT on `public.geometry_columns` and `public.geography_columns` to PUBLIC; the User runtime and migrator guards accept only `public.spatial_ref_sys` outside `user_service`, so `migrate`, `validate` and serving stop with `database_privilege_cross_schema_data` | PostGIS `postgis.sql.in` (3.5); map-service-user `UserDatabasePrivileges.java` |
| Hub revision 0001 runs `CREATE SCHEMA IF NOT EXISTS hub_data`, which needs database CREATE even when the schema exists; only a strictly empty first install gets a temporary grant, revoked in a finalizer | map-service-hub `docs/database-roles.md` |
| Hub and Agent role SQL must be re-applied after every migration; `cloud-up.sh` does not do that | map-service-hub and map-service-agent `docs/database-roles.md`, `scripts/cloud-up.sh` |
| Prepare revokes PUBLIC rights on the database and on schema `public`, so CONNECT and `public` USAGE are granted explicitly where PUBLIC used to supply them | `user-database-bootstrap-prepare.sql` |

So the first boot runs no init script and creates no application database, the User
bootstrap goes first, PostGIS and the `db/init` content come after its finalization,
the two PostGIS views lose PUBLIC SELECT (owner approval), and the first Hub migration
runs inside the bootstrap under a temporary grant.

## Who does what

| Owner | Operator (root on the VM, over IAP) |
|---|---|
| Approves withdrawing PUBLIC SELECT on `public.geometry_columns` and `public.geography_columns` (`--approve-postgis-view-revoke`) | Runs `check`, then `run`, then `verify` |
| Keeps `test-user-runtime-db-password` and `test-user-migration-password` as two different 64-character lowercase hex values (the User activation SQL refuses anything else; `gcp_secrets.py generate` makes url-safe tokens) | Validates every secret's shape without printing it |
| Chooses the `image-release` run; with the owner's GitHub login prepares the bundle, downloads the User CI artifact and computes the SQL pins | Verifies bundle, artifact digest and SQL pins on the host |
| Decides recovery after any HOLD | Quarantines a failed one-use attempt and stops; never retries it |

## Prerequisites

1. Enrollment steps 1–8: `/usr/local/lib/map-deploy/release_manifest.py` and
   `deploy-gcp.py` installed, `/etc/map-deploy` and `/var/lib/map-deploy` root-owned
   (the latter mode 0700), and a clean root-owned checkout
   `/srv/map-test/map-service-infra` at the reviewed commit, which contains this
   script.
2. Host packages; a new Ubuntu 24.04 image has none of the first two:
   - Docker Engine from Docker's own apt repository: `docker-ce` 28 or newer (the test
     host pins `5:29.8.2-1~ubuntu.24.04~noble` with the classic image store) and
     `docker-compose-plugin` 2.24.4 or newer (`!override`). Below Engine 28 the script
     stops with `docker_engine_28_required`: the bootstrap and migration networks need
     gateway mode `isolated`.
   - `google-cloud-cli` from Google's apt repository, so `/usr/bin/gcloud` exists.
     `scripts/gcp_secrets.py` uses that path first and only falls back to a `gcloud`
     on `PATH` (the image's snap).
   - `python3`, `curl`, `flock` and `sha256sum`, which the image already has.
3. `.env.test` (step 5): owner root, mode 0600, a single link. It holds
   `POSTGRES_DB=map_test`, `POSTGRES_USER=map`, `MAP_STACK_ENV=test`,
   `USER_DATABASE_USER=map_user_runtime`, `AGENT_DATABASE_USER=map_agent_runtime`,
   `LANGGRAPH_SCHEMA=langgraph`, `IMAGE_REGISTRY=ghcr.io/we-meet-trip`, and these values
   equal to Secret Manager: `POSTGRES_PASSWORD`, `USER_DATABASE_PASSWORD`,
   `AGENT_DATABASE_PASSWORD`, `HUB_DATABASE_URL` (login `map_hub_runtime`),
   `MAP_ADMIN_PASSWORD`, `ADMIN_DATABASE_URL` (same password),
   `POSTGRES_EXPORTER_DSN` (login `map_pg_exporter`, database `map_test`). The
   migration credentials (`USER_MIGRATION_*`, `HUB_MIGRATION_DATABASE_URL`,
   `AGENT_CHECKPOINT_MIGRATION_DSN`) must not be in `.env.test`.
4. Each of the ten secrets in [Secret flows](#secret-flows) has an enabled version, and
   the VM service account holds `roles/secretmanager.secretAccessor` on each
   (`gcloud secrets get-iam-policy <name> --project mapservice-test`).
5. The VM reaches `ghcr.io` (anonymous pulls by digest) and
   `raw.githubusercontent.com` (the six SQL files).
6. No container of Compose project `map-test` other than `postgres` exists; a new host
   has none.

Confirm on the host before the first `check`:

```bash
docker version --format '{{.Server.Version}}'      # 28 or newer
docker compose version --short                     # 2.24.4 or newer
command -v gcloud; ls -l /usr/bin/gcloud           # the apt package
sudo stat -c '%U %a %h' /srv/map-test/map-service-infra/.env.test   # root 600 1
```

## Inputs and how each is pinned

| Input | Origin | What the script checks |
|---|---|---|
| Release bundle: `release.json`, `compose.images.yml`, `compose.admin-images.yml`, `SHA256SUMS` | `deploy-gcp.py prepare` on the workstation (it compares the GitHub artifact digest) | Every path component from `/` down root-owned, not a link and not group/other writable, and the four files root 0600 with a single link; installed `release_manifest.py verify --expected-run-id --registry-check` (`SHA256SUMS` shows only that the files belong together; the registry check requires each image's OCI revision label to equal its source commit and its version label the release tag); `IMAGE_TAG` and `IMAGE_REGISTRY` in `.env.test` match it; the first `run` records the SHA-256 of `release.json` and every later run must use the same bundle |
| User CI evidence `user-bootstrap-postgres-verification` of the bundle's exact User commit | The zip exactly as GitHub serves it (`gh api …/artifacts/<id>/zip`) | Root, mode 0600, a single link, in root-only directories; SHA-256 of the zip equals `--ci-artifact-digest` (the artifact's `digest` in the GitHub API); exactly one member `user-bootstrap-postgres-verification.json`; `success` true, `source` equal to the User commit and five required checks passed. The digest goes into `inputs.json` and the User receipt |
| Six SQL files: User `docs/user-database-bootstrap-{prepare,finalize,activate,quarantine}.sql`, Hub and Agent `docs/database-roles.sql` | Downloaded on the host from `raw.githubusercontent.com/we-meet-trip/map-service-<repo>/<commit>/<path>` at the bundle's source commits | Each download must have the SHA-256 that the workstation computed at that exact commit, given as one `<sha256>  we-meet-trip/map-service-<repo>@<commit>:<path>` line in the pins file (root, mode 0600, a single link, in root-only directories). A missing or duplicate line, a different download or a changed cache file is a HOLD. Files are cached per commit, so checking one bundle never blocks running another |
| Database passwords | Secret Manager through `gcp_secrets.py materialize` | See [Secret flows](#secret-flows) |
| Owner approval to withdraw PUBLIC SELECT on the two views | `--approve-postgis-view-revoke` | `run` and `postgis-acl` refuse without it |

## Workstation (owner's GitHub login, read-only)

Run in a checkout of this repository at the reviewed commit.

1. Release bundle (it replaces the separate preparation in step 12, which reuses it):

   ```bash
   gh run list -a -R we-meet-trip/map-service-infra -w image-release -b develop -L 5 \
     --json databaseId,headSha,event,status,conclusion,createdAt
   RUN=<chosen develop image-release run ID>
   GH_TOKEN="$(gh auth token)" python3 scripts/deploy-gcp.py prepare \
     --run-id "$RUN" --output "map-bundle-$RUN" --target-environment test
   python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(d["release_tag"]); [print(n, d["services"][n]["source_sha"]) for n in ("user", "hub", "agent")]' "map-bundle-$RUN/release.json"
   ```

2. User CI evidence for `services.user.source_sha`. Accept only a `push` run of
   workflow `ci` with conclusion `success` and an artifact that has not expired. GitHub
   keeps artifacts 14 days, so download the zip before its `expires_at`; a zip kept from
   earlier whose SHA-256 equals the recorded `digest` stays valid input. After expiry
   the owner either re-runs that CI run (`gh run rerun <CI run ID> -R we-meet-trip/map-service-user`,
   possible up to 30 days after the run) and uses its new artifact and digest, or
   chooses a newer release whose User commit has fresh evidence; `image-release` must be
   enabled first (step 14 of the enrollment in [CUTOVER_SUPERVISOR.md](CUTOVER_SUPERVISOR.md)).
   On 2026-10-05 the evidence for the current `develop` release (`image-release` run
   `35982343330`, User `e79fe317`) was artifact `10799874168` of CI run `35982285154`,
   digest `sha256:c2f0cee36371a867a96afd83d5cee49fcb224606674c978a35c7cbecc312825a`,
   expiring `2026-10-08T09:40:37Z`.

   ```bash
   USER_SHA=<services.user.source_sha>
   gh run list -R we-meet-trip/map-service-user -w ci -c "$USER_SHA" \
     --json databaseId,event,headBranch,status,conclusion,createdAt
   CI_RUN=<that run ID>
   gh api "repos/we-meet-trip/map-service-user/actions/runs/$CI_RUN/artifacts" \
     --jq '.artifacts[] | select(.name == "user-bootstrap-postgres-verification") | [.id, .digest, (.expired | tostring), .expires_at] | @tsv'
   ART_ID=<id>; ART_DIGEST=<digest, sha256:...>
   gh api "repos/we-meet-trip/map-service-user/actions/artifacts/$ART_ID/zip" > user-bootstrap-postgres-verification.zip
   printf 'sha256:%s\n' "$(shasum -a 256 user-bootstrap-postgres-verification.zip | cut -d' ' -f1)"   # equals $ART_DIGEST
   unzip -l user-bootstrap-postgres-verification.zip    # one member: user-bootstrap-postgres-verification.json
   ```

   Keep `CI_RUN`, `ART_ID` and `ART_DIGEST` with the enrollment record; the digest is
   also the `--ci-artifact-digest` argument.

3. SQL pins at the bundle's exact commits, computed through the GitHub API (the host
   later downloads the same files over a different channel and stops on any
   difference):

   ```bash
   HUB_SHA=<services.hub.source_sha>; AGENT_SHA=<services.agent.source_sha>
   pin() {   # repo commit path
     printf '%s  we-meet-trip/map-service-%s@%s:%s\n' \
       "$(gh api -H 'Accept: application/vnd.github.raw+json' "repos/we-meet-trip/map-service-$1/contents/$3?ref=$2" | shasum -a 256 | cut -d' ' -f1)" \
       "$1" "$2" "$3"
   }
   {
     for f in prepare finalize activate quarantine; do pin user "$USER_SHA" "docs/user-database-bootstrap-$f.sql"; done
     pin hub "$HUB_SHA" docs/database-roles.sql
     pin agent "$AGENT_SHA" docs/database-roles.sql
   } > sql-pins.txt
   cat sql-pins.txt      # six lines
   ```

   `git show <commit>:<path> | shasum -a 256` in a clone of each repository gives the
   same values and is a second check.

4. Copy the inputs to the host:

   ```bash
   gcloud compute scp --recurse --tunnel-through-iap --project mapservice-test --zone us-central1-a \
     "map-bundle-$RUN" user-bootstrap-postgres-verification.zip sql-pins.txt map-test:~/
   ```

## Host

In an IAP SSH session on the host (`gcloud compute ssh map-test --project
mapservice-test --zone us-central1-a --tunnel-through-iap`), make root-only copies of
exactly these files; nothing is copied into an existing tree. Then check the bundle
and set `IMAGE_TAG` and `IMAGE_REGISTRY` exactly as a receive rewrites them:

```bash
RUN=<run ID>
sudo install -d -o root -g root -m 0700 /root/map-enroll /root/map-enroll/bundle-$RUN /root/map-enroll/db-inputs
for f in release.json compose.images.yml compose.admin-images.yml SHA256SUMS; do
  sudo install -o root -g root -m 0600 ~/map-bundle-$RUN/$f /root/map-enroll/bundle-$RUN/$f
done
sudo install -o root -g root -m 0600 ~/user-bootstrap-postgres-verification.zip ~/sql-pins.txt /root/map-enroll/db-inputs/
sudo python3 /usr/local/lib/map-deploy/release_manifest.py verify --bundle /root/map-enroll/bundle-$RUN --expected-run-id $RUN --registry-check
sudo python3 -B - /root/map-enroll/bundle-$RUN/release.json /srv/map-test/map-service-infra/.env.test <<'PY'
import importlib.util as u, json, sys
from pathlib import Path
s = u.spec_from_file_location("d", "/usr/local/lib/map-deploy/deploy-gcp.py"); d = u.module_from_spec(s); s.loader.exec_module(d)
data = json.loads(Path(sys.argv[1]).read_text())
d.require_target_source(data, "test")
path = Path(sys.argv[2]); old = path.read_bytes(); new = d.updated_environment(old, data["release_tag"])
if new != old:
    d.replace_environment(path, new, path.stat())
print("IMAGE_TAG", data["release_tag"], "updated" if new != old else "unchanged")
PY
```

Then, from the workstation, with `RUN` and `ART_DIGEST` from above (each phase prints
one value-free JSON line; the full log is `/var/lib/map-db-bootstrap/run.log`):

```bash
SCRIPT=/srv/map-test/map-service-infra/scripts/gcp-test-db-bootstrap.sh
ARGS="--bundle /root/map-enroll/bundle-$RUN --run-id $RUN --ci-artifact /root/map-enroll/db-inputs/user-bootstrap-postgres-verification.zip --ci-artifact-digest $ART_DIGEST --sql-pins /root/map-enroll/db-inputs/sql-pins.txt"
gcloud compute ssh map-test --project mapservice-test --zone us-central1-a --tunnel-through-iap \
  --command "sudo bash $SCRIPT check $ARGS"                                  # {"phase":"P0_preflight","result":"ok"}
gcloud compute ssh map-test --project mapservice-test --zone us-central1-a --tunnel-through-iap \
  --command "sudo bash $SCRIPT run $ARGS --approve-postgis-view-revoke"      # ... "result":"DATABASE_READY_FOR_NORMAL_MIGRATIONS"
gcloud compute ssh map-test --project mapservice-test --zone us-central1-a --tunnel-through-iap \
  --command "sudo bash $SCRIPT verify"                                       # {"phase":"V_verify","result":"ok"}
```

`check` changes no container and no database: it validates the host, the bundle, the
artifact, the SQL pins (it downloads and caches the six files), the secrets and the
Compose project. If the IAP session drops during `run`, the script keeps running (HUP
is ignored); reconnect and read the log and records before doing anything else. `run`,
`quarantine` and `postgis-acl` also take `/var/lib/map-deploy/deploy.lock`, the lock of
the receiver, the backup timers and the watchdog (which holds it only for a moment each
cycle), so they never overlap a receive or a backup. The receiver takes that lock
without waiting: a `deploy-gcp-test` run that arrives meanwhile fails with
`DeploymentBusy` and changes nothing; find it with
`gh run list -R we-meet-trip/map-service-infra --workflow deploy.yml` and dispatch it
again. Enrollment step 12 starts the stack only after `run` printed
`DATABASE_READY_FOR_NORMAL_MIGRATIONS` and `verify` passed; once any other container of
the project exists, `run` refuses to start.

### Phases

| Phase | Action | Re-run behaviour |
|---|---|---|
| P0 | Host, repository, Docker/Compose versions; no project container other than `postgres` (checked before any input or secret is read); bundle paths, files and registry check, `IMAGE_TAG`, CI artifact digest and content, SQL pins, secrets, approval; `inputs.json` pins the inputs | Later runs must use the same bundle |
| P1 | First boot through a root-owned Compose override: `POSTGRES_DB=postgres`, an empty init directory, health probe on `postgres`. Until `map_test` exists, the cluster must hold only `postgres`, `template0`, `template1` and the operator role | The fresh-cluster check repeats on every run until `map_test` exists, also after a first start that stopped after initdb |
| P2 | Marker (32 random bytes, root 0600 file, only its SHA-256 recorded); `CREATE DATABASE map_test TEMPLATE template0`; `COMMENT ON DATABASE` | Skipped when marked |
| P3 | Waits up to 15 s until no other backend (an autovacuum worker included) is on `map_test`; attempt record (HOLD, no retry); one-use 32-byte password in memory; prepare SQL | Never repeated |
| P4 | Exact bundle User image on the isolated internal network `map-test-user-bootstrap`, read-only, uid 10001, no capabilities, 384 MiB, no logs, 120 s inside and 135 s outside; password and marker on stdin; its stderr is discarded and only an exactly shaped result line is accepted | Never repeated |
| P5 | Waits for zero bootstrap sessions; finalize SQL | Never repeated |
| P6 | SCRAM host-rule guard; activate SQL (runtime and migrator LOGIN with the Secret Manager values); TCP SCRAM probes; User receipt | Activation runs only while both roles are still NOLOGIN |
| P7 | Normal Compose configuration for postgres (a populated volume never runs `db/init`); the three `/etc/map-deploy/*-migration.env` files; Hub and Agent digests pulled; User `migrate` and `validate` through `service-migration-job.py`; User catalog baseline | Idempotent |
| P8 | Withdraws a temporary Hub grant left by a killed run; PostGIS in `public` with the view revoke in the same transaction; `00-create-schemas.sql`; Hub then Agent `database-roles.sql`; the `10-admin.sh` SQL; `map_pg_exporter` holding only `pg_monitor`; CONNECT and `public` USAGE grants; Hub, Agent and exporter passwords; postconditions; User guard dry-run; catalog unchanged | Idempotent |
| P9 | Only while `hub_data.alembic_version` is absent and `hub_data` has no relation: write-once window record, `GRANT CREATE ON DATABASE` to `map_hub_owner` with a 20-minute migrator expiry, first Hub migration, revoke (also on any failure), closing record. Then Hub role SQL, Hub migration again without the grant, Hub role SQL, Agent migration, Agent role SQL, admin SQL, User `validate`, catalog unchanged | Idempotent; a window record without its closing record is a HOLD for the owner |
| verify | SCRAM host rules, bootstrap login retired, no temporary Hub grant, postconditions (among them: no schema other than `public` and the four service schemas), User guard dry-run, SCRAM logins of all eight roles from the private `postgres` address, User catalog equal to the bootstrap record | Read-only |

Operator sessions and probe sessions start with `SET search_path = pg_catalog, pg_temp`:
the operator role's default search path names `user_service` and `hub_data`, whose
owners are service roles, so an unqualified call must never reach a function created
there.

## Secret flows

| Secret Manager name | Key | Used for | Path |
|---|---|---|---|
| `test-postgres-password` | `POSTGRES_PASSWORD` | operator `map` | `.env.test` → Compose at first boot; the script only compares it with Secret Manager |
| `test-user-runtime-db-password` | `USER_DATABASE_PASSWORD` | activate SQL `runtime_password` | materialize → `/run/map-db-bootstrap.*/` (tmpfs, 0600) → validated → shell variable (file deleted) → `\set` line on psql stdin |
| `test-user-migration-password` | `USER_MIGRATION_PASSWORD` | activate SQL `migrator_password`; `user-migration.env` | same; env file root 0600 → `service-migration-job.py` `--env-file` (deleted once Docker has read it) |
| `test-hub-database-url` | `HUB_DATABASE_URL` | `map_hub_runtime` password (URL-decoded) | same → `ALTER ROLE … PASSWORD :'var'` from stdin |
| `test-hub-migration-database-url` | `HUB_MIGRATION_DATABASE_URL` | `map_hub_migrator` password; `hub-migration.env` verbatim | same |
| `test-agent-runtime-db-password` | `AGENT_DATABASE_PASSWORD` | `map_agent_runtime` | same |
| `test-agent-checkpoint-migration-dsn` | `AGENT_CHECKPOINT_MIGRATION_DSN` | `map_agent_migrator`; `agent-migration.env` verbatim | same |
| `test-map-admin-password` | `MAP_ADMIN_PASSWORD` | `mapadminpw` of the `10-admin.sh` SQL | same; the body's own `\getenv mapadminpw MAP_ADMIN_PASSWORD` line is dropped, so the value never comes from the psql or `docker exec` environment (the container's copy may be stale) |
| `test-admin-database-url` | `ADMIN_DATABASE_URL` | consistency check only | must carry the `MAP_ADMIN_PASSWORD` value |
| `test-pg-exporter-dsn` | `POSTGRES_EXPORTER_DSN` | `map_pg_exporter` | same |
| generated on the host | bootstrap password | prepare SQL, job stdin | memory only; retired by finalize |
| generated on the host | marker | database comment; prepare, finalize, activate and quarantine SQL; job stdin | `/var/lib/map-db-bootstrap/marker` root 0600, kept on the host; records hold only its SHA-256 |

No value appears in argv, output or `run.log`, in `docker run -e NAME=value`, or under
shell tracing (the script refuses `-x`). The bootstrap job reads its two secrets from
stdin because `-e NAME` copies a value into the container's `Config.Env`. Operator
sessions set `log_error_verbosity=terse`, `log_min_error_statement=panic` and
`log_statement=none`, so a failing statement that carries a literal is not written to
the PostgreSQL log, and psql runs with `VERBOSITY=terse` and `SHOW_CONTEXT=never`.

## Records

`/var/lib/map-db-bootstrap/` (root 0700): `inputs.json`, `user-bootstrap-attempt.json`,
`user-bootstrap-job.json`, `user-bootstrap-failure.json` (written by a quarantine),
`user-bootstrap-receipt.json`, `hub-initial-window.json`,
`hub-initial-window-closed.json`, `db-bootstrap-complete.json` (with the image IDs the
bootstrap ran, `postgres_image_id` included), `receipts/*.json` from
`service-migration-job.py`, `user-catalog.sha256`, `sql/SOURCES` and
`sql/<repo>-<commit>/`, `compose.postgres-bootstrap.yml` (the first-boot Compose
override), the empty `empty-initdb/` it mounts, `bootstrap.lock`, `run.log`, and
`marker`, the only secret, which never leaves the host.

## Verification queries (no values)

```bash
PG=$(sudo docker ps -q --filter label=com.docker.compose.project=map-test --filter label=com.docker.compose.service=postgres)
sudo docker exec -i "$PG" psql -X -A -U map -d map_test <<'SQL'
SET search_path = pg_catalog, pg_temp;
SELECT current_setting('server_version_num')::int >= 170000 AS pg17, pg_get_userbyid(datdba) AS owner,
       shobj_description(oid, 'pg_database') ~ '^map-user-bootstrap:v1:[0-9a-f]{64}$' AS marked
  FROM pg_database WHERE datname = current_database();                         -- t | map | t
SELECT obj_description('user_service'::regnamespace, 'pg_namespace');           -- map-user-bootstrap:v1:finalized
SELECT version, checksum, installed_by, success FROM user_service.flyway_schema_history
 ORDER BY installed_rank LIMIT 4;  -- 001..004, -192854188 -2113231432 1973132559 -777713445, map_user_bootstrap, t
SELECT rolname, rolcanlogin, rolinherit, rolpassword LIKE 'SCRAM-SHA-256$%' AS scram, rolvaliduntil
  FROM pg_authid WHERE rolname ~ '^map' ORDER BY 1;
  -- LOGIN + scram: map_admin, map_pg_exporter (inherit t), map_{user,hub,agent}_{runtime,migrator} (inherit f)
  -- NOLOGIN: map_user_bootstrap (password NULL), map_{user,hub,agent}_owner; map_hub_migrator valid until infinity
SELECT pg_get_userbyid(member), pg_get_userbyid(roleid), inherit_option, set_option
  FROM pg_auth_members WHERE pg_get_userbyid(member) ~ '^map_' ORDER BY 1;
  -- migrators → owners (f, t); map_pg_exporter → pg_monitor (t, t); nothing else
SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace
 WHERE nspname IN ('user_service', 'hub_data', 'langgraph', 'admin_data') ORDER BY 1;
  -- admin_data map_admin, hub_data map_hub_owner, langgraph map_agent_owner, user_service map_user_owner
SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_'
   AND nspname NOT IN ('public', 'information_schema', 'admin_data', 'hub_data', 'langgraph', 'user_service');  -- 0 rows
SELECT rolname FROM pg_roles WHERE rolname ~ '^map_'
   AND (has_database_privilege(oid, current_database(), 'CREATE') OR has_database_privilege(oid, current_database(), 'TEMP'));  -- 0 rows
SELECT rolname FROM pg_roles WHERE rolname ~ '^map_' AND has_schema_privilege(oid, 'public', 'USAGE') ORDER BY 1;
  -- map_admin, map_hub_owner, map_hub_runtime
SELECT has_table_privilege('public', 'public.spatial_ref_sys', 'SELECT'),
       has_table_privilege('public', 'public.geometry_columns', 'SELECT'),
       has_table_privilege('public', 'public.geography_columns', 'SELECT');     -- t | f | f
SELECT NOT EXISTS (SELECT 1 FROM pg_hba_file_rules WHERE error IS NOT NULL OR (type LIKE 'host%' AND auth_method <> 'scram-sha-256'
  AND NOT ((address = '127.0.0.1' AND netmask = '255.255.255.255') OR (address = '::1' AND netmask = 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'))));  -- t
SELECT (SELECT count(*) FROM user_service.flyway_schema_history), (SELECT string_agg(version_num, ',') FROM hub_data.alembic_version),
       (SELECT count(*) FROM langgraph.checkpoint_migrations);
SQL
```

## Failure handling

Every failure prints `{"result":"HOLD","code":…,"automatic_retry":false}` and exits.
Read the code, `run.log` and the records first; never retry blindly.

| Where / code | State left | What to do |
|---|---|---|
| P0, any code | Nothing changed | Fix the reported input and run `check` again. `secrets_rejected` lists problem codes (key names only) in the line before it. `docker_engine_28_required` / `docker_compose_2_24_4_required`: install from Docker's apt repository. `*_must_be_root_0600_single_link`, `bundle_not_root_owned`: recopy with `install -o root -g root -m 0600` into root 0700 directories (every directory above an input must be root-owned and not group/other writable). `repo_file_missing_<file>`: the checkout lacks that file; it is not the reviewed commit. `sql_pin_mismatch_<file>`: the download differs from the workstation's value; recompute the pin at the exact commit, never edit it to match. `ci_artifact_rejected`: wrong zip, digest or commit |
| `deployment_lock_busy` | Nothing changed | A receive or a backup holds the deployment lock; run again when it ends |
| P0 `project_already_has_<service>_container` | Nothing changed by the script | A stack was started (`cloud-up.sh`, enrollment step 12) before the bootstrap finished, or this host already serves. That `cloud-up.sh` may have installed PostGIS without the view revoke and run migrations without the Hub and Agent role SQL. The script never removes containers; owner review |
| `sql_cache_differs_from_pin_<file>` | Nothing changed | A cached file under `/var/lib/map-db-bootstrap/sql/` changed after it was verified; owner review |
| P1 `fresh_cluster_unexpected` | Cluster initialised | Something ran init scripts or created roles or databases; stop, owner decides. The script never deletes a volume |
| P1 `postgres_start_failed` | Volume may be initialised | Run again; the fresh-cluster check repeats until `map_test` exists |
| P2 `unmarked_database_requires_review`, `target_database_not_recognized` | `map_test` foreign or unmarked | Never drop or re-comment it automatically; owner review |
| P3 `target_database_has_other_sessions` | No attempt record | Find the backend (`SELECT pid, usename, application_name, backend_type FROM pg_stat_activity WHERE datname = 'map_test'`), let it end, run again |
| P3, quarantine result `manual_review_prepare_not_applied` | Prepare rolled back; attempt and failure records exist | If `run.log` shows `bootstrap requires exclusive pristine database and new dedicated roles`, another backend (for example an autovacuum worker) attached between the wait and prepare. The owner confirms no `map_user_*` role and no `user_service` schema exist, renames `user-bootstrap-attempt.json` and `user-bootstrap-failure.json` to `*.reviewed-<UTC>`, and only then runs again |
| P3–P5, any other failure or interrupt | The script stopped the exact job, ran the User quarantine SQL, terminated only the inventoried bootstrap sessions and wrote `user-bootstrap-failure.json` | HOLD. Keep the partial history, objects and marker. Do not reset comments, edit migrations, repair history or drop the database or volume as recovery (map-service-user `docs/user-database-bootstrap.md`). Report to the owner |
| Host lost inside P3–P5 (no failure record) | Unknown | `sudo bash /srv/map-test/map-service-infra/scripts/gcp-test-db-bootstrap.sh quarantine`; expect `"result":"confirmed"`. `manual_required` means a check did not hold: owner review |
| P6 `activation_failed` | Bootstrap login retired; runtime and migrator NOLOGIN | The terse error line in `run.log` names the refusal: `bootstrap activation target rejected`, `bootstrap activation role state rejected` (for example a remaining `map_user_*` session, or a role no longer NOLOGIN without a password) or `bootstrap activation privileges rejected`. Its fourth refusal, `independent activation secrets required`, does not occur here: P0 already refuses passwords that are not two different 64-character lowercase hex values. Fix the cause and run again; activation is atomic |
| P7–P9 migration failure | Job removed; the receipt in `receipts/` has `error_code` | Fix the cause and run again. Agent invalid-index failures need operator recovery first (map-service-agent `docs/database-roles.md`) |
| Run killed inside the first Hub window, or the first Hub migration failed | A killed run may leave the grant; the migrator login then expires 20 minutes after the window opened | The next `run` withdraws the grant at the start of P8 (`hub_stale_window_revoked`) and holds at P9 with `hub_initial_window_unfinished_owner_review`. The owner checks that `has_database_privilege('map_hub_owner','map_test','CREATE')` is `f` and that `map_hub_owner` owns no schema other than `hub_data`. With `hub_data.alembic_version` present, rename `hub-initial-window.json` to `*.reviewed-<UTC>` and run again (no new window). Without it, `hub_data` must have no relation; rename and run again (one more window). Manual withdrawal, if ever needed: `REVOKE CREATE ON DATABASE map_test FROM map_hub_owner; ALTER ROLE map_hub_migrator VALID UNTIL 'infinity';` |
| P9 `hub_data_not_empty_for_initial_window` | No grant given | `hub_data` holds relations without Alembic history; owner review |
| P8/P9 `user_catalog_changed_by_provisioning` | Provisioning finished but the User contract changed | Stop; owner review |
| verify `hub_temporary_create_present` | A temporary Hub grant remains | Before step 12, run `run` again (P8 withdraws it); once other containers exist `run` refuses to start, so withdraw it manually as above |
| verify `user_catalog_differs_from_bootstrap_record` | Every earlier check passed | Expected after a newer User release added migrations or after a User password rotation; otherwise owner review |

For this test environment only (synthetic data), the owner may decide to discard the
volume `map-test_postgres-data` and `/var/lib/map-db-bootstrap` and start over with a
new marker. That is an owner decision, never part of the script.

## Maintenance: PostGIS view grants

`ALTER EXTENSION postgis UPDATE` may run the extension's grant statements again and
restore PUBLIC SELECT on the two views; the User guards would then stop the next User
`migrate`, `validate` or container start with `database_privilege_cross_schema_data`.
`postgis-acl` re-applies the approved revoke while the stack runs, under the deployment
lock, and proves the result with the provisioning postconditions and the User guard
dry-run. It changes no container and needs no secret. A `deploy-gcp-test` run that
arrives while it holds the lock fails with `DeploymentBusy` without changes; dispatch it
again:

```bash
gcloud compute ssh map-test --project mapservice-test --zone us-central1-a --tunnel-through-iap \
  --command "sudo bash $SCRIPT postgis-acl --approve-postgis-view-revoke"   # {"phase":"M_postgis_acl","result":"ok"}
```

Use the script of the commit the host is checked out at. Without the script, the same
change is `REVOKE SELECT ON TABLE public.geometry_columns, public.geography_columns FROM PUBLIC;`
as the operator, followed by the view rows of the verification queries above.

## Maintenance: Hub and Agent role SQL

Hub's and Agent's `docs/database-roles.sql` keep new objects closed to the runtime
roles until they run again after a migration. `cloud-up.sh` never applies them, and
`run` refuses to start once other containers exist, so a later release that brings Hub
or Agent migrations (for example the newer release of enrollment step 12) needs them
applied by hand right after its migrations, under the deployment lock. On the
workstation, compute the pin of that file at the release's exact commit with `pin` from
[Workstation](#workstation-owners-github-login-read-only) step 3. On the host, download
the same file over the other channel and apply it only when it matches:

```bash
REPO_NAME=hub; COMMIT=<services.hub.source_sha>; PIN=<the workstation's SHA-256 of that file>   # or agent
F=/root/map-enroll/db-inputs/$REPO_NAME-database-roles-$COMMIT.sql
sudo curl -fsS --proto =https --tlsv1.2 -o "$F" \
  "https://raw.githubusercontent.com/we-meet-trip/map-service-$REPO_NAME/$COMMIT/docs/database-roles.sql"
echo "$PIN  $F" | sudo sha256sum -c -               # "<file>: OK"; on anything else stop here
PG=$(sudo docker ps -q --filter label=com.docker.compose.project=map-test --filter label=com.docker.compose.service=postgres)
{ echo 'SET search_path = pg_catalog, pg_temp;'; sudo cat "$F"; } |
  sudo flock -w 15 /var/lib/map-deploy/deploy.lock docker exec -i "$PG" psql -X -q -v ON_ERROR_STOP=1 -U map -d map_test
sudo bash /srv/map-test/map-service-infra/scripts/gcp-test-db-bootstrap.sh verify
```

A `verify` after a release that added User migrations ends with
`user_catalog_differs_from_bootstrap_record`, as listed under failure handling.

## Residual limits

- `cloud-up.sh` does not re-apply Hub or Agent role SQL after migrations: a later
  release that adds Hub or Agent tables leaves the runtime without grants until the
  SQL is applied again ([Maintenance: Hub and Agent role SQL](#maintenance-hub-and-agent-role-sql)).
- The `postgis/postgis:17-3.5` tag is not pinned by digest; the image ID is printed in
  the P1 `fresh_cluster` line and kept as `postgres_image_id` in
  `db-bootstrap-complete.json`, so a later move of the tag can be compared with it.
- The `search_path` pin covers this script's sessions only. P8 applies
  `db/init/00-create-schemas.sql`, which sets the operator role's default `search_path`
  to `user_service, hub_data, public`, and from enrollment step 12 on every
  `cloud-up.sh` run (every receive) applies that file and `db/init/10-admin.sh` as that
  superuser without a pinned path. A function such as `format(text, name)` that a
  service owner created in `user_service` or `hub_data` matches the file's
  `format(…, current_user)` call better than `pg_catalog.format(text, VARIADIC "any")`
  and would run as superuser. A later release should run both files with
  `PGOPTIONS='-c search_path=pg_catalog,pg_temp'` or call `pg_catalog.format` in
  `00-create-schemas.sql`.
