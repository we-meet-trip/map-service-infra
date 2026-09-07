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
the fixed root-owned `/usr/local/lib/map-deploy`. The existing mandatory six-image
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

The root service polls every2s, validates the fixed GCP instance once, and obtains
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
`{"schema_version":1,"instance_id":"2327348931395410137","hold":true}`. The next
watchdog scan stops only the public four; DB/Redis/OSRM/observability remain up.
Normal `docker stop` without that hold is treated as a crash and may be restarted.
To resume, inspect the unchanged completed latch/approved receipt, acquire the same
lock, remove only this explicit maintenance marker, fsync the parent, then let the
watchdog revalidate and start. A pending cutover latch continues quarantine even
after maintenance removal. The receiver rejects deployment while maintenance is
present, before source checkout or mutation; it cannot bypass the hold.

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
