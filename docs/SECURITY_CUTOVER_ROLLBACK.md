# GCP security cutover and application rollback

This receiver requires `/var/lib/map-deploy/rollback-policy.json`. Installing the
receiver without that policy fails before source checkout, image pull or serving
changes. The policy is deliberately outside Git and never generated, extended or
promoted by `receive`.

The first release that enforces the current age/terms and moderation contracts must
not automatically fall back to a release missing those controls. This remains true
when the database schema happens to be backward compatible.

## Policy contract

The JSON object has exactly these four keys:

- `schema_version`: integer `1` (not boolean).
- `instance_id`: exact GCP instance ID `2327348931395410137`.
- `candidate_allowed`: nonempty list of complete six-image objects.
- `rollback_verified`: list of complete six-image objects, initially empty.

Each image object has exactly `user`, `agent`, `hub`, `yolo`, `admin`, `admin-web`.
Each value must be `ghcr.io/we-meet-trip/map-service-<service>@sha256:<64 lowercase hex>`.
No tags, foreign repositories, extra/missing keys, duplicate JSON keys or duplicate
tuples are accepted. Each list is capped at 32 tuples; the entire file at 64 KiB.
The file must be a regular root-owned file of mode 0600 or 0644, opened with
`O_NOFOLLOW`. The state directory must be root-owned, not a symlink, and not
writable by group/others.

For first security cutover, an operator copies the exact reviewed six digests from
the authenticated release bundle into `candidate_allowed`, leaving
`rollback_verified: []`. Installing the file is an explicit host-policy action;
app workflows cannot do it. Root must stage and validate both receiver and policy
under the shared deployment lock before atomically replacing installed files. Keep
the lock throughout the pair installation so no receiver observes half an update.
Retain a checksummed copy of the previous receiver/policy for inspection, but never
restore an old receiver to bypass this guard or add an unsafe release to the list.

A candidate must match one entire allowed object before deployment starts. For a
prior release, the receiver captures each container's actual `.Image`. It resolves
each `rollback_verified` `repository@digest` using **local** `docker image inspect`
and compares `.Id` for all six images to those captured identities. Missing local
images, incomplete snapshots or any mismatch prohibit rollback. OCI configuration,
manifest/index digests and tags are not assumed interchangeable. It never pulls a
prior image just to confer rollback permission. A detached administrator currently
cannot supply a complete local six-image snapshot and therefore conservatively has
no automatic application rollback through this six-image policy.

Only a separately reviewed, exact-release validation can justify an operator adding
a tuple to `rollback_verified`. Receiver health success does not perform that
promotion and is not evidence of overall service/store readiness.

## Deployment and failure behavior

1. Authenticate bundle/host and policy, lock, capture prior source/environment,
   active images and immutable infrastructure. Finish preflight, Admin migration
   compatibility and verified remote PostgreSQL prebackup.
2. Persist candidate environment and a mode-0600 `security-cutover.json` latch via
   file and parent-directory fsync. Its `starting_private` phase precedes the first
   serving mutation. Gracefully stop `edge` and verify it is stopped.
3. Run pinned `cloud-up.sh` without `--edge`. Its explicit application service list
   starts the private app stack, preserving existing PostgreSQL/Redis and image
   pins. Existing DNS remains running. Verify edge stays stopped, infrastructure
   identities and loopback health/readiness/unauthorized-account responses.
4. Persist `private_ready`, then `opening_ingress`. Start only the exact pinned
   edge with `--no-deps --no-build --pull never`. Verify all infrastructure and
   public smoke responses. Persist current release and `complete`.
5. On post-start failure, use the existing application rollback only when the prior
   **entire** tuple was independently verified. Pre-start failure restores only
   source/environment and does not start old applications.
6. With no verified prior, preserve candidate source, environment and bundle.
   Gracefully stop **edge, proxy, user, yolo**, attempting all four even if a stop
   fails, then independently verify all four have zero running containers. Store
   `quarantined` or `quarantine_failed`, never report deployment success. A failed
   verified rollback also attempts this quarantine. There is no down, prune,
   database/container removal or volume operation in this path.

The four services match the inspected ingress: Caddy accepts public 80/443 and
forwards to proxy; proxy routes REST/chat to User and Vision websocket to YOLO.
GCP's other application bindings are loopback. This action closes existing
websockets as well as the public listener. Agent, Hub, databases, Redis, independent
OSRM, monitoring and their volumes are preserved.

A nonterminal latch forces quarantine at the next accepted receive, including
when the prior images match a verified tuple. Stopped containers are included in
that retry's snapshot. A partially started candidate never counts as a validated
prior. The latch remains outside the checkout; source rollback cannot remove it.
Neither a successful run nor a retry modifies `rollback-policy.json`.

SIGTERM, SIGINT and SIGHUP become cleanup exceptions. The command runner stops its
whole subprocess group (TERM, bounded grace, KILL) on timeout or interruption
before the receiver handles rollback/quarantine. Repeated termination signals
cannot interrupt that cleanup. Failure logging contains only phase/type metadata,
never subprocess output, environment values or request bodies.

## Verification scope and residual operational gate

The stdlib suite injects unapproved/missing/tampered policy, first-transition
prebackup/private/public failure, compatible rollback, interrupted retry and process
interruption. It checks exact-six local identity, all-four stop attempts, candidate
preservation, no policy promotion, and private readiness before edge opening. These
are local tests with simulated Docker/HTTP calls, not an actual GCP crash drill.

SIGKILL, kernel failure and power loss cannot run Python cleanup. The durable latch
prevents a subsequent receive from granting unsafe rollback, and a completed manual
Docker stop remains stopped under the current `unless-stopped` restart policy.
However, without a separate boot/watchdog recovery command, immediate closure in
all hard-kill windows (especially after edge opening and before `complete`) is not
proven. A real reboot/hard-kill drill and independent recovery supervision remain
an explicit operational gate. Do not reboot Docker/VM as a routine check on the
current data-bearing host. The latch does not itself supervise an orphan after
SIGKILL. Existing PG/Redis/OSRM restoration and full RPO/RTO acceptance are separate.
