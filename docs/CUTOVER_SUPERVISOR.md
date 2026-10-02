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
`CF-Connecting-IP` with the address it observes). Docker Engine 28.0.0 or later must
already be installed: the migration jobs create networks with an isolated IPv4
gateway. Run every host step as root, in this order.

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
     account, which the Hub migration contract refuses);
   - `POSTGRES_EXPORTER_DSN` with the `map_pg_exporter` login and database `map_test`
     (the example value names database `map`);
   - runtime passwords and internal tokens from Secret Manager: on the VM, whose service
     account can read the secrets listed in the Terraform variable `runtime_secret_ids`,
     `python3 scripts/gcp_secrets.py materialize --project mapservice-test --output <root-only file> KEY=SECRET_NAME...`
     writes them to a 0600 file and refuses any value outside `[A-Za-z0-9._~+/=:@?&%,-]`;
     merge those keys into `.env.test` without displaying them, then delete the file;
   - `EDGE_DOMAIN=test-api.mapservice.app` and `EDGE_EMAIL`; `DUCKDNS_SUBDOMAIN` and
     `DUCKDNS_TOKEN` stay empty;
   - `APPLE_ENABLED=false` and an empty `KAKAO_APP_ID=`, so this environment never
     accepts real Apple or Kakao sign-in;
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
   `guard_retry_instance_verification` means `target.json` or the machine identity is
   wrong. After Docker is installed, also install and enable
   `deploy/gcp/map-metadata-block.service`: it drops container traffic to
   `169.254.169.254` except DNS on port 53; host processes are unaffected.
9. Prepare the empty database. Start PostgreSQL alone, the only raw Compose call here;
   it is none of the public four, and its first boot runs `db/init` (PostGIS, the four
   schemas and the `map_admin` login):
   ```bash
   cd /srv/map-test/map-service-infra
   sudo docker compose --env-file .env.test -f docker-compose.yml -f docker-compose.test.yml --profile infra up -d postgres
   ```
   Then, as the database operator:
   - run the User bootstrap first (map-service-user `docs/user-database-bootstrap.md`:
     prepare SQL, one `UserBootstrapApplication` run, finalize SQL), which provides
     `map_user_owner`, `map_user_migrator` and `map_user_runtime`; the normal migrator
     refuses an unbootstrapped database;
   - apply Hub's and Agent's `docs/database-roles.sql` for `map_hub_owner`,
     `map_hub_migrator`, `map_hub_runtime` and `map_agent_owner`, `map_agent_migrator`,
     `map_agent_runtime`;
   - create the test exporter login `map_pg_exporter` holding only `pg_monitor`;
   - give each runtime login and `map_pg_exporter` the password merged into `.env.test`
     in step 5, and each migrator its own, different Secret Manager password.

   Then write the three `/etc/map-deploy/{user,hub,agent}-migration.env` files as in
   [SERVICE_MIGRATION_DEPLOYMENT.md](SERVICE_MIGRATION_DEPLOYMENT.md): root, mode 0600,
   a single link, no comments or blank lines.
10. With the owner's GitHub login, download the two edge assets of the draft release
    and compare their SHA-256 with `archive_sha256` and `report_sha256` in
    [install-artifact-20260906.json](../docker/caddy-security/install-artifact-20260906.json).
    The release's own asset of that name is an older version; the repository file is
    the anchor.
    ```bash
    gh release download caddy-security-20260906-v1 -R we-meet-trip/map-service-infra \
      -p map-caddy-security-image-20260906.tar -p caddy-security-20260906.json
    ```
    Copy both with `gcloud compute scp --tunnel-through-iap`, then in `repo`:
    ```bash
    sudo python3 scripts/install-caddy-artifact.py \
      --archive <dir>/map-caddy-security-image-20260906.tar \
      --report <dir>/caddy-security-20260906.json \
      --install-compose /etc/map-deploy/caddy-verified.yml
    ```
11. In Cloudflare, create the DNS-only (not proxied) A record `test-api.mapservice.app`
    for the address of `map-test-ip`, which
    `gcloud compute addresses describe map-test-ip --region us-central1 --project mapservice-test --format='value(address)'`
    prints, and confirm that `dig +short test-api.mapservice.app` returns exactly that
    address. Caddy requests its certificate when edge starts in the next step, and the
    first receive checks public readiness through `public_url`; a missing or proxied
    record fails that check after ingress opens, and a new host has no verified rollback,
    so it ends quarantined.
12. Start the first stack from a verified release bundle. Tag images cannot be used: the
    migration jobs accept only `ghcr.io/we-meet-trip/map-service-<service>@sha256:…` and
    refuse a tag with `serving_image_not_pinned`, so no application starts. On a
    workstation, verify the latest successful `develop` `image-release`; `cloud-up.sh`
    takes only image digests from the bundle and its Compose files from `repo`, so a run
    from before the merge in step 14 works here:
    ```bash
    GH_TOKEN="$(gh auth token)" python3 scripts/deploy-gcp.py prepare \
      --run-id <develop image-release run ID> --output <bundle> --target-environment test
    ```
    `<bundle>` must not exist yet. Copy it to the host with
    `gcloud compute scp --recurse --tunnel-through-iap`, check the copy with
    `sudo python3 /usr/local/lib/map-deploy/release_manifest.py verify --bundle <bundle> --expected-run-id <run ID>`,
    and set `IMAGE_TAG` in `.env.test` to the `release_tag` of `<bundle>/release.json`
    (`docker-compose.registry.yml` still interpolates it; every receive rewrites it).
    With `public-restart.yml` in place, start the initial stack with the role flags the
    receiver uses:
    ```bash
    cd /srv/map-test/map-service-infra
    sudo RELEASE_BUNDLE=<bundle> EDGE_IMAGE_OVERRIDE=/etc/map-deploy/caddy-verified.yml \
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
    latch); start them again with the same command, so keep `<bundle>` until then.
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
    `/etc/ssh/ssh_host_ed25519_key.pub` read over the IAP session, then submit the
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
    repository level.
14. Merge to `develop` and run `image-release` from `develop`. In the `release.json` of
    its `release-manifest` artifact, the top-level `infra_sha` and
    `provenance.workflow_sha` must be the merge commit. Only a release built after this
    merge may be received: a receive checks out the bundle's `infra_sha`, and an earlier
    `develop` keeps the dynamic DNS updater in `docker-compose.edge.yml`, which requires
    the `DUCKDNS_SUBDOMAIN` and `DUCKDNS_TOKEN` left empty here, so preflight stops.
    Under `deploy.lock`, write `/var/lib/map-deploy/rollback-policy.json` (root, 0600)
    with that release's six images in `candidate_allowed`, `rollback_verified: []` and
    the `instance_id` string of step 2 (see
    [SECURITY_CUTOVER_ROLLBACK.md](SECURITY_CUTOVER_ROLLBACK.md#policy-contract)), for
    example `sudo flock /var/lib/map-deploy/deploy.lock install -o root -g root -m 0600 <staged policy> /var/lib/map-deploy/rollback-policy.json`.
    Then deploy through `deploy-gcp-test`. An automatic run that reached the host
    before the policy listed the release was refused without changes; rerun it with
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
