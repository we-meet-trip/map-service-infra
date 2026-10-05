# Public cutover crash recovery

This is a candidate host installation, not evidence that the GCP supervisor is
installed. Keep the reviewed work branch until user acceptance; no develop/master
merge is authorized by these tests.

## Choice and boundaries

Three approaches were compared. A boot service which calls `docker stop` only
after Docker starts leaves an automatic-restart exposure window. Early IPv4/IPv6
firewall gating can close that window, but filter failure must not block the whole
Docker daemon and its PostgreSQL/Redis/OSRM services. That approach was rejected.
The selected approach permanently uses Docker `restart: 'no'` for **edge, proxy,
user, yolo only**, with a separate systemd supervisor owning their restart policy.
No firewall, Docker daemon configuration, database volume or other restart policy
is changed. The service orders AFTER Docker and has no Requires/BindsTo/ExecStop
which could stop or prevent database/OSRM/monitoring startup.

Docker documents `no` as disabling automatic restarts and warns against combining
Docker restart policies with an external process manager; those two owners are
therefore not used for the same four containers. Other services retain their
existing Docker policy. [Docker restart policy and process manager guidance](https://docs.docker.com/engine/containers/start-containers-automatically/)

## Receiver and durable state

Install `cutover_watchdog.py`, `deploy-gcp.py`, `release_manifest.py` together under
the fixed root-owned `/usr/local/lib/map-deploy`; `backup_job.py` beside them reads the
host target through `deploy-gcp.py`. The existing mandatory six-image
rollback policy remains outside Git and is neither expanded nor promoted here.

`public-restart.yml` is a root-owned, exact-byte four-service restart override.
Both receiver Compose calls and the new `cloud-up.sh` append it last. Test-host
manual cloud-up also validates/applies it whenever the host file exists. Receiver
preflight checks the final four restart values, rejects old cloud-up code without
the supervisor contract marker, and checks actual container policies after private
start and again before publishing readiness. Source checkout cannot remove this
host file. Raw operator `docker compose` bypasses must not be used on this host.

After private AND public smoke, `security-public-ready.json` records the exact
approved six-image tuple and public container IDs/image configuration IDs. It is
fsynced before the terminal cutover latch. Creation independently resolves local
registry digests and compares all six actual application image IDs; a phase named
`complete` is insufficient. Rollback readiness additionally requires a tuple in
`rollback_verified` and a new smoke. Pending attempts never authorize a restart.
Detached-admin six-image enrollment is not implemented by this local-only receipt;
that future topology needs a separately reviewed cross-host receipt contract.

The fixed SSH command becomes `receive-supervised.sh` with no caller arguments.
It runs the receiver as the MainPID of `map-deploy-receive.service`, a transient
systemd unit with `KillMode=control-group`, `SendSIGKILL=yes`, stop grace10s and
runtime maximum90min. The Python receiver requires that cgroup; direct old SSH
commands fail before checkout or mutation. systemd must reap remaining children,
including a child with its own process session, when the main receiver dies. The
watchdog skips an active/activating/deactivating receiver unit as well as a held
deploy lock, so it does not race cleanup with orphan Compose processes.

## Watchdog, boot and failure behavior

The root service polls every2s, validates the GCP instance named by `target.json` once
(restart it after changing that file), and obtains
the same root-owned, no-symlink `deploy.lock` before reading the latch. It never
uses a stale timestamp to declare a live deployment dead. A healthy deployment can
hold the lock while backups/pulls/smoke execute; the watchdog performs no mutation.
The transient unit runtime deadline bounds a hung receiver separately.

For a valid nonterminal latch, it stops all four public services independently and
verifies none remain running. It repeats safely on subsequent polls. It does not
roll back source/env/images or start the prior release. For a valid terminal latch,
it requires a matching ready receipt, still-approved tuple, exact public IDs/images
and restart=no. It starts only stopped members of that same receipt, in order
yolo→user→proxy→edge; a stopped edge waits for real private health/readiness first.
It does not pull/create/recreate containers, read environment/backup keys, or emit
raw process output/metadata. A missing container or changed identity requires a
reviewed receiver deployment rather than guessing a replacement.

The 2s poll interval is not a 2s closure guarantee: systemd child cleanup (up to10s),
sequential Docker graceful stops (30s each, command timeout90s) and verification
also consume time. The fixture records its measured kill-to-four-stop duration;
real-host long-lived socket closure latency must be measured separately.

An already running completed release is not stopped because of a failed supervisor
read, stale receipt, missing policy, unavailable health endpoint or service failure.
Those errors cannot authorize any start either. After a boot, the four restart=no
containers consequently remain closed until validation succeeds; other containers
start according to their unchanged policies. Invalid/corrupt latch metadata is
reported as retry/no-unverified-start rather than treated as proof that healthy
running serving should be stopped. A verified pending latch still permits four-stop
even if its rollback policy is missing.

Stopping the watchdog itself has no ExecStop affecting containers. Already running
serving continues; public crash/boot recovery is unavailable until it returns.
This availability dependency needs an independent alert. Normal public crashes are
retried every poll; repeated crashes can remain degraded without touching data or
unrelated services. These are not full availability/RPO/RTO guarantees.

## Staged installation and explicit maintenance

Actual GCP installation is root-coordinated, after code/CI review and a separate
isolated Linux fault check. Do not run a Docker/VM crash on the data-bearing host.

1. Under the shared deployment lock, stage checksummed root-owned scripts/unit and
   preserve existing receiver/policy copies. Do not point SSH to a half-installed
   set. Do not delete or rewrite the root rollback policy. Release the lock before
   invoking `cutover_watchdog.py enroll`, which takes it itself.
2. Enrollment requires an existing valid complete/verified-rolled-back latch, exact
   allowed six-image identities, all four running and actual private/public smoke.
   It writes the fixed override atomically, changes restart policy on only those
   four exact existing container IDs, and verifies/captures the ready receipt.
   A failure is not enrollment success. No application is stopped by enrollment.
3. Install `map-cutover-watchdog.service`, daemon-reload and enable/start that unit.
   Verify its healthy pass starts/stops no container. Change the forced SSH command
   to the fixed root-owned wrapper only after enrollment and watchdog readiness.
   A direct new receiver requires the active watchdog and host override.
4. Preserve ready/latch/policy files and all data. There is no automatic rollback to
   the old unguarded receiver. If supervision fails, a completed running release is
   left serving; repair/roll forward the supervisor. While quarantined, repair and
   deploy a policy-allowed candidate. Do not fake `complete` or remove the latch.

For an intentional public maintenance stop, acquire deploy.lock and atomically
write root-owned `/var/lib/map-deploy/public-maintenance.json` with exactly
`{"schema_version":1,"instance_id":"<instance_id of target.json>","hold":true}`. The next
watchdog scan stops only the public four; DB/Redis/OSRM/observability remain up.
Normal `docker stop` without that hold is treated as a crash and may be restarted.
To resume, inspect the unchanged completed latch/approved receipt, acquire the same
lock, remove only this explicit maintenance marker, fsync the parent, then let the
watchdog revalidate and start. A pending cutover latch continues quarantine even
after maintenance removal. The receiver rejects deployment while maintenance is
present, before source checkout or mutation; it cannot bypass the hold.

## New test host enrollment (one time)

Root-owned `/etc/map-deploy/target.json` names the machine the receiver, watchdog and
backup timers act on. `verify_instance` loads it before every receive, watchdog start
and enrollment and compares project, name, numeric ID and zone with the machine's
metadata; the backup timers take their checkout from it. Without the file a process is
bound to the original test host (`mapcenter-b59ca`, `us-central1-a`, `map-test`,
`2327348931395410137`, `/home/mapadmin26/map-service-infra`), which keeps working
unchanged until it is retired; any other machine without the file fails the metadata
comparison before any lock, checkout or container change. A file that is present but
is not a root-owned regular file of mode 0600/0644 with exactly the keys and value
formats below is refused, never replaced by that fallback.

The values below are those of the GCP test project: `mapservice-test`,
`us-central1-a`, VM `map-test`, checkout `/srv/map-test/map-service-infra`, public
origin `https://test-api.mapservice.app` (a Cloudflare DNS-only A record for the
reserved address `map-test-ip`; not proxied, because the edge overwrites
`CF-Connecting-IP` with the address it observes). Docker Engine 28.1.0 or later must
already be installed: the migration jobs create networks with an isolated IPv4
gateway, and the Caddy artifact checks of steps 10 and 12 run
`docker image inspect --platform` (API 1.49). This host takes the five Docker packages
from Docker's apt repository at fixed versions, held with `apt-mark hold`: `docker-ce`
and `docker-ce-cli` `5:29.8.2-1~ubuntu.24.04~noble`, `containerd.io`
`2.3.6-1~ubuntu.24.04~noble`, `docker-buildx-plugin` `0.37.1-1~ubuntu.24.04~noble` and
`docker-compose-plugin` `5.5.1-1~ubuntu.24.04~noble`. Engine 29 uses the containerd
image store on a new installation; this host keeps the classic store, so
`/etc/docker/daemon.json` holds `{"features": {"containerd-snapshotter": false}}`
before `dockerd` first starts (a later switch hides the images and containers already
there). `google-cloud-cli` must come from Google's apt repository, so that
`/usr/bin/gcloud` exists: `scripts/gcp_secrets.py` (steps 5 and 9) runs that path
first, and the backup jobs run `gcloud` from `PATH`, where `/usr/bin` precedes the
image's `/snap/bin`. Run every host step as root, in this order.

1. Create `/usr/local/lib/map-deploy` (`root:root`, 0755) and install these nine files
   from the reviewed pushed commit, root-owned and not writable by group/other
   (`receive-supervised.sh` 0755, the rest 0644), recording their SHA-256:
   `deploy-gcp.py`, `release_manifest.py`, `cutover_watchdog.py`,
   `receive-supervised.sh`, `backup_job.py`, `backup-test-service.py`,
   `backup-test-redis-service.py`, `pg_backup.py`, `redis_backup.py`. The receiver,
   the watchdog, the backup timers and the `verify-override` call in `cloud-up.sh`
   load these installed copies.
2. `install -d -o root -g root -m 0755 /etc/map-deploy`, then write `target.json`
   (`root:root`, 0644 or 0600, not a link):
   ```json
   {"project": "mapservice-test", "zone": "us-central1-a", "instance": "map-test",
    "instance_id": "<numeric instance id as a string>",
    "public_url": "https://test-api.mapservice.app",
    "repo": "/srv/map-test/map-service-infra", "dns_profile": false}
   ```
   `instance_id` is the VM's `computeMetadata/v1/instance/id`. `public_url` is a bare
   https origin (no path, port or trailing slash); probes append their own paths.
   With `dns_profile: false` the receiver never renders, pins or keeps the dynamic DNS
   updater. Confirm the binding with the installed receiver:
   ```bash
   sudo python3 -c 'import importlib.util as u; s = u.spec_from_file_location("d", "/usr/local/lib/map-deploy/deploy-gcp.py"); d = u.module_from_spec(s); s.loader.exec_module(d); d.verify_instance(); print(d.INSTANCE, d.INSTANCE_ID, d.REPO, d.DNS_PROFILE)'
   ```
3. Clone this repository into `repo` (root-owned, since the receiver runs Git there
   as root) at the reviewed pushed `feature-gcp-platform` HEAD and leave it clean. The
   first accepted release checks out its own `infra_sha` detached (a `develop`
   release); this HEAD is only the restore point before that.
4. `/etc/map-deploy/backup.env` (root, 0600):
   ```text
   BACKUP_DIR=<root-only local directory>
   BACKUP_REMOTE=gs://map-test-backups/test
   ```
   Redis copies go to `<BACKUP_REMOTE>/redis-v1` and are accepted only under
   `/test/redis-v1`, so the remote ends with the environment name. There is no
   credentials file: the VM service account uploads and re-reads every object, which
   needs the cloud-platform scope and object create/read on that bucket. The timers
   refuse `RETAIN_DAYS`.
5. In `repo`, run `scripts/make-test-env.sh`; its output is a local fixture, so correct
   `.env.test` before use:
   - `PLACES_STUB_MODE=false` (receiver preflight refuses stub mode);
   - `HUB_DATABASE_URL` with the `map_hub_runtime` login (the script writes the `map`
     account, which the Hub migration contract refuses), merged from the secret
     `test-hub-database-url` below rather than edited by hand;
   - `POSTGRES_EXPORTER_DSN` with the `map_pg_exporter` login and database `map_test`
     (the example value names database `map`), merged from `test-pg-exporter-dsn`;
     step 9 sets both logins' passwords from these secrets and refuses a `.env.test`
     that differs from them;
   - runtime passwords and internal tokens from Secret Manager: on the VM, whose service
     account can read the secrets listed in the Terraform variable `runtime_secret_ids`,
     `python3 scripts/gcp_secrets.py materialize --project mapservice-test --output <root-only file> KEY=SECRET_NAME...`
     writes them to a 0600 file and refuses any value outside `[A-Za-z0-9._~+/=:@?&%,-]`;
     merge those keys into `.env.test` without displaying them, then delete the file.
     A secret without an enabled version fails the whole call and no file is written,
     so leave such secrets out and keep their keys empty (on 2026-10-05 those of
     `TOUR_API_SERVICE_KEY`, `ODSAY_API_KEY_FALLBACK`, `SEOUL_OPENAPI_KEY` and
     `PM_SERVICE_KEY`). Merge serving values only: the migration credentials
     (`USER_MIGRATION_PASSWORD`, `HUB_MIGRATION_DATABASE_URL`,
     `AGENT_CHECKPOINT_MIGRATION_DSN`) go only to `/etc/map-deploy/*-migration.env`,
     which step 9 writes, and step 9 refuses a `.env.test` that holds them.
     `test-user-runtime-db-password` and `test-user-migration-password` must hold two
     different 64-character lowercase hex values, the only form User's activation SQL
     accepts;
   - `EDGE_DOMAIN=test-api.mapservice.app` and `EDGE_EMAIL`; `DUCKDNS_SUBDOMAIN` and
     `DUCKDNS_TOKEN` stay empty;
   - `APPLE_ENABLED=false`, which neither `.env.example` nor the script writes, and the
     line `KAKAO_APP_ID=` kept with an empty value (without the line User falls back to
     its built-in Kakao app ID), so this environment never accepts real Apple or Kakao
     sign-in;
   - `GEMINI_MODEL=gemini-3.5-flash-lite` and `GEMINI_THINKING_BUDGET=-1` (that model
     rejects 0 with HTTP 400);
   - external API keys (Places, Gemini) reach Secret Manager before that materialize:
     the owner pipes each value from the old test host's `.env.test` into
     `python3 scripts/gcp_secrets.py put --project mapservice-test <SECRET_NAME>`,
     which reads it only from stdin, so it is never printed.
6. `install -d -o root -g root -m 0700 /var/lib/map-deploy`.
7. Before anything starts, write `public-restart.yml` from the installed
   `cutover_watchdog.OVERRIDE` bytes as a new root file of mode 0600, never a link.
   `enroll` needs a completed latch, so a new host cannot use it.
   ```bash
   sudo python3 -c 'import importlib.util as u, os; s = u.spec_from_file_location("g", "/usr/local/lib/map-deploy/cutover_watchdog.py"); g = u.module_from_spec(s); s.loader.exec_module(g); fd = os.open("/var/lib/map-deploy/public-restart.yml", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600); os.write(fd, g.OVERRIDE.encode()); os.fsync(fd); os.close(fd)'
   sudo /usr/bin/python3 /usr/local/lib/map-deploy/cutover_watchdog.py verify-override  # {"phase": "override_verified"}
   sudo stat -c '%U %a %h %s' /var/lib/map-deploy/public-restart.yml                     # root 600 1 115
   ```
   `verify-override` checks the exact bytes, root ownership and mode 0600/0644 but not
   the link count, so `stat` confirms nlink 1 and mode 600. If the stack starts before
   this file exists, the public four are created with Docker restart `unless-stopped`;
   the first receive then fails its restart check after writing the latch and
   quarantines them.
8. Install `deploy/map-cutover-watchdog.service` in `/etc/systemd/system`, run
   `systemctl daemon-reload` and `systemctl enable --now` it. Without a latch it changes nothing and retries, and
   `active` alone does not show that the identity check passed:
   ```bash
   systemctl is-active map-cutover-watchdog.service              # active
   sudo journalctl -u map-cutover-watchdog.service -o cat -n 1   # {"phase": "guard_retry_cutover_latch"}
   ```
   The watchdog writes a line only when its phase changes, so right after the start the
   last journal line can still be systemd's `Started …` line; repeat the `journalctl`
   call until it shows a phase. `guard_retry_instance_verification` means `target.json`
   or the machine identity is wrong. After Docker is installed, also install and enable
   `deploy/gcp/map-metadata-block.service`: it drops container traffic to
   `169.254.169.254` except DNS on port 53; host processes are unaffected.
9. Bootstrap the empty database with
   [`scripts/gcp-test-db-bootstrap.sh`](../scripts/gcp-test-db-bootstrap.sh) before
   anything else uses PostgreSQL; [GCP_TEST_DB_BOOTSTRAP.md](GCP_TEST_DB_BOOTSTRAP.md)
   has the inputs, phases, secret flows, verification queries and recovery. Do not
   start PostgreSQL with plain Compose here: its first boot would run `db/init`
   (PostGIS, four schemas, `map_admin`), and the User empty-host bootstrap
   (map-service-user `docs/user-database-bootstrap.md`) refuses a database that already
   holds an extension other than plpgsql, a user schema, another backend or a
   `map_user_*` role. The bootstrap runs the exact User image that step 12 starts, so
   the release is prepared here and step 12 reuses it.
   - Host packages: the pinned Docker packages and `google-cloud-cli` named above; the
     script stops with `docker_engine_28_required` below Engine 28 and with
     `docker_compose_2_24_4_required` below Compose 2.24.4.
   - Owner inputs: approval to withdraw PUBLIC SELECT on the PostGIS views
     `public.geometry_columns` and `public.geography_columns` (PostGIS grants it, while
     the User runtime and migrator guards accept only `public.spatial_ref_sys`, so
     `migrate`, `validate` and User serving otherwise stop with
     `database_privilege_cross_schema_data`), and `test-user-runtime-db-password` and
     `test-user-migration-password` as two different 64-character lowercase hex values
     (the User activation SQL refuses anything else; `gcp_secrets.py generate` makes
     url-safe tokens).
   - On a workstation with the owner's GitHub login: choose the `develop`
     `image-release` run and run
     `GH_TOKEN="$(gh auth token)" python3 scripts/deploy-gcp.py prepare --run-id <run ID> --output map-bundle-<run ID> --target-environment test`
     (the output directory must not exist yet);
     download the `user-bootstrap-postgres-verification` artifact of the bundle's
     `services.user.source_sha` (a `push` run of `ci` with conclusion `success`) as the
     zip GitHub serves, with
     `gh api repos/we-meet-trip/map-service-user/actions/artifacts/<id>/zip`, and compare
     its SHA-256 with the artifact's `digest`. GitHub keeps it 14 days: download it
     before its `expires_at` (a zip kept from earlier whose SHA-256 equals the recorded
     `digest` stays valid). After that the owner re-runs that CI run, possible up to 30
     days after it, for a new artifact, or chooses a newer release whose User commit has
     fresh evidence (`image-release` must be enabled first, see step 14). Compute the
     SHA-256 of the six SQL files (User's four `docs/user-database-bootstrap-*.sql`, Hub's
     and Agent's `docs/database-roles.sql`) at the bundle's exact commits into a pins
     file with one `<sha256>  we-meet-trip/map-service-<repo>@<commit>:<path>` line each.
   - On the host, after `gcloud compute scp --recurse --tunnel-through-iap` to the
     operator's home: install the four bundle files, the zip and the pins file with
     `install -o root -g root -m 0600` into new root 0700 directories. The script
     refuses an input path with any component, from `/` down to the input itself, that
     is not root-owned, is a link or is writable by group or others, and bundle files,
     a zip, a pins file or a `.env.test` that is not root 0600 with a single link, so
     nothing can replace an input after it was checked. Check the bundle with
     `sudo python3 /usr/local/lib/map-deploy/release_manifest.py verify --bundle <bundle> --expected-run-id <run ID> --registry-check`
     (the registry check ties each image digest to its source commit and release tag;
     the script repeats it) and set `IMAGE_TAG` and `IMAGE_REGISTRY` in `.env.test` from
     its `release.json` the way a receive rewrites them.

   Then run the script from the checkout as root: `check` changes no container and no
   database, `verify` is read-only.
   ```bash
   S=/srv/map-test/map-service-infra/scripts/gcp-test-db-bootstrap.sh
   A="--bundle <bundle> --run-id <run ID> --ci-artifact <zip> --ci-artifact-digest <sha256:…> --sql-pins <pins file>"
   sudo bash $S check $A                                # {"phase":"P0_preflight","result":"ok"}
   sudo bash $S run $A --approve-postgis-view-revoke    # … "result":"DATABASE_READY_FOR_NORMAL_MIGRATIONS"
   sudo bash $S verify                                  # {"phase":"V_verify","result":"ok"}
   ```
   It boots the cluster once with no init script and no application database, creates
   `map_test` from `template0` with an independent marker, and runs the User prepare
   SQL, one `UserBootstrapApplication` run of the bundle's User digest on an isolated
   internal network, the finalize SQL and the activation SQL. It then switches
   PostgreSQL to the normal Compose configuration (a populated volume never runs
   `db/init`), writes the three `/etc/map-deploy/{user,hub,agent}-migration.env` files
   as in [SERVICE_MIGRATION_DEPLOYMENT.md](SERVICE_MIGRATION_DEPLOYMENT.md) and runs
   User `migrate` and `validate`; installs PostGIS in `public` with the view revoke,
   the `db/init` SQL, Hub's and Agent's role SQL, `map_admin`, the exporter login
   `map_pg_exporter` holding only `pg_monitor` and every login password from Secret
   Manager; runs the first Hub migration under a temporary `CREATE ON DATABASE` that is
   always revoked, Hub again without it, then Agent, with the role SQL after each; and
   proves every login over SCRAM. Step 12's migration jobs then have nothing left to
   apply. A failure is a HOLD and is never retried automatically. Inside the one-use
   User bootstrap the script stops the exact job, applies the User quarantine SQL and
   terminates only that role's sessions; keep the partial history, objects and marker,
   and do not reset comments, repair history or drop the database as recovery. If the
   host was lost there, run the script with `quarantine`. `run` holds the deployment
   lock; records and the log are in `/var/lib/map-db-bootstrap`; no secret value is
   printed or logged.
10. With the owner's GitHub login, download the two edge assets of the draft release
    and compare their SHA-256 with `archive_sha256` and `report_sha256` in
    [install-artifact-20260906.json](../docker/caddy-security/install-artifact-20260906.json).
    The release's own asset of that name is an older version; the repository file is
    the anchor.
    ```bash
    gh release download caddy-security-20260906-v1 -R we-meet-trip/map-service-infra \
      -p map-caddy-security-image-20260906.tar -p caddy-security-20260906.json
    ```
    Copy both with `gcloud compute scp --tunnel-through-iap`. Files in the operator's
    home can still change after a root tool has checked them, so the installer reads
    only root-only copies; in `repo`:
    ```bash
    sudo install -d -o root -g root -m 0700 /root/map-enroll /root/map-enroll/caddy
    sudo install -o root -g root -m 0600 ~/map-caddy-security-image-20260906.tar ~/caddy-security-20260906.json /root/map-enroll/caddy/
    sudo python3 scripts/install-caddy-artifact.py \
      --archive /root/map-enroll/caddy/map-caddy-security-image-20260906.tar \
      --report /root/map-enroll/caddy/caddy-security-20260906.json \
      --install-compose /etc/map-deploy/caddy-verified.yml
    ```
11. In Cloudflare, create the DNS-only (not proxied) A record `test-api.mapservice.app`
    for the address of `map-test-ip`, which
    `gcloud compute addresses describe map-test-ip --region us-central1 --project mapservice-test --format='value(address)'`
    prints, and confirm through public resolvers that
    `dig +short test-api.mapservice.app @1.1.1.1` and
    `dig +short test-api.mapservice.app @8.8.8.8` each return exactly that address and
    that `dig +short AAAA test-api.mapservice.app @1.1.1.1` returns nothing (the VM has
    IPv4 only). Caddy requests its certificate when edge starts in the next step, and the
    first receive checks public readiness through `public_url`; a missing or proxied
    record fails that check after ingress opens, and a new host has no verified rollback,
    so it ends quarantined.
12. Start the first stack from the release bundle of step 9: the root-only `<bundle>`
    copy that the database bootstrap checked and used, whose `release_tag` is already
    `IMAGE_TAG` in `.env.test` (`docker-compose.registry.yml` still interpolates it;
    every receive rewrites it). Do not prepare a different release for the first start.
    Start only after step 9's `run` printed `DATABASE_READY_FOR_NORMAL_MIGRATIONS` (it
    then wrote the root 0600 `/var/lib/map-db-bootstrap/db-bootstrap-complete.json`,
    whose `release_tag` is this bundle's) and `verify` printed `ok`; run `verify` again
    right before this step. The `*-migration.env` files and a healthy `postgres` exist
    from the bootstrap's P7 on, so they do not show that it finished. `cloud-up.sh` on a
    held bootstrap creates `redis` and the other containers, after which the bootstrap
    refuses to run (`project_already_has_<service>_container`), while `cloud-up.sh`
    itself never applies the Hub and Agent role SQL and, if the bootstrap's P8 never
    ran, installs PostGIS without the view revoke. Tag images cannot be used: the
    migration jobs accept only
    `ghcr.io/we-meet-trip/map-service-<service>@sha256:…` and refuse a tag with
    `serving_image_not_pinned`, so no application starts. `cloud-up.sh` takes only image
    digests from the bundle and its Compose files from `repo`, so a `develop` release
    from before the merge in step 14 works here. With `public-restart.yml` in place and
    no release received yet, start the initial stack with the role flags the receiver
    uses, under the deployment lock (the receiver takes it without waiting, so a receive
    that arrives meanwhile fails with `DeploymentBusy` and changes nothing):
    ```bash
    cd /srv/map-test/map-service-infra
    sudo test ! -e /var/lib/map-deploy/security-cutover.json &&
      sudo flock -o -w 10 /var/lib/map-deploy/deploy.lock env RELEASE_BUNDLE=<bundle> \
        EDGE_IMAGE_OVERRIDE=/etc/map-deploy/caddy-verified.yml \
        ./scripts/cloud-up.sh --test --registry --vision --edge --admin --monitoring
    ```
    There is no `--dns` and no `docker pull caddy`: the override pins the loaded image
    with `pull_policy: never`. Root is required because the migration jobs accept only
    migration files owned by the invoking user. The first receive needs existing
    `postgres`, `redis` and `edge` containers and a running `user`; it never creates
    them. `edge`, `proxy`, `user` and `yolo` must show restart policy `no`:
    ```bash
    sudo docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}} {{.HostConfig.RestartPolicy.Name}}' \
      $(sudo docker ps -aq --filter label=com.docker.compose.project=map-test)
    ```
    Until the first release, a reboot leaves those four stopped (restart `no` and no
    latch); start them again with the same locked command, so keep `<bundle>` in its
    root-only directory until then. This manual `cloud-up.sh` takes its pre-migration
    backup without `/etc/map-deploy/backup.env`: the dump stays in `/root/backups/test`
    and is not uploaded; remote copies start with the first receive and the backup
    timers. The first receive compares the single Alembic head of the running `admin`
    with that of the release it receives and refuses with
    `admin migration change requires verified rollback compatibility` when they differ.
    If map-service-admin `develop` gains a migration after this release, the owner
    enables `image-release` (see step 14) to build a newer `develop` release; prepare,
    copy and check it as in step 9 (the bootstrap itself does not run again), set its tag
    in `.env.test` and repeat this step with it before step 14. If that release also
    carries Hub or Agent migrations, apply its Hub and Agent `docs/database-roles.sql`
    right after this step's `cloud-up.sh`, as in
    [GCP_TEST_DB_BOOTSTRAP.md](GCP_TEST_DB_BOOTSTRAP.md#maintenance-hub-and-agent-role-sql),
    and run `verify` again: `cloud-up.sh` never applies them, and new tables stay closed
    to the runtime roles until then.
13. Create a new SSH key pair for this host only and the local `mapdeploy` account that
    key logs in to. sshd runs a forced command through the account's login shell (with
    `nologin` every deploy would exit 1), and StrictModes refuses a `~/.ssh` that group
    or others can write:
    ```bash
    sudo useradd --create-home --shell /bin/sh mapdeploy
    sudo install -d -o mapdeploy -g mapdeploy -m 0700 /home/mapdeploy/.ssh
    ```
    The account gets the public key as a forced command, and sudo lets it run only the
    wrapper, which needs EUID 0 and no arguments:
    ```text
    # /home/mapdeploy/.ssh/authorized_keys (owner mapdeploy, 0600)
    restrict,command="/usr/bin/sudo -n /usr/local/lib/map-deploy/receive-supervised.sh" ssh-ed25519 AAAA... map-test-deploy
    # /etc/sudoers.d/map-deploy-receiver (root, 0440, checked with visudo -cf)
    mapdeploy ALL=(root) NOPASSWD: /usr/local/lib/map-deploy/receive-supervised.sh ""
    ```
    OS Login adds an `AuthorizedKeysCommand` but keeps `AuthorizedKeysFile`; confirm
    that `sudo sshd -T -C user=mapdeploy,host=probe,addr=127.0.0.1` lists both
    `authorizedkeysfile` and `authorizedkeyscommand`. If the local key is still not
    accepted, pin `AuthorizedKeysFile` in a `Match User mapdeploy` block. Build the host
    key line `map-test-deploy <type> <key>` from the VM's
    `/etc/ssh/ssh_host_ed25519_key.pub` read over the IAP session. That session trusted
    the key on first use, so compare it over the Compute API with the ED25519
    fingerprint cloud-init printed when it generated the key at first boot:
    ```bash
    gcloud compute instances get-serial-port-output map-test --project mapservice-test --zone us-central1-a --port 1 \
      | grep -A8 'Generating public/private ed25519 key pair' | grep -o 'SHA256:[A-Za-z0-9+/]*' | sort -u
    ssh-keygen -lf <known_hosts>   # must show that same SHA256 value
    ```
    The serial log keeps only recent output; when it no longer holds the fingerprint,
    pinning the key read over IAP is an explicit owner decision. Then submit the
    rejected constant `{}` once with the new key:
    ```bash
    printf '{}' | ssh -T -i <key> -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
      -o UserKnownHostsFile=<known_hosts> -o HostKeyAlias=map-test-deploy \
      -o 'ProxyCommand=gcloud compute start-iap-tunnel map-test 22 --listen-on-stdin --project=mapservice-test --zone=us-central1-a --verbosity=error' \
      mapdeploy@map-test
    ```
    Success is exit code 1 with `{"status": "failed", "error_type": "DeployError"}` on
    stderr; no checkout, policy or container changes. A job on any branch the `gcp-test`
    environment admits can read its secrets, so before storing any, confirm that its
    deployment branch policy still admits only `develop` (set that way on 2026-10-02):
    ```bash
    gh api repos/we-meet-trip/map-service-infra/environments/gcp-test --jq .deployment_branch_policy
    # {"custom_branch_policies":true,"protected_branches":false}
    gh api repos/we-meet-trip/map-service-infra/environments/gcp-test/deployment-branch-policies \
      --jq '.branch_policies[] | .type + " " + .name'   # branch develop
    ```
    Then store the private key and that host key line as the `gcp-test` environment
    secrets `TEST_DEPLOY_SSH_KEY` and `TEST_DEPLOY_SSH_KNOWN_HOSTS`, and set the
    environment variables `TEST_WIF_PROVIDER`, `TEST_DEPLOYER_SA`, `TEST_GCP_PROJECT`,
    `TEST_GCP_ZONE` and `TEST_INSTANCE`, all with `--env gcp-test` and never at
    repository level. Once `gh secret list --env gcp-test -R we-meet-trip/map-service-infra`
    lists both secrets, delete the local private key: the environment secret is its only
    copy, and a later probe needs a new pair installed like this one.
14. Merge to `develop` and run `image-release` from `develop`. On 2026-10-05
    `gh workflow list -a -R we-meet-trip/map-service-infra` showed `image-release` as
    `disabled_manually`; a disabled workflow neither follows the merge nor accepts a
    dispatch, so the owner enables it first
    (`gh workflow enable image-release -R we-meet-trip/map-service-infra`). In the
    `release.json` of its `release-manifest` artifact, the top-level `infra_sha` and
    `provenance.workflow_sha` must be the merge commit. Only a release built after this
    merge may be received: a receive checks out the bundle's `infra_sha`, and an earlier
    `develop` keeps the dynamic DNS updater in `docker-compose.edge.yml`, which requires
    the `DUCKDNS_SUBDOMAIN` and `DUCKDNS_TOKEN` left empty here, so preflight stops.
    Under `deploy.lock`, write `/var/lib/map-deploy/rollback-policy.json` (root, 0600)
    with that release's six images in `candidate_allowed`, `rollback_verified: []` and
    the `instance_id` string of step 2 (see
    [SECURITY_CUTOVER_ROLLBACK.md](SECURITY_CUTOVER_ROLLBACK.md#policy-contract)), for
    example `sudo flock /var/lib/map-deploy/deploy.lock install -o root -g root -m 0600 <staged policy> /var/lib/map-deploy/rollback-policy.json`.
    Before the receive, confirm on the host that the console stack is healthy:
    `http://127.0.0.1:8202/health/ready` and `http://127.0.0.1:8203/` answer 200 and
    `http://127.0.0.1:8202/api/v1/auth/me` answers 401. `deploy-gcp.py` accepts the
    `cloud-up.sh` exit code 3 (console stack unhealthy) as `console_degraded`, but its
    private smoke still requires those answers, so a console failure during this first
    receive, which has no verified rollback, ends quarantined. That contradicts
    `cloud-up.sh`, which returns 3 so that a console failure keeps the public entry
    points open; it is a product defect left for a later release. Then deploy through
    `deploy-gcp-test`. An automatic run that reached the host before the policy listed
    the release, or that met the deployment lock (`DeploymentBusy`), was refused without
    changes; rerun it with
    `gh workflow run deploy.yml --ref develop -f run_id=<image-release run ID>`.
15. Expect `security-cutover.json` at phase `complete`, a matching
    `security-public-ready.json` and the watchdog journal at
    `{"phase": "completed_public_supervised"}`. Install
    `deploy/map-test-backup.{service,timer}` and
    `deploy/map-test-redis-backup.{service,timer}`, enable both timers and start
    `map-test-backup.service` and `map-test-redis-backup.service` once;
    `/var/lib/map-deploy/backup-status.json` and `redis-backup-status.json` must report
    `"code": "COMPLETE"`. Then run the public smoke against
    `https://test-api.mapservice.app`: `/healthz` 200, `/healthz/app` 200 with
    `status` `UP`, and unauthenticated `/api/v1/users/me` 401.

Road routing is a separate host installation
([OSRM_RUNTIME_RELEASE.md](OSRM_RUNTIME_RELEASE.md)). For this host, render its units
with every option the renderer requires; the script has no execute bit, and `--output`
must not exist yet:
```bash
python3 scripts/osrm-systemd.py --environment test --network map-test-net \
  --release-dir <release dir> --infra-dir <versioned infra tooling dir> \
  --foot-port 5200 --bicycle-port 5201 --output <new dir>
```
It renders `srv-map\x2dosrm\x2dtest.mount` for the fixed mount point
`/srv/map-osrm-test`, `map-osrm-test.service` and `runtime.env`. The service reads
`runtime.env` from `--release-dir`, so copy the rendered file there. Hub's
`OSRM_FOOT_BASE_URL` and `OSRM_BICYCLE_BASE_URL` stay empty until the engine check
passes.

## Evidence scope

`python3 -m unittest discover -s tests -v` covers receiver/policy regressions and
watchdog lock races, unit cleanup states, all nonterminal phases, unchanged healthy
serving, missing/corrupt/stale receipt, wrong image/ID, restart-policy drift,
maintenance, enrollment health-before-change and scoped resume ordering. These are
stdlib local tests with simulated Docker/HTTP calls.

`cutover-recovery-check` uses a disposable GitHub Linux runner, actual systemd and
a separate empty privileged dind daemon with no host socket/host volume mounts,
no network, CPU/RAM/PID limits, and newly created busybox sentinels. It checks a
real MainPID SIGKILL with a detached TERM-ignoring child, real flock exclusion,
four-stop, abrupt inner-daemon termination/start with pending public remaining
closed, unchanged sentinel IDs/images and terminal receipt resumption. No MAP
image, real DB, GCP metadata, actual BFF/public readiness, six production digests
or full VM power cycle is in that synthetic fixture. Its artifact records this
scope explicitly. A fixture PASS must not be relabeled GCP boot/fault acceptance.
